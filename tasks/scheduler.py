"""Scheduler — all periodic background jobs."""

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from vkbottle import API

from core.crypto import decrypt_token
from core.telegram import send_to_telegram
from core.text_guard import is_llm_failure, is_publishable as _is_publishable
from tasks.content_parser import fetch_and_schedule
from database.service import (
    get_all_active_groups, get_setting, set_setting,
    claim_due_posts, reset_stale_publishing, mark_post_published, mark_post_failed,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ─── Job 1: Auto-post (only real content from sources) ──────────────────────

async def _autopost_job():
    """
    Auto-posting: fetch fresh content from group's sources and schedule it.
    No content sources or nothing new → bot stays silent. We never publish
    "filler" themed posts just to keep the schedule going.
    """
    from database.service import get_content_sources

    groups = await get_all_active_groups()

    for group in groups:
        try:
            enabled = (await get_setting(group.group_id, "autopost_enabled", "false")).lower()
            if enabled != "true":
                continue

            try:
                interval_hours = int(await get_setting(group.group_id, "autopost_interval_hours", "6"))
            except ValueError:
                interval_hours = 6
            last_post_str = await get_setting(group.group_id, "_last_autopost", "")

            if last_post_str:
                try:
                    last_post_time = datetime.fromisoformat(last_post_str)
                    elapsed = (datetime.now(timezone.utc) - last_post_time).total_seconds() / 3600
                    if elapsed < interval_hours:
                        continue
                except ValueError:
                    pass

            sources = await get_content_sources(group.group_id)
            if not sources:
                # Nothing to draw from — don't invent a post.
                continue

            logger.info(f"Auto-post: fetching sources for group {group.group_id}...")
            count = await fetch_and_schedule(group.group_id)

            if count > 0:
                await set_setting(group.group_id, "_last_autopost", datetime.now(timezone.utc).isoformat())
                logger.info(f"Auto-post: scheduled {count} posts for group {group.group_id}")
            else:
                logger.info(f"Auto-post: no fresh content for group {group.group_id}, staying silent")

        except Exception as e:
            logger.error(f"Auto-post failed for group {group.group_id}: {e}")


# ─── Job 2: Scheduled posts publisher ───────────────────────────────────────

async def _scheduled_posts_job():
    # Recover any post stuck mid-publish (crash between claim and result), then
    # atomically claim due posts so none is ever published twice.
    await reset_stale_publishing()
    posts = await claim_due_posts()
    if not posts:
        return

    # Group posts by group_id to reuse API instances
    from collections import defaultdict
    by_group = defaultdict(list)
    for p in posts:
        by_group[p.group_id].append(p)

    groups = await get_all_active_groups()
    group_map = {g.group_id: g for g in groups}

    for gid, group_posts in by_group.items():
        group = group_map.get(gid)
        if not group:
            continue

        try:
            token = decrypt_token(group.access_token)
            api = API(token=token)

            for p in group_posts:
                # Заглушка сбоя ИИ, попавшая в план (schedule_post с генерацией),
                # на стену не идёт — и повторять её бессмысленно.
                if is_llm_failure(p.text):
                    logger.error(f"Scheduled post #{p.id} holds an LLM failure text — not publishing")
                    await mark_post_failed(p.id, max_attempts=1)
                    continue
                try:
                    attachments = p.attachments or ""
                    post_kwargs = {"owner_id": -gid, "message": p.text}
                    if attachments:
                        post_kwargs["attachments"] = attachments
                    result = await api.wall.post(**post_kwargs)
                    vk_post_id = result.post_id if result else 0
                    await mark_post_published(p.id, vk_post_id)
                    logger.info(f"Scheduled post #{p.id} published for group {gid}")

                    # Cross-post to Telegram
                    try:
                        await send_to_telegram(gid, p.text, vk_post_id)
                    except Exception as tg_err:
                        logger.warning(f"Telegram cross-post failed for post #{p.id}: {tg_err}")

                except Exception as e:
                    logger.error(f"Failed to publish scheduled post #{p.id}: {e}")
                    await mark_post_failed(p.id)
        except Exception as e:
            logger.error(f"Scheduled posts error for group {gid}: {e}")


# ─── Job 4: Content tasks (smart copywriter tasks) ──────────────────────────

async def _content_tasks_job():
    """Check and execute scheduled content tasks (patch notes, articles, etc.)."""
    from datetime import timedelta
    from croniter import croniter
    from core.content_writer import write_article
    from core.images import find_and_upload_image
    from database.service import (
        get_all_active_content_tasks, update_content_task_run,
        create_scheduled_post, delete_content_task,
    )

    tasks = await get_all_active_content_tasks()
    now = datetime.now(timezone.utc)

    for task in tasks:
        try:
            # Check if it's time to run this task
            cron = croniter(task.schedule_cron, task.last_run_at or (now - timedelta(days=30)))
            next_run = cron.get_next(datetime)
            # Make next_run timezone-aware if it isn't
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)

            if next_run > now:
                continue

            logger.info(f"Content task #{task.id} '{task.name}' running for group {task.group_id}...")

            # Generate content based on task type.
            # patch_notes (GitHub commits) was removed — it produced "Нет коммитов"
            # junk; the site source (content_parser) is the single content path now.
            if task.task_type == "article":
                text = await write_article(
                    group_id=task.group_id,
                    source_url=task.source_url,
                    instruction=task.instruction,
                    length=task.length,
                )
            elif task.task_type == "digest":
                text = await write_article(
                    group_id=task.group_id,
                    source_url=task.source_url,
                    instruction=task.instruction or "Напиши дайджест новостей за неделю",
                    length="medium",
                )
            else:
                # Исполнителя для такого типа нет (patch_notes удалён) — задача
                # никогда не выполнится; выключаем, а не повторяем каждые 30 мин.
                logger.warning(
                    f"Content task #{task.id} has unknown type {task.task_type!r} — deactivating"
                )
                await delete_content_task(task.id, task.group_id)
                continue

            if _is_publishable(text):
                # Try to find and upload a relevant image
                attachment = ""
                try:
                    from database.service import get_group as _get_group
                    group = await _get_group(task.group_id)
                    if group:
                        token = decrypt_token(group.access_token)
                        api = API(token=token)
                        attachment = await find_and_upload_image(api, task.group_id, post_text=text) or ""
                except Exception as img_err:
                    logger.warning(f"Image upload failed for task #{task.id}: {img_err}")

                # Schedule for publication in 10 minutes
                scheduled_at = now + timedelta(minutes=10)
                await create_scheduled_post(
                    group_id=task.group_id,
                    text=text,
                    scheduled_at=scheduled_at,
                    source=f"task:{task.name}",
                    attachments=attachment,
                )
                logger.info(f"Content task #{task.id} generated post for group {task.group_id}")
            else:
                logger.warning(f"Content task #{task.id} produced no publishable post: {(text or '')[:100]!r}")

            await update_content_task_run(task.id)

        except Exception as e:
            logger.error(f"Content task #{task.id} error: {e}")


