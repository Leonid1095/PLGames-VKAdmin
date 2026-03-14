"""Scheduler — all periodic background jobs."""

import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from vkbottle import API

from core.ai_brain import generate_post
from core.crypto import decrypt_token
from core.telegram import send_to_telegram
from tasks.content_parser import fetch_and_schedule
from database.service import (
    get_all_active_groups, get_setting, set_setting,
    get_due_posts, mark_post_published, mark_post_failed,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ─── Job 1: Auto-post (AI-generated, per-group interval) ────────────────────

async def _autopost_job():
    """
    Auto-posting: fetch content from group sources, then write a real post.
    Never generates from nothing — always uses source material.
    """
    from database.service import get_content_sources

    groups = await get_all_active_groups()

    for group in groups:
        try:
            enabled = (await get_setting(group.group_id, "autopost_enabled", "false")).lower()
            if enabled != "true":
                continue

            interval_hours = int(await get_setting(group.group_id, "autopost_interval_hours", "6"))
            last_post_str = await get_setting(group.group_id, "_last_autopost", "")

            if last_post_str:
                try:
                    last_post_time = datetime.fromisoformat(last_post_str)
                    elapsed = (datetime.now(timezone.utc) - last_post_time).total_seconds() / 3600
                    if elapsed < interval_hours:
                        continue
                except ValueError:
                    pass

            # Check if group has content sources
            sources = await get_content_sources(group.group_id)
            if not sources:
                logger.info(f"Auto-post: group {group.group_id} has no content sources, skipping")
                continue

            # Fetch real content from sources and schedule post
            logger.info(f"Auto-post: fetching sources for group {group.group_id}...")
            count = await fetch_and_schedule(group.group_id)

            if count > 0:
                await set_setting(group.group_id, "_last_autopost", datetime.now(timezone.utc).isoformat())
                logger.info(f"Auto-post: scheduled {count} posts for group {group.group_id}")
            else:
                logger.info(f"Auto-post: no new content from sources for group {group.group_id}")

        except Exception as e:
            logger.error(f"Auto-post failed for group {group.group_id}: {e}")


# ─── Job 2: Scheduled posts publisher ───────────────────────────────────────

async def _scheduled_posts_job():
    posts = await get_due_posts()
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
                try:
                    result = await api.wall.post(owner_id=-gid, message=p.text)
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


# ─── Job 3: Content parser ──────────────────────────────────────────────────

async def _content_parse_job():
    """Parse content sources for groups that don't have autopost enabled (those are handled by _autopost_job)."""
    groups = await get_all_active_groups()
    for group in groups:
        try:
            autopost = (await get_setting(group.group_id, "autopost_enabled", "false")).lower()
            if autopost == "true":
                continue  # already handled by _autopost_job

            count = await fetch_and_schedule(group.group_id)
            if count:
                logger.info(f"Content parser: scheduled {count} posts for group {group.group_id}")
        except Exception as e:
            logger.error(f"Content parser error for group {group.group_id}: {e}")


# ─── Job 4: Content tasks (smart copywriter tasks) ──────────────────────────

async def _content_tasks_job():
    """Check and execute scheduled content tasks (patch notes, articles, etc.)."""
    from datetime import timedelta
    from croniter import croniter
    from core.content_writer import write_article, write_patch_notes
    from database.service import (
        get_all_active_content_tasks, update_content_task_run,
        create_scheduled_post,
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

            # Generate content based on task type
            if task.task_type == "patch_notes" and task.source_url:
                text = await write_patch_notes(
                    group_id=task.group_id,
                    github_url=task.source_url,
                    days=7,
                )
            elif task.task_type == "article":
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
                logger.warning(f"Unknown task type: {task.task_type}")
                continue

            if text and not text.startswith("Ошибка") and not text.startswith("Не удалось"):
                # Schedule for publication in 10 minutes
                scheduled_at = now + timedelta(minutes=10)
                await create_scheduled_post(
                    group_id=task.group_id,
                    text=text,
                    scheduled_at=scheduled_at,
                    source=f"task:{task.name}",
                )
                logger.info(f"Content task #{task.id} generated post for group {task.group_id}")
            else:
                logger.warning(f"Content task #{task.id} failed to generate: {text[:100]}")

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

    # Content parser: check every 4 hours
    scheduler.add_job(
        _content_parse_job,
        trigger=IntervalTrigger(hours=4),
        id="content_parser", replace_existing=True,
    )

    # Content tasks: check every 30 minutes
    scheduler.add_job(
        _content_tasks_job,
        trigger=IntervalTrigger(minutes=30),
        id="content_tasks", replace_existing=True,
    )

    # Analytics: collect every 6 hours
    scheduler.add_job(
        _analytics_job,
        trigger=IntervalTrigger(hours=6),
        id="analytics", replace_existing=True,
    )

    # Widget refresh: every 1 hour
    scheduler.add_job(
        _widget_refresh_job,
        trigger=IntervalTrigger(hours=1),
        id="widget_refresh", replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: autopost(1h), scheduled_posts(5m), "
        "content_parser(4h), content_tasks(30m), analytics(6h), widgets(1h)"
    )
