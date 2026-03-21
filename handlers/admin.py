"""Admin command handler — full-featured group administration."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from core.ai_brain import generate_post
from core.group_context import GroupContext
from core.telegram import send_to_telegram
from database.service import (
    get_setting, set_setting, clear_user_history, grant_vip,
    get_pending_suggestions, get_suggestion, review_suggestion,
    create_scheduled_post, get_content_plan,
    add_content_source, get_content_sources, delete_content_source,
    get_post_analytics, get_top_users,
    create_ban_record, remove_ban_record, get_ban_history,
    create_newsletter, update_newsletter_progress,
    create_content_task, get_content_tasks, delete_content_task,
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
        "/пост [тема] — короткий пост (3-5 предложений)\n"
        "/статья <url|тема> [инструкция] — полноценная статья\n"
        "/черновик <url|тема> — статья без публикации\n"
        "/патчнот <github_url> [дней] — патч-ноты из GitHub\n"
        "/запланировать ЧЧ:ММ текст — запланировать пост\n"
        "/контентплан — посты на сегодня\n"
        "/предложка — предложенные посты\n"
        "/принять <id> — опубликовать предложение\n"
        "/отклонить <id> [причина] — отклонить\n\n"
        "📋 Контент-задачи:\n"
        "/задача список — автоматические задачи\n"
        '/задача добавить <тип> "<cron>" <url>\n'
        "/задача удалить <id>\n\n"
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
        "🏆 Виджет-лидерборд:\n"
        "/виджет — обновить виджет топ-участников\n\n"
        "🤖 ИИ-настройки:\n"
        "/обновить — пересканировать группу и обновить ИИ\n"
        "/аинфо — текущие ИИ-настройки группы\n\n"
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
        post_kwargs = {"owner_id": -ctx.group_id, "message": post_text}
        # Attach a thematic image
        try:
            from core.images import find_and_upload_image
            attachment = await find_and_upload_image(ctx.api, ctx.group_id, post_text=post_text)
            if attachment:
                post_kwargs["attachments"] = attachment
        except Exception:
            pass
        result = await ctx.api.wall.post(**post_kwargs)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, post_text, vk_post_id)
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
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, suggestion.text, vk_post_id)
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
        if stype not in ("rss", "vk_group", "api", "web"):
            return "Поддерживаемые типы: rss, vk_group, api, web"
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
    # /рассылка текст — всё после команды это текст
    raw = parts[1] if len(parts) > 1 else ""
    if len(parts) > 2:
        raw = raw + " " + parts[2]
    text = raw.strip()
    if not text:
        return "Формат: /рассылка текст сообщения"

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


async def cmd_article(ctx, from_id, parts, peer_id) -> str:
    """Write an article from URL and publish to wall."""
    if len(parts) < 2:
        return (
            "Формат:\n"
            "/статья https://example.com [инструкция]\n\n"
            "Примеры:\n"
            "/статья https://github.com/user/repo напиши патч-ноты\n"
            "/статья https://news.site/article расскажи подробно\n"
            "/статья https://wowhead.com/guide обзор для новичков"
        )

    from core.content_writer import write_from_url

    raw = parts[1]
    if len(parts) > 2:
        raw = raw + " " + parts[2]
    raw = raw.strip()

    if not raw.startswith("http"):
        return "Укажите ссылку на источник. Бот читает страницу и пишет статью на её основе."

    url_parts = raw.split(maxsplit=1)
    url = url_parts[0]
    instruction = url_parts[1] if len(url_parts) > 1 else ""

    text = await write_from_url(
        group_id=ctx.group_id,
        url=url,
        instruction=instruction,
    )

    if text.startswith("Не удалось") or text.startswith("Ошибка"):
        return text

    try:
        post_kwargs = {"owner_id": -ctx.group_id, "message": text}
        # Attach a thematic image
        try:
            from core.images import find_and_upload_image
            attachment = await find_and_upload_image(ctx.api, ctx.group_id, post_text=text)
            if attachment:
                post_kwargs["attachments"] = attachment
        except Exception:
            pass
        result = await ctx.api.wall.post(**post_kwargs)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, text, vk_post_id)
        return f"Статья опубликована!\n\n{text[:500]}..."
    except Exception as e:
        logger.error(f"Failed to publish article: {e}")
        return f"Статья готова, но ошибка публикации: {e}\n\nТекст:\n{text[:1000]}"


async def cmd_draft(ctx, from_id, parts, peer_id) -> str:
    """Write an article but don't publish — just show it."""
    if len(parts) < 2:
        return "Формат: /черновик https://url [инструкция]"

    from core.content_writer import write_from_url

    raw = parts[1]
    if len(parts) > 2:
        raw = raw + " " + parts[2]
    raw = raw.strip()

    if not raw.startswith("http"):
        return "Укажите ссылку на источник."

    url_parts = raw.split(maxsplit=1)
    url = url_parts[0]
    instruction = url_parts[1] if len(url_parts) > 1 else ""

    text = await write_from_url(
        group_id=ctx.group_id,
        url=url,
        instruction=instruction,
    )

    return f"Черновик (не опубликован):\n\n{text}"