# ─── Job 5: Analytics collector ──────────────────────────────────────────────

async def _analytics_job():
    from tasks.analytics import collect_analytics
    await collect_analytics()


# ─── Job 6: Widget refresh ─────────────────────────────────────────────────

async def _widget_refresh_job():
    from core.widgets import update_all_widgets
    await update_all_widgets()


# ─── Job 7: Daily proactive summary (digest + public welcome + milestones) ──
#
# This is the job that makes the bot feel *alive* instead of purely reactive:
# once a day it greets new members publicly (DMs are blocked by VK), DMs the
# admin a digest built from collected analytics, and celebrates membership
# milestones on the wall.

_MILESTONES = [50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]


def _last_milestone_reached(count: int) -> int:
    reached = 0
    for m in _MILESTONES:
        if count >= m:
            reached = m
    return reached


async def _get_member_count(api, group_id: int) -> int | None:
    try:
        resp = await api.groups.get_members(group_id=group_id, count=0)
        return resp.count if resp else None
    except Exception as e:
        logger.warning(f"member count fetch failed for group {group_id}: {e}")
        return None


async def _public_welcome(api, group_id: int, welcomes: list[dict]) -> None:
    """Post ONE public wall post greeting newcomers (avoids per-user DM spam and
    works around VK blocking group→user DMs for non-openers)."""
    if not welcomes:
        return
    enabled = (
        (await get_setting(group_id, "welcome_ai", "false")).lower() == "true"
        or bool(await get_setting(group_id, "welcome_message", ""))
    )
    if not enabled:
        return

    mentions = ", ".join(f"[id{w['id']}|{w.get('name') or 'друг'}]" for w in welcomes[:30])
    intro = "Рады новым участникам! 👋"
    try:
        from core.ai_brain import generate_response, _get_group_ai_context
        ai_ctx = await _get_group_ai_context(group_id)
        sys = (ai_ctx.get("ai_system_prompt") or
               "Ты дружелюбный администратор группы ВКонтакте.")
        text = await generate_response(
            prompt="Напиши короткое (1-2 предложения) тёплое публичное приветствие "
                   "для новых участников группы. Без обращения по имени — имена допишутся отдельно.",
            system_prompt=sys, group_id=group_id,
        )
        if not is_llm_failure(text):
            intro = text.strip()
    except Exception as e:
        logger.warning(f"AI welcome generation failed for group {group_id}: {e}")

    message = f"{intro}\n\n{mentions}"
    try:
        await api.wall.post(owner_id=-group_id, message=message)
        logger.info(f"Public welcome posted for {len(welcomes)} newcomers in group {group_id}")
    except Exception as e:
        logger.warning(f"Public welcome post failed for group {group_id}: {e}")


