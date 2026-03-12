"""Admin command handler — full-featured group administration."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from core.ai_brain import generate_post
from core.group_context import GroupContext
from database.service import (
    get_setting, set_setting, clear_user_history, grant_vip,
    get_pending_suggestions, get_suggestion, review_suggestion,
    create_scheduled_post, get_content_plan,
    add_content_source, get_content_sources, delete_content_source,
    get_post_analytics, get_top_users,
    create_ban_record, remove_ban_record, get_ban_history,
    create_newsletter, update_newsletter_progress,
)

logger = logging.getLogger(__name__)


def is_owner(ctx: GroupContext, user_id: int) -> bool:
    return user_id == ctx.admin_vk_id


async def handle_admin_command(ctx: GroupContext, from_id: int, text: str, peer_id: int) -> str | None:
    if not text.startswith("/"):
        return None
    if not is_owner(ctx, from_id):
        return None

    parts = text.split(maxsplit=2)
    cmd = parts[0].lower()

    handler = COMMANDS.get(cmd)
    if handler:
        return await handler(ctx, from_id, parts, peer_id)
    return None


# ─── Individual command handlers ─────────────────────────────────────────────

async def cmd_help(ctx, from_id, parts, peer_id) -> str:
    return (
        "Команды администратора:\n\n"
        "📝 Контент:\n"
        "/пост [тема] — сгенерировать и опубликовать пост\n"
        "/запланировать ЧЧ:ММ текст — запланировать пост\n"
        "/контентплан — посты на сегодня\n"
        "/предложка — предложенные посты\n"
        "/принять <id> — опубликовать предложение\n"
        "/отклонить <id> [причина] — отклонить\n\n"
        "📊 Аналитика:\n"
        "/стата — статистика группы\n"
        "/аналитика — статистика постов\n"
        "/топ [xp|rep|messages] — рейтинг участников\n\n"
        "🛡 Модерация:\n"
        "/бан <vk_id> [причина] — забанить\n"
        "/разбан <vk_id> — разбанить\n"
        "/баны — история банов\n"
        "/закреп <post_id> — закрепить пост\n\n"
        "📡 Парсинг:\n"
        "/источник добавить rss <url> — добавить RSS\n"
        "/источник список — список источников\n"
        "/источник удалить <id> — удалить\n\n"
        "📨 Рассылка:\n"
        "/рассылка текст — отправить всем участникам\n\n"
        "⚙️ Настройки:\n"
        "/настройка <ключ> <значение>\n"
        "/посмотреть <ключ>\n"
        "/очистить <vk_id> — очистить память\n"
        "/vip <vk_id> <days>\n"
        "/помощь — это сообщение"
    )


async def cmd_post(ctx, from_id, parts, peer_id) -> str:
    topic = parts[1] if len(parts) > 1 else ""
    post_text = await generate_post(group_id=ctx.group_id, topic=topic)
    try:
        await ctx.api.wall.post(owner_id=-ctx.group_id, message=post_text)
        return f"Пост опубликован!\n\n{post_text}"
    except Exception as e:
        logger.error(f"Failed to publish post: {e}")
        return f"Ошибка публикации: {e}"


async def cmd_schedule(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 3:
        return "Формат: /запланировать ЧЧ:ММ текст поста"
    try:
        time_str = parts[1].strip()
        h, m = map(int, time_str.split(":"))
        now = datetime.now(timezone.utc)
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
        text = parts[2].strip()
        post = await create_scheduled_post(ctx.group_id, text, scheduled, source="manual")
        return f"Пост #{post.id} запланирован на {scheduled.strftime('%d.%m %H:%M')} UTC"
    except ValueError:
        return "Формат времени: ЧЧ:ММ (например 14:30)"


async def cmd_content_plan(ctx, from_id, parts, peer_id) -> str:
    now = datetime.now(timezone.utc)
    posts = await get_content_plan(ctx.group_id, now)
    if not posts:
        return "На сегодня нет запланированных постов."
    lines = ["Контент-план на сегодня:\n"]
    for p in posts:
        time_str = p.scheduled_at.strftime("%H:%M")
        status_icon = {"pending": "⏳", "published": "✅", "failed": "❌"}.get(p.status, "?")
        lines.append(f"{status_icon} {time_str} — {p.text[:60]}... [{p.source}]")
    return "\n".join(lines)


async def cmd_suggestions(ctx, from_id, parts, peer_id) -> str:
    posts = await get_pending_suggestions(ctx.group_id)
    if not posts:
        return "Нет предложенных постов на рассмотрение."
    lines = ["Предложенные посты:\n"]
    for p in posts:
        lines.append(f"#{p.id} от vk.com/id{p.from_vk_id}:\n{p.text[:100]}...\n")
    lines.append("Ответьте /принять <id> или /отклонить <id> [причина]")
    return "\n".join(lines)


async def cmd_accept(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /принять <id>"
    try:
        sid = int(parts[1].strip())
    except ValueError:
        return "Укажите числовой ID предложения."
    suggestion = await get_suggestion(sid)
    if not suggestion or suggestion.group_id != ctx.group_id:
        return "Предложение не найдено."
    if suggestion.status != "pending":
        return f"Предложение уже обработано (статус: {suggestion.status})."

    await review_suggestion(sid, "approved", from_id)
    try:
        result = await ctx.api.wall.post(owner_id=-ctx.group_id, message=suggestion.text)
        await review_suggestion(sid, "published", from_id)
        # Notify author
        try:
            await ctx.api.messages.send(
                user_id=suggestion.from_vk_id,
                message=f"Ваше предложение #{sid} опубликовано на стене группы! Спасибо!",
                random_id=0,
            )
        except Exception:
            pass
        return f"Предложение #{sid} опубликовано!"
    except Exception as e:
        return f"Ошибка публикации: {e}"


async def cmd_reject(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /отклонить <id> [причина]"
    try:
        sub = parts[1].split(maxsplit=1)
        sid = int(sub[0].strip())
        reason = sub[1] if len(sub) > 1 else (parts[2] if len(parts) > 2 else "")
    except ValueError:
        return "Укажите числовой ID предложения."
    suggestion = await get_suggestion(sid)
    if not suggestion or suggestion.group_id != ctx.group_id:
        return "Предложение не найдено."

    await review_suggestion(sid, "rejected", from_id, reason)
    try:
        msg = f"Ваше предложение #{sid} отклонено."
        if reason:
            msg += f" Причина: {reason}"
        await ctx.api.messages.send(
            user_id=suggestion.from_vk_id, message=msg, random_id=0,
        )
    except Exception:
        pass
    return f"Предложение #{sid} отклонено."


async def cmd_stats(ctx, from_id, parts, peer_id) -> str:
    try:
        members_resp = await ctx.api.groups.get_members(group_id=ctx.group_id, count=0)
        member_count = members_resp.count if members_resp else 0
    except Exception:
        member_count = "?"

    pending = await get_pending_suggestions(ctx.group_id, limit=100)
    analytics = await get_post_analytics(ctx.group_id, limit=10)

    total_likes = sum(p.likes for p in analytics)
    total_views = sum(p.views for p in analytics)

    return (
        f"Статистика группы:\n\n"
        f"Участников: {member_count}\n"
        f"Предложений на модерации: {len(pending)}\n\n"
        f"Последние 10 постов:\n"
        f"Лайков: {total_likes}\n"
        f"Просмотров: {total_views}"
    )


async def cmd_analytics(ctx, from_id, parts, peer_id) -> str:
    posts = await get_post_analytics(ctx.group_id, limit=10)
    if not posts:
        return "Нет данных аналитики. Данные собираются автоматически каждые 6 часов."
    lines = ["Аналитика последних постов:\n"]
    for p in posts:
        date = p.published_at.strftime("%d.%m") if p.published_at else "?"
        lines.append(
            f"Пост #{p.vk_post_id} ({date}): "
            f"👍{p.likes} 🔁{p.reposts} 💬{p.comments} 👁{p.views}"
        )
    return "\n".join(lines)


async def cmd_top(ctx, from_id, parts, peer_id) -> str:
    order = parts[1].strip().lower() if len(parts) > 1 else "xp"
    labels = {"xp": "опыту", "rep": "репутации", "messages": "сообщениям", "level": "уровню"}
    label = labels.get(order, "опыту")
    users = await get_top_users(ctx.group_id, order_by=order)
    if not users:
        return "Пока нет данных о пользователях."
    lines = [f"Топ участников по {label}:\n"]
    for i, u in enumerate(users, 1):
        val = getattr(u, order if order != "rep" else "reputation", u.xp)
        lines.append(f"{i}. vk.com/id{u.vk_id} — {val}")
    return "\n".join(lines)


async def cmd_ban(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /бан <vk_id> [причина]"
    try:
        sub = parts[1].split(maxsplit=1)
        uid = int(sub[0].strip())
        reason = sub[1] if len(sub) > 1 else (parts[2] if len(parts) > 2 else "Нарушение правил")
    except ValueError:
        return "Укажите числовой VK ID."
    try:
        await ctx.api.groups.ban(
            group_id=ctx.group_id, owner_id=uid,
            reason=0, comment=reason, comment_visible=1,
        )
        await create_ban_record(ctx.group_id, uid, from_id, reason)
        return f"Пользователь {uid} забанен. Причина: {reason}"
    except Exception as e:
        return f"Ошибка бана: {e}"


async def cmd_unban(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /разбан <vk_id>"
    try:
        uid = int(parts[1].strip())
    except ValueError:
        return "Укажите числовой VK ID."
    try:
        await ctx.api.groups.unban(group_id=ctx.group_id, owner_id=uid)
        await remove_ban_record(ctx.group_id, uid)
        return f"Пользователь {uid} разбанен."
    except Exception as e:
        return f"Ошибка разбана: {e}"


async def cmd_bans(ctx, from_id, parts, peer_id) -> str:
    records = await get_ban_history(ctx.group_id, limit=20)
    if not records:
        return "История банов пуста."
    lines = ["Последние баны:\n"]
    for r in records:
        status = "🔴 Бан" if not r.unbanned_at else "🟢 Разбан"
        date = r.banned_at.strftime("%d.%m.%Y")
        lines.append(f"{status} vk.com/id{r.vk_id} ({date}) — {r.reason or 'без причины'}")
    return "\n".join(lines)


async def cmd_pin(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /закреп <post_id>"
    try:
        post_id = int(parts[1].strip())
    except ValueError:
        return "Укажите числовой ID поста."
    try:
        await ctx.api.wall.pin(owner_id=-ctx.group_id, post_id=post_id)
        return f"Пост {post_id} закреплён."
    except Exception as e:
        return f"Ошибка закрепления: {e}"


async def cmd_source(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /источник добавить rss <url> | /источник список | /источник удалить <id>"

    sub_parts = parts[1].strip().split(maxsplit=2) if len(parts) > 1 else []
    sub_cmd = sub_parts[0].lower() if sub_parts else ""

    if sub_cmd == "добавить":
        if len(sub_parts) < 3:
            return "Формат: /источник добавить rss <url>"
        stype = sub_parts[1].lower()
        url = sub_parts[2].strip()
        if stype not in ("rss", "vk_group"):
            return "Поддерживаемые типы: rss, vk_group"
        src = await add_content_source(ctx.group_id, stype, url)
        return f"Источник #{src.id} добавлен: {stype} — {url}"

    elif sub_cmd == "список":
        sources = await get_content_sources(ctx.group_id)
        if not sources:
            return "Нет активных источников контента."
        lines = ["Источники контента:\n"]
        for s in sources:
            fetched = s.last_fetched_at.strftime("%d.%m %H:%M") if s.last_fetched_at else "никогда"
            lines.append(f"#{s.id} [{s.source_type}] {s.source_url} (последний парсинг: {fetched})")
        return "\n".join(lines)

    elif sub_cmd == "удалить":
        if len(sub_parts) < 2:
            return "Формат: /источник удалить <id>"
        try:
            sid = int(sub_parts[1].strip())
        except ValueError:
            return "Укажите числовой ID источника."
        ok = await delete_content_source(sid)
        return f"Источник #{sid} удалён." if ok else "Источник не найден."

    return "Формат: /источник добавить rss <url> | /источник список | /источник удалить <id>"


async def cmd_newsletter(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /рассылка текст сообщения"
    text = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
    if len(parts) > 2:
        text = parts[1] + " " + parts[2]
    text = text.strip()
    if not text:
        return "Укажите текст рассылки."

    # Get member count
    try:
        members_resp = await ctx.api.groups.get_members(group_id=ctx.group_id, count=0)
        total = members_resp.count if members_resp else 0
    except Exception as e:
        return f"Не удалось получить участников: {e}"

    if total == 0:
        return "В группе нет участников."

    nl = await create_newsletter(ctx.group_id, text, from_id, total)

    # Launch in background
    asyncio.create_task(_send_newsletter(ctx, nl.id, text, total))
    return f"Рассылка #{nl.id} запущена для {total} участников. Это займёт некоторое время."


async def _send_newsletter(ctx: GroupContext, newsletter_id: int, text: str, total: int):
    """Background task: send newsletter to all group members."""
    sent = 0
    offset = 0
    batch_size = 200

    try:
        while offset < total:
            members_resp = await ctx.api.groups.get_members(
                group_id=ctx.group_id, offset=offset, count=batch_size,
            )
            if not members_resp or not members_resp.items:
                break

            for uid in members_resp.items:
                try:
                    await ctx.api.messages.send(
                        user_id=uid, message=text, random_id=0,
                    )
                    sent += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)  # rate limit ~20 req/s

            offset += batch_size
            await update_newsletter_progress(newsletter_id, sent)

        await update_newsletter_progress(newsletter_id, sent, status="sent")

        # Notify admin
        try:
            await ctx.api.messages.send(
                user_id=ctx.admin_vk_id,
                message=f"Рассылка #{newsletter_id} завершена. Отправлено: {sent}/{total}",
                random_id=0,
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Newsletter #{newsletter_id} failed: {e}")
        await update_newsletter_progress(newsletter_id, sent, status="failed")


async def cmd_setting(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 3:
        return "Формат: /настройка <ключ> <значение>"
    key = parts[1].strip()
    value = parts[2].strip()
    await set_setting(ctx.group_id, key, value)
    return f"Настройка обновлена:\n{key} = {value}"


async def cmd_view(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /посмотреть <ключ>"
    key = parts[1].strip()
    value = await get_setting(ctx.group_id, key, default="(не задано)")
    return f"{key} = {value}"


async def cmd_clear(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 2:
        return "Формат: /очистить <vk_id>"
    try:
        uid = int(parts[1].strip())
        await clear_user_history(ctx.group_id, uid)
        return f"Память диалога с пользователем {uid} очищена."
    except ValueError:
        return "Укажите корректный числовой VK ID."


async def cmd_vip(ctx, from_id, parts, peer_id) -> str:
    if len(parts) < 3:
        return "Формат: /vip <vk_id> <days>"
    try:
        sub_parts = parts[1].split()
        uid = int(sub_parts[0].strip())
        d = int(parts[2].strip()) if len(parts) > 2 else int(sub_parts[1].strip())
        await grant_vip(ctx.group_id, uid, d)
        return f"Пользователю {uid} выдан VIP на {d} дней."
    except (ValueError, IndexError):
        return "Ошибка формата. Укажите: /vip <vk_id> <days>"


# ─── Command dispatch table ──────────────────────────────────────────────────

COMMANDS = {
    # Content
    "/помощь": cmd_help,
    "/пост": cmd_post,
    "/запланировать": cmd_schedule,
    "/контентплан": cmd_content_plan,
    "/предложка": cmd_suggestions,
    "/принять": cmd_accept,
    "/отклонить": cmd_reject,
    # Analytics
    "/стата": cmd_stats,
    "/аналитика": cmd_analytics,
    "/топ": cmd_top,
    # Moderation
    "/бан": cmd_ban,
    "/разбан": cmd_unban,
    "/баны": cmd_bans,
    "/закреп": cmd_pin,
    # Content sources
    "/источник": cmd_source,
    # Newsletter
    "/рассылка": cmd_newsletter,
    # Settings
    "/настройка": cmd_setting,
    "/посмотреть": cmd_view,
    "/очистить": cmd_clear,
    "/vip": cmd_vip,
}