async def cmd_patch_notes(ctx, from_id, parts, peer_id) -> str:
    """Generate patch notes from GitHub repo."""
    if len(parts) < 2:
        return "Формат: /патчнот https://github.com/owner/repo [дней]"

    from core.content_writer import write_patch_notes

    raw = parts[1]
    if len(parts) > 2:
        raw = raw + " " + parts[2]

    sub = raw.strip().split(maxsplit=1)
    url = sub[0]
    days = 7
    if len(sub) > 1:
        try:
            days = int(sub[1])
        except ValueError:
            pass

    if not url.startswith("http"):
        url = f"https://github.com/{url}"

    text = await write_patch_notes(
        group_id=ctx.group_id,
        github_url=url,
        days=days,
    )

    if text.startswith("Ошибка") or text.startswith("Нет коммитов") or text.startswith("Неверная"):
        return text

    try:
        # Try to attach a relevant image
        post_kwargs = {"owner_id": -ctx.group_id, "message": text}
        try:
            from core.images import find_and_upload_image
            attachment = await find_and_upload_image(ctx.api, ctx.group_id, query="software update", post_text=text)
            if attachment:
                post_kwargs["attachments"] = attachment
        except Exception:
            pass

        result = await ctx.api.wall.post(**post_kwargs)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, text, vk_post_id)
        return f"Патч-ноты опубликованы!\n\n{text[:500]}..."
    except Exception as e:
        return f"Ошибка публикации: {e}\n\nТекст:\n{text[:1000]}"


async def cmd_content_task(ctx, from_id, parts, peer_id) -> str:
    """Manage recurring content tasks."""
    if len(parts) < 2:
        return (
            "Формат:\n"
            "/задача список — показать задачи\n"
            "/задача добавить <тип> <cron> <url> [инструкция]\n"
            "/задача удалить <id>\n\n"
            "Типы: patch_notes, article, digest\n\n"
            "Примеры:\n"
            '/задача добавить patch_notes "0 18 * * 5" https://github.com/user/repo\n'
            "  → Каждую пятницу в 18:00 — патч-ноты\n"
            '/задача добавить article "0 10 * * 1" https://news.site/feed дайджест новостей\n'
            "  → Каждый понедельник в 10:00 — статья по ссылке"
        )

    raw = parts[1]
    if len(parts) > 2:
        raw = raw + " " + parts[2]
    sub = raw.strip().split(maxsplit=1)
    sub_cmd = sub[0].lower()

    if sub_cmd == "список":
        tasks = await get_content_tasks(ctx.group_id)
        if not tasks:
            return "Нет активных контент-задач. Добавьте через /задача добавить"
        lines = ["Контент-задачи:\n"]
        for t in tasks:
            last = t.last_run_at.strftime("%d.%m %H:%M") if t.last_run_at else "никогда"
            lines.append(
                f"#{t.id} [{t.task_type}] {t.name}\n"
                f"  Расписание: {t.schedule_cron}\n"
                f"  Источник: {t.source_url or '—'}\n"
                f"  Последний запуск: {last}"
            )
        return "\n".join(lines)

    elif sub_cmd == "удалить":
        if len(sub) < 2:
            return "Формат: /задача удалить <id>"
        try:
            tid = int(sub[1].strip())
        except ValueError:
            return "Укажите числовой ID задачи."
        ok = await delete_content_task(tid)
        return f"Задача #{tid} удалена." if ok else "Задача не найдена."

    elif sub_cmd == "добавить":
        if len(sub) < 2:
            return 'Формат: /задача добавить <тип> "<cron>" <url> [инструкция]'

        # Parse: type "cron" url [instruction]
        import re
        rest = sub[1].strip()

        # Extract type
        type_parts = rest.split(maxsplit=1)
        task_type = type_parts[0]
        if task_type not in ("patch_notes", "article", "digest"):
            return "Тип должен быть: patch_notes, article или digest"

        rest = type_parts[1] if len(type_parts) > 1 else ""

        # Extract cron (in quotes)
        cron_match = re.match(r'"([^"]+)"\s*(.*)', rest)
        if not cron_match:
            return 'Укажите расписание в кавычках: "0 18 * * 5" (cron формат)'

        cron_expr = cron_match.group(1)
        rest = cron_match.group(2).strip()

        # Validate cron
        try:
            from croniter import croniter
            croniter(cron_expr)
        except Exception:
            return f"Неверный cron: {cron_expr}. Пример: 0 18 * * 5 (пятница 18:00)"

        # Extract URL and instruction
        url_parts = rest.split(maxsplit=1)
        source_url = url_parts[0] if url_parts else ""
        instruction = url_parts[1] if len(url_parts) > 1 else ""

        # Generate name
        name = f"{task_type}_{source_url.split('/')[-1] if source_url else 'manual'}"

        task = await create_content_task(
            group_id=ctx.group_id,
            name=name,
            task_type=task_type,
            schedule_cron=cron_expr,
            source_url=source_url,
            instruction=instruction,
        )
        return (
            f"Задача #{task.id} создана!\n"
            f"Тип: {task_type}\n"
            f"Расписание: {cron_expr}\n"
            f"Источник: {source_url or '—'}\n"
            f"Инструкция: {instruction or '—'}"
        )

    return "Неизвестная подкоманда. Используйте: /задача список | добавить | удалить"