async def _admin_digest(api, group_id: int, admin_vk_id: int, joins: int, leaves: int) -> None:
    from database.service import get_post_analytics, get_escalations_since, count_active_dialogs
    analytics = await get_post_analytics(group_id, limit=20)

    lines = ["📊 Сводка по группе за сутки:"]
    if analytics:
        total_likes = sum(a.likes or 0 for a in analytics)
        total_views = sum(a.views or 0 for a in analytics)
        total_comments = sum(a.comments or 0 for a in analytics)
        top = max(analytics, key=lambda a: (a.likes or 0) + (a.reposts or 0) + (a.comments or 0))
        lines.append(
            f"• Последние {len(analytics)} постов: 👍 {total_likes}, 💬 {total_comments}, 👁 {total_views}"
        )
        lines.append(
            f"• Лучший пост: 👍 {top.likes or 0} / 💬 {top.comments or 0} / 👁 {top.views or 0}"
        )
    else:
        lines.append("• Постов с аналитикой пока нет.")
    lines.append(f"• Новых участников: +{joins}, вышло: −{leaves}")

    # Пульс «живого админа»: сколько людей говорило с ботом и кого он звал.
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    dialogs = await count_active_dialogs(group_id, since)
    escalations = await get_escalations_since(group_id, since)
    unresolved = [e for e in escalations if not e.resolved]
    if dialogs:
        lines.append(f"• Диалогов с ботом: {dialogs}")
    if escalations:
        lines.append(f"• Звали человека: {len(escalations)} (не закрыто: {len(unresolved)})")
        for e in unresolved[:5]:
            lines.append(f"   — {e.user_name} (vk.com/id{e.vk_id}): {e.reason[:60]}")

    if not analytics and joins == 0 and leaves == 0 and dialogs == 0 and not escalations:
        return  # nothing worth pinging the admin about

    try:
        await api.messages.send(user_id=admin_vk_id, message="\n".join(lines), random_id=0)
        logger.info(f"Admin digest sent to {admin_vk_id} for group {group_id}")
    except Exception as e:
        logger.warning(f"Admin digest DM failed for group {group_id}: {e}")


async def _milestone_post(api, group_id: int, count: int | None) -> None:
    if not count:
        return
    reached = _last_milestone_reached(count)
    if reached == 0:
        return
    try:
        last_celebrated = int(await get_setting(group_id, "_last_member_milestone", "0"))
    except ValueError:
        last_celebrated = 0
    if reached <= last_celebrated:
        return
    try:
        await api.wall.post(
            owner_id=-group_id,
            message=f"🎉 Нас уже {reached}! Спасибо каждому, кто с нами. Дальше — больше! 🚀",
        )
        await set_setting(group_id, "_last_member_milestone", str(reached))
        logger.info(f"Milestone {reached} celebrated for group {group_id}")
    except Exception as e:
        logger.warning(f"Milestone post failed for group {group_id}: {e}")


# Дайджест — раз в календарный день по Москве, не раньше DIGEST_HOUR. Почасовой
# триггер + проверка даты переживают рестарты (не успели в 10:00 — догоним позже),
# а старый гейт «прошло 20 ч» сдвигал отправку на 4 ч в сутки, вплоть до ночи.
DIGEST_TZ = ZoneInfo("Europe/Moscow")
DIGEST_HOUR = 10


def _daily_summary_due(last_str: str, now: datetime) -> bool:
    local_now = now.astimezone(DIGEST_TZ)
    if local_now.hour < DIGEST_HOUR:
        return False
    try:
        last = datetime.fromisoformat(last_str)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.astimezone(DIGEST_TZ).date() < local_now.date()


async def _daily_summary_job():
    from database.service import take_daily_membership_state

    groups = await get_all_active_groups()
    now = datetime.now(timezone.utc)

    for group in groups:
        try:
            # Once per Moscow calendar day, from DIGEST_HOUR (trigger fires hourly).
            last_str = await get_setting(group.group_id, "_last_daily_summary", "")
            if not _daily_summary_due(last_str, now):
                continue

            token = decrypt_token(group.access_token)
            api = API(token=token)

            state = await take_daily_membership_state(group.group_id)
            await _public_welcome(api, group.group_id, state["welcomes"])
            await _admin_digest(
                api, group.group_id, group.admin_vk_id,
                state["joins"], state["leaves"],
            )
            count = await _get_member_count(api, group.group_id)
            await _milestone_post(api, group.group_id, count)

            await set_setting(group.group_id, "_last_daily_summary", now.isoformat())
        except Exception as e:
            logger.error(f"Daily summary failed for group {group.group_id}: {e}")


# ─── Note: _content_tasks_job uses croniter; install with: pip install croniter


# ─── Scheduler startup ──────────────────────────────────────────────────────

async def start_scheduler():
    # Auto-posting: check every hour
    scheduler.add_job(
        _autopost_job,
        trigger=IntervalTrigger(hours=1),
        id="autopost", replace_existing=True,
    )

    # Scheduled posts: check every 5 minutes
    scheduler.add_job(
        _scheduled_posts_job,
        trigger=IntervalTrigger(minutes=5),
        id="scheduled_posts", replace_existing=True,
    )

    # Content tasks: check every 30 minutes
    scheduler.add_job(
        _content_tasks_job,
        trigger=IntervalTrigger(minutes=30),
        id="content_tasks", replace_existing=True,
    )

    # Analytics: hourly, first run ~1 min after start — otherwise a restart
    # left the dashboard without stats for up to 6 hours. wall.get goes via
    # the service key (one call per group), so hourly is cheap.
    scheduler.add_job(
        _analytics_job,
        trigger=IntervalTrigger(hours=1),
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        id="analytics", replace_existing=True,
    )

    # Widget refresh: every 1 hour
    scheduler.add_job(
        _widget_refresh_job,
        trigger=IntervalTrigger(hours=1),
        id="widget_refresh", replace_existing=True,
    )

    # Daily proactive summary: trigger hourly, but each group acts once/~day
    # (digest to admin + public welcome + membership milestones).
    scheduler.add_job(
        _daily_summary_job,
        trigger=IntervalTrigger(hours=1),
        id="daily_summary", replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: autopost(1h), scheduled_posts(5m), "
        "content_tasks(30m), analytics(1h), widgets(1h), "
        "daily_summary(1h/once-a-day from 10:00 MSK)"
    )