async def cmd_refresh(ctx, from_id, parts, peer_id) -> str:
    """Re-scan group and regenerate AI settings."""
    from core.crypto import decrypt_token
    from database.service import get_group

    group = await get_group(ctx.group_id)
    if not group:
        return "Ошибка: группа не найдена."

    try:
        token = decrypt_token(group.access_token)
    except Exception:
        return "Ошибка: не удалось расшифровать токен группы."

    from core.group_setup import setup_group_ai
    ok = await setup_group_ai(ctx.group_id, token)
    if ok:
        return (
            "ИИ-настройки группы обновлены!\n"
            "Бот пересканировал группу и адаптировал свою личность, "
            "правила модерации и темы контента."
        )
    return "Не удалось обновить настройки. Проверьте логи."


async def cmd_ai_info(ctx, from_id, parts, peer_id) -> str:
    """Show current AI settings for the group."""
    from core.ai_brain import _get_group_ai_context

    ai_ctx = await _get_group_ai_context(ctx.group_id)

    if not ai_ctx["ai_system_prompt"]:
        return (
            "ИИ-настройки не сгенерированы. "
            "Выполните /обновить для автоматической настройки."
        )

    lines = ["Текущие ИИ-настройки группы:\n"]
    lines.append(f"Описание: {ai_ctx['ai_group_description']}")
    lines.append(f"Стиль: {ai_ctx['ai_tone']}")
    lines.append(f"\nСистемный промпт:\n{ai_ctx['ai_system_prompt']}")
    lines.append(f"\nПравила модерации:\n{ai_ctx['ai_moderation_rules']}")
    lines.append(f"\nТемы контента:\n{ai_ctx['ai_content_topics']}")
    return "\n".join(lines)


async def cmd_widget(ctx, from_id, parts, peer_id) -> str:
    """Manually refresh the leaderboard widget."""
    from core.widgets import update_widget_for_group

    widget_enabled = (await get_setting(ctx.group_id, "widget_enabled", "false")).lower()
    if widget_enabled != "true":
        return (
            "Виджет отключён. Включите его в настройках:\n"
            "/настройка widget_enabled true"
        )

    success, message = await update_widget_for_group(ctx.group_id)
    if success:
        return f"Виджет топ-участников обновлён! {message}"
    return f"Не удалось обновить виджет: {message}"


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
    # Content writer
    "/статья": cmd_article,
    "/черновик": cmd_draft,
    "/патчнот": cmd_patch_notes,
    "/задача": cmd_content_task,
    # AI setup
    "/обновить": cmd_refresh,
    "/аинфо": cmd_ai_info,
    # Widget
    "/виджет": cmd_widget,
}
