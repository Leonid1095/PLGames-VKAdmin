"""AI Agent — autonomous admin that understands natural language and calls tools.

Replaces command-based handlers with a single LLM entry point.
Admin writes in natural language, agent decides what to do.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from openai import AsyncOpenAI
from core.config import settings
from core.group_context import GroupContext

logger = logging.getLogger(__name__)

# ─── Tool definitions for the LLM ───────────────────────────────────────────

ADMIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "publish_post",
            "description": "Write and publish a post to the group wall. Use when admin asks to write/publish/post something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic or full text for the post"
                    },
                    "with_image": {
                        "type": "boolean",
                        "description": "Attach a thematic image",
                        "default": True
                    }
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_article",
            "description": "Fetch a URL and write an article based on it. Use when admin gives a link and wants content from it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Source URL to read and rewrite"},
                    "instruction": {"type": "string", "description": "Additional instructions (tone, focus, length)", "default": ""}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_post",
            "description": "Schedule a post for later. Use when admin says 'schedule', 'post at X time', 'publish tomorrow', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Post text"},
                    "time": {"type": "string", "description": "Time in HH:MM format (UTC)"},
                    "generate": {
                        "type": "boolean",
                        "description": "If true, generate post from topic instead of using text literally",
                        "default": False
                    }
                },
                "required": ["text", "time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ban_user",
            "description": "Ban a user from the group. Use when admin says to ban someone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "VK user ID to ban"},
                    "reason": {"type": "string", "description": "Ban reason", "default": "Нарушение правил"}
                },
                "required": ["user_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "unban_user",
            "description": "Unban a user. Use when admin says to unban someone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "VK user ID to unban"}
                },
                "required": ["user_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "Get group statistics: members, posts, engagement. Use when admin asks about stats, analytics, how things are going.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_content_plan",
            "description": "Show today's scheduled posts. Use when admin asks about content plan, schedule, what's planned.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_suggestions",
            "description": "Show pending user-suggested posts. Use when admin asks about suggestions, предложка.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "review_suggestion",
            "description": "Accept or reject a user suggestion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "suggestion_id": {"type": "integer", "description": "Suggestion ID"},
                    "action": {"type": "string", "enum": ["accept", "reject"]},
                    "reason": {"type": "string", "description": "Rejection reason (if rejecting)", "default": ""}
                },
                "required": ["suggestion_id", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_content_source",
            "description": "Add an RSS feed, VK group, or website as a content source for auto-posting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Source URL (RSS feed, VK group, website)"},
                    "source_type": {
                        "type": "string",
                        "enum": ["rss", "vk_group", "web"],
                        "description": "Type of source. Agent should detect this from URL.",
                        "default": "rss"
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_content_sources",
            "description": "List all content sources (RSS, VK groups, websites).",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_content_source",
            "description": "Remove a content source by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "integer", "description": "Source ID to remove"}
                },
                "required": ["source_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_newsletter",
            "description": "Send a message to all group members. Use when admin wants to broadcast/notify everyone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message text to send"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "change_setting",
            "description": "Change a bot setting. Use for moderation level, autoposting, welcome messages etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Setting key"},
                    "value": {"type": "string", "description": "New value"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_users",
            "description": "Show leaderboard of top users by XP, reputation or messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_by": {
                        "type": "string",
                        "enum": ["xp", "reputation", "messages", "level"],
                        "default": "xp"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_gamification",
            "description": "Enable or disable gamification (XP, levels, reputation). Use when admin mentions gamification, XP, levels, reputation, leaderboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean", "description": "true to enable, false to disable"}
                },
                "required": ["enabled"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_patch_notes",
            "description": "Generate and publish patch notes from a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "github_url": {"type": "string", "description": "GitHub repository URL"},
                    "days": {"type": "integer", "description": "Days of history to include", "default": 7}
                },
                "required": ["github_url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_ai",
            "description": "Re-scan the group and update AI personality/settings. Use when admin says to reconfigure, re-learn, or update AI.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pin_post",
            "description": "Pin a post on the group wall.",
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "integer", "description": "VK post ID to pin"}
                },
                "required": ["post_id"]
            }
        }
    },
]

# Subset of tools available to regular users
USER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_profile",
            "description": "Show user's profile: level, XP, reputation, stats. Use when user asks about their profile, stats, level.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_post",
            "description": "Submit a post suggestion for admin review. Use when user wants to suggest/propose content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Post text to suggest"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "horoscope",
            "description": "Generate a fun horoscope. Use when user asks for horoscope.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "who_am_i",
            "description": "AI personality analysis based on chat history. Use when user asks 'who am I', 'analyze me'.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]


# ─── Tool executors ──────────────────────────────────────────────────────────

async def _exec_publish_post(ctx: GroupContext, args: dict) -> str:
    from core.ai_brain import generate_post
    from core.telegram import send_to_telegram

    topic = args.get("topic", "").strip()
    post_text = await generate_post(group_id=ctx.group_id, topic=topic)

    if not post_text or len(post_text.strip()) < 50 or post_text.startswith("Извините"):
        return (
            "Не удалось сгенерировать пост — нет ни темы, ни описания группы. "
            "Напишите о чём писать (например: «напиши пост про новое обновление»)."
        )

    post_kwargs = {"owner_id": -ctx.group_id, "message": post_text}
    if args.get("with_image", True):
        try:
            from core.images import find_and_upload_image
            attachment = await find_and_upload_image(ctx.api, ctx.group_id, post_text=post_text)
            if attachment:
                post_kwargs["attachments"] = attachment
        except Exception:
            pass

    try:
        result = await ctx.api.wall.post(**post_kwargs)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, post_text, vk_post_id)
        return f"Пост опубликован (ID: {vk_post_id}).\n\nТекст:\n{post_text[:300]}..."
    except Exception as e:
        return f"Ошибка публикации: {e}"


async def _exec_write_article(ctx: GroupContext, args: dict) -> str:
    from core.content_writer import write_from_url
    from core.telegram import send_to_telegram

    url = args["url"]
    instruction = args.get("instruction", "")
    text = await write_from_url(group_id=ctx.group_id, url=url, instruction=instruction)

    if text.startswith("Не удалось") or text.startswith("Ошибка"):
        return text

    post_kwargs = {"owner_id": -ctx.group_id, "message": text}
    try:
        from core.images import find_and_upload_image
        attachment = await find_and_upload_image(ctx.api, ctx.group_id, post_text=text)
        if attachment:
            post_kwargs["attachments"] = attachment
    except Exception:
        pass

    try:
        result = await ctx.api.wall.post(**post_kwargs)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, text, vk_post_id)
        return f"Статья опубликована (ID: {vk_post_id}).\n\n{text[:300]}..."
    except Exception as e:
        return f"Статья готова, но ошибка публикации: {e}\n\n{text[:500]}"


async def _exec_schedule_post(ctx: GroupContext, args: dict) -> str:
    from database.service import create_scheduled_post
    from core.ai_brain import generate_post

    text = args["text"]
    time_str = args["time"]

    if args.get("generate"):
        text = await generate_post(group_id=ctx.group_id, topic=text)

    try:
        h, m = map(int, time_str.split(":"))
        now = datetime.now(timezone.utc)
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if scheduled <= now:
            scheduled += timedelta(days=1)
    except ValueError:
        return "Не удалось разобрать время. Формат: ЧЧ:ММ"

    post = await create_scheduled_post(ctx.group_id, text, scheduled, source="agent")
    return f"Пост запланирован на {scheduled.strftime('%d.%m %H:%M')} UTC (ID: {post.id})"


async def _exec_ban_user(ctx: GroupContext, args: dict) -> str:
    from database.service import create_ban_record

    uid = args["user_id"]
    reason = args.get("reason", "Нарушение правил")
    try:
        await ctx.api.groups.ban(
            group_id=ctx.group_id, owner_id=uid,
            reason=0, comment=reason, comment_visible=1,
        )
        await create_ban_record(ctx.group_id, uid, ctx.admin_vk_id, reason)
        return f"Пользователь {uid} забанен. Причина: {reason}"
    except Exception as e:
        return f"Ошибка бана: {e}"


async def _exec_unban_user(ctx: GroupContext, args: dict) -> str:
    from database.service import remove_ban_record

    uid = args["user_id"]
    try:
        await ctx.api.groups.unban(group_id=ctx.group_id, owner_id=uid)
        await remove_ban_record(ctx.group_id, uid)
        return f"Пользователь {uid} разбанен."
    except Exception as e:
        return f"Ошибка разбана: {e}"


async def _exec_get_stats(ctx: GroupContext, args: dict) -> str:
    from database.service import get_pending_suggestions, get_post_analytics

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
        f"Участников: {member_count}\n"
        f"Предложений на модерации: {len(pending)}\n"
        f"Последние 10 постов: {total_likes} лайков, {total_views} просмотров"
    )


async def _exec_get_content_plan(ctx: GroupContext, args: dict) -> str:
    from database.service import get_content_plan

    now = datetime.now(timezone.utc)
    posts = await get_content_plan(ctx.group_id, now)
    if not posts:
        return "На сегодня нет запланированных постов."
    lines = []
    for p in posts:
        time_str = p.scheduled_at.strftime("%H:%M")
        status_icon = {"pending": "ожидает", "published": "опубликован", "failed": "ошибка"}.get(p.status, "?")
        lines.append(f"{time_str} — {p.text[:60]}... [{status_icon}]")
    return "\n".join(lines)


async def _exec_get_suggestions(ctx: GroupContext, args: dict) -> str:
    from database.service import get_pending_suggestions

    posts = await get_pending_suggestions(ctx.group_id)
    if not posts:
        return "Нет предложенных постов."
    lines = []
    for p in posts:
        lines.append(f"#{p.id} от vk.com/id{p.from_vk_id}: {p.text[:100]}...")
    return "\n".join(lines)


async def _exec_review_suggestion(ctx: GroupContext, args: dict) -> str:
    from database.service import get_suggestion, review_suggestion
    from core.telegram import send_to_telegram

    sid = args["suggestion_id"]
    action = args["action"]
    reason = args.get("reason", "")

    suggestion = await get_suggestion(sid)
    if not suggestion or suggestion.group_id != ctx.group_id:
        return "Предложение не найдено."
    if suggestion.status != "pending":
        return f"Предложение уже обработано ({suggestion.status})."

    if action == "accept":
        await review_suggestion(sid, "approved", ctx.admin_vk_id)
        try:
            result = await ctx.api.wall.post(owner_id=-ctx.group_id, message=suggestion.text)
            vk_post_id = result.post_id if result else 0
            await send_to_telegram(ctx.group_id, suggestion.text, vk_post_id)
            await review_suggestion(sid, "published", ctx.admin_vk_id)
            try:
                await ctx.api.messages.send(
                    user_id=suggestion.from_vk_id,
                    message=f"Ваше предложение #{sid} опубликовано!",
                    random_id=0,
                )
            except Exception:
                pass
            return f"Предложение #{sid} опубликовано."
        except Exception as e:
            return f"Ошибка публикации: {e}"
    else:
        await review_suggestion(sid, "rejected", ctx.admin_vk_id, reason)
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


async def _exec_add_content_source(ctx: GroupContext, args: dict) -> str:
    from database.service import add_content_source

    url = args["url"]
    stype = args.get("source_type", "rss")
    src = await add_content_source(ctx.group_id, stype, url)
    return f"Источник добавлен (ID: {src.id}): {stype} — {url}"


async def _exec_list_content_sources(ctx: GroupContext, args: dict) -> str:
    from database.service import get_content_sources

    sources = await get_content_sources(ctx.group_id)
    if not sources:
        return "Нет активных источников контента."
    lines = []
    for s in sources:
        fetched = s.last_fetched_at.strftime("%d.%m %H:%M") if s.last_fetched_at else "никогда"
        lines.append(f"#{s.id} [{s.source_type}] {s.source_url} (парсинг: {fetched})")
    return "\n".join(lines)


async def _exec_remove_content_source(ctx: GroupContext, args: dict) -> str:
    from database.service import delete_content_source

    sid = args["source_id"]
    ok = await delete_content_source(sid)
    return f"Источник #{sid} удалён." if ok else "Источник не найден."


async def _exec_send_newsletter(ctx: GroupContext, args: dict) -> str:
    import asyncio
    from database.service import (
        create_newsletter, update_newsletter_progress,
        get_setting, set_setting,
    )

    text = args["text"]
    if not text or len(text.strip()) < 10:
        return "Текст рассылки слишком короткий — рассылка не запущена."

    # Daily cap: 1 newsletter / 24h to prevent runaway LLM or accidental triggers
    last_sent_str = await get_setting(ctx.group_id, "_last_newsletter_at", "")
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            hours_since = (datetime.now(timezone.utc) - last_sent).total_seconds() / 3600
            if hours_since < 24:
                remaining = 24 - hours_since
                return (
                    f"Рассылка уже отправлялась сегодня (лимит 1 раз в 24ч). "
                    f"Следующая будет доступна через {remaining:.1f} ч."
                )
        except ValueError:
            pass

    try:
        members_resp = await ctx.api.groups.get_members(group_id=ctx.group_id, count=0)
        total = members_resp.count if members_resp else 0
    except Exception as e:
        return f"Не удалось получить участников: {e}"

    if total == 0:
        return "В группе нет участников."

    nl = await create_newsletter(ctx.group_id, text, ctx.admin_vk_id, total)
    await set_setting(ctx.group_id, "_last_newsletter_at", datetime.now(timezone.utc).isoformat())

    async def _send():
        sent = 0
        offset = 0
        try:
            while offset < total:
                members_resp = await ctx.api.groups.get_members(
                    group_id=ctx.group_id, offset=offset, count=200,
                )
                if not members_resp or not members_resp.items:
                    break
                for uid in members_resp.items:
                    try:
                        await ctx.api.messages.send(user_id=uid, message=text, random_id=0)
                        sent += 1
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
                offset += 200
                await update_newsletter_progress(nl.id, sent)
            await update_newsletter_progress(nl.id, sent, status="sent")
        except Exception as e:
            logger.error(f"Newsletter #{nl.id} failed: {e}")
            await update_newsletter_progress(nl.id, sent, status="failed")

    asyncio.create_task(_send())
    return f"Рассылка запущена для {total} участников."


async def _exec_change_setting(ctx: GroupContext, args: dict) -> str:
    from database.service import set_setting

    key = args["key"]
    value = args["value"]
    await set_setting(ctx.group_id, key, value)
    return f"Настройка обновлена: {key} = {value}"


async def _exec_get_top_users(ctx: GroupContext, args: dict) -> str:
    from database.service import get_top_users

    order = args.get("order_by", "xp")
    labels = {"xp": "опыту", "reputation": "репутации", "messages": "сообщениям", "level": "уровню"}
    label = labels.get(order, "опыту")
    users = await get_top_users(ctx.group_id, order_by=order, limit=20)
    if not users:
        return "Пока нет данных."
    lines = [f"Топ по {label}:"]
    _attr = {"messages": "messages_count"}.get(order, order)
    for i, u in enumerate(users[:20], 1):
        val = getattr(u, _attr, u.xp)
        lines.append(f"{i}. vk.com/id{u.vk_id} — {val}")
    return "\n".join(lines)


async def _exec_generate_patch_notes(ctx: GroupContext, args: dict) -> str:
    from core.content_writer import write_patch_notes
    from core.telegram import send_to_telegram

    url = args["github_url"]
    days = args.get("days", 7)
    if not url.startswith("http"):
        url = f"https://github.com/{url}"

    text = await write_patch_notes(group_id=ctx.group_id, github_url=url, days=days)
    if text.startswith("Ошибка") or text.startswith("Нет коммитов") or text.startswith("Неверная"):
        return text

    post_kwargs = {"owner_id": -ctx.group_id, "message": text}
    try:
        from core.images import find_and_upload_image
        attachment = await find_and_upload_image(ctx.api, ctx.group_id, query="software update", post_text=text)
        if attachment:
            post_kwargs["attachments"] = attachment
    except Exception:
        pass

    try:
        result = await ctx.api.wall.post(**post_kwargs)
        vk_post_id = result.post_id if result else 0
        await send_to_telegram(ctx.group_id, text, vk_post_id)
        return f"Патч-ноты опубликованы!\n\n{text[:300]}..."
    except Exception as e:
        return f"Ошибка публикации: {e}\n\n{text[:500]}"


async def _exec_refresh_ai(ctx: GroupContext, args: dict) -> str:
    from core.crypto import decrypt_token
    from database.service import get_group
    from core.group_setup import setup_group_ai

    group = await get_group(ctx.group_id)
    if not group:
        return "Группа не найдена."
    try:
        token = decrypt_token(group.access_token)
    except Exception:
        return "Ошибка расшифровки токена."

    ok = await setup_group_ai(ctx.group_id, token)
    return "ИИ-настройки обновлены! Бот адаптировался к группе." if ok else "Не удалось обновить."


async def _exec_toggle_gamification(ctx: GroupContext, args: dict) -> str:
    from database.service import set_setting

    enabled = args.get("enabled", False)
    await set_setting(ctx.group_id, "gamification_enabled", "true" if enabled else "false")
    if enabled:
        return "Геймификация включена! Участники будут получать XP за комментарии, лайки и репосты, повышать уровни и репутацию."
    return "Геймификация выключена."


async def _exec_pin_post(ctx: GroupContext, args: dict) -> str:
    post_id = args["post_id"]
    try:
        await ctx.api.wall.pin(owner_id=-ctx.group_id, post_id=post_id)
        return f"Пост {post_id} закреплён."
    except Exception as e:
        return f"Ошибка: {e}"


async def _exec_get_profile(ctx: GroupContext, args: dict, user_id: int = 0) -> str:
    from database.service import get_user_stats

    stats = await get_user_stats(ctx.group_id, user_id)
    vip_status = "Обычный"
    if stats.is_vip:
        expires = stats.vip_expires.strftime("%d.%m.%Y") if stats.vip_expires else "Навсегда"
        vip_status = f"VIP (до {expires})"
    requests_left = "Безлимит" if stats.is_vip else max(0, 10 - stats.daily_requests)

    return (
        f"Статус: {vip_status}\n"
        f"Уровень: {stats.level} | XP: {stats.xp}\n"
        f"Репутация: {stats.reputation}\n"
        f"Баланс: {stats.balance} коинов\n"
        f"ИИ-запросов сегодня: {requests_left}"
    )


async def _exec_suggest_post(ctx: GroupContext, args: dict, user_id: int = 0) -> str:
    from handlers.suggestions import handle_suggestion
    # peer_id == user_id because agent only runs in personal-message context
    return await handle_suggestion(ctx, user_id, args["text"], peer_id=user_id)


async def _exec_horoscope(ctx: GroupContext, args: dict) -> str:
    from core.ai_brain import generate_response
    return await generate_response(
        prompt="Напиши один короткий, смешной и абсурдный гороскоп на сегодня.",
        group_id=ctx.group_id,
    )


async def _exec_who_am_i(ctx: GroupContext, args: dict, user_id: int = 0) -> str:
    from core.ai_brain import generate_response
    from database.service import get_user_history

    history = await get_user_history(ctx.group_id, user_id)
    if not history:
        return "Мы ещё мало общались, чтобы понять кто ты!"

    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    if not user_msgs:
        return "Не нашёл твоих сообщений. Давай поболтаем!"

    context_text = "\n".join(user_msgs[-10:])
    return await generate_response(
        prompt=f"Мои сообщения:\n{context_text}\n\nОпиши, кто я?",
        system_prompt=(
            "Ты психолог-комик. Прочитай сообщения и сделай шуточный, "
            "ироничный, но не обидный вывод о характере (2-3 предложения)."
        ),
        group_id=ctx.group_id,
    )


# ─── Tool dispatcher ────────────────────────────────────────────────────────

TOOL_EXECUTORS = {
    # Admin tools
    "publish_post": _exec_publish_post,
    "write_article": _exec_write_article,
    "schedule_post": _exec_schedule_post,
    "ban_user": _exec_ban_user,
    "unban_user": _exec_unban_user,
    "get_stats": _exec_get_stats,
    "get_content_plan": _exec_get_content_plan,
    "get_suggestions": _exec_get_suggestions,
    "review_suggestion": _exec_review_suggestion,
    "add_content_source": _exec_add_content_source,
    "list_content_sources": _exec_list_content_sources,
    "remove_content_source": _exec_remove_content_source,
    "send_newsletter": _exec_send_newsletter,
    "change_setting": _exec_change_setting,
    "get_top_users": _exec_get_top_users,
    "toggle_gamification": _exec_toggle_gamification,
    "generate_patch_notes": _exec_generate_patch_notes,
    "refresh_ai": _exec_refresh_ai,
    "pin_post": _exec_pin_post,
    # User tools
    "get_profile": _exec_get_profile,
    "suggest_post": _exec_suggest_post,
    "horoscope": _exec_horoscope,
    "who_am_i": _exec_who_am_i,
}

# Tools that need user_id passed
_USER_ID_TOOLS = {"get_profile", "suggest_post", "who_am_i"}

# Build sets of allowed tool names per role, used to gate execution.
_ADMIN_TOOL_NAMES = {t["function"]["name"] for t in ADMIN_TOOLS}
_USER_TOOL_NAMES = {t["function"]["name"] for t in USER_TOOLS}


# ─── Agent core ──────────────────────────────────────────────────────────────

def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.OPENROUTER_BASE_URL,
        api_key=settings.OPENROUTER_API_KEY,
        timeout=120.0,
    )


async def _build_system_prompt(ctx: GroupContext, is_admin: bool) -> str:
    """Build agent system prompt with group context."""
    from core.ai_brain import _get_group_ai_context

    ai_ctx = await _get_group_ai_context(ctx.group_id)

    group_info = ""
    if ai_ctx["ai_group_description"]:
        group_info = f"\nГруппа: {ai_ctx['ai_group_description']}"
    if ai_ctx["ai_system_prompt"]:
        group_info += f"\nЛичность бота: {ai_ctx['ai_system_prompt']}"

    if is_admin:
        return (
            "Ты — AI-администратор группы ВКонтакте. Ты помогаешь владельцу управлять группой.\n"
            "Общайся на русском языке, дружелюбно и по делу.\n"
            "У тебя есть инструменты для публикации, модерации, аналитики и настроек.\n"
            "Если пользователь просит что-то сделать — используй нужный инструмент.\n"
            "Если просто общается — отвечай как умный помощник.\n"
            "Не показывай технические детали (IDs, JSON). Говори человеческим языком.\n"
            "Если не уверен что имел в виду пользователь — уточни.\n"
            f"{group_info}\n"
            f"Дата: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"
        )
    else:
        return (
            "Ты — дружелюбный помощник в группе ВКонтакте. Общайся на русском.\n"
            "У тебя есть инструменты: профиль, предложение постов, гороскоп, анализ личности.\n"
            "Если пользователь просит что-то — используй подходящий инструмент.\n"
            "Если просто общается — будь собеседником, помни контекст.\n"
            f"{group_info}"
        )


async def run_agent(
    ctx: GroupContext,
    user_id: int,
    text: str,
    is_admin: bool = False,
) -> str:
    """Main agent entry point. Handles any message via LLM + tool calling."""
    from database.service import (
        get_setting, get_user_history, save_user_history,
        check_and_increment_limit,
    )

    # Rate limit for non-admin users
    if not is_admin:
        can_request = await check_and_increment_limit(ctx.group_id, user_id)
        if not can_request:
            return (
                "На сегодня лимит бесплатных запросов исчерпан (10). "
                "Завтра снова смогу помочь!"
            )

    model = await get_setting(ctx.group_id, "active_model", settings.DEFAULT_MODEL)
    system_prompt = await _build_system_prompt(ctx, is_admin)
    tools = ADMIN_TOOLS + USER_TOOLS if is_admin else USER_TOOLS

    # Load conversation history
    history = await get_user_history(ctx.group_id, user_id)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-10:])  # Last 10 exchanges
    messages.append({"role": "user", "content": text})

    client = _get_client()

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools if tools else None,
            extra_headers={
                "HTTP-Referer": "https://github.com/vk-ai-admin",
                "X-Title": "VK AI Admin Bot",
            },
        )
    except Exception as e:
        logger.error(f"Agent LLM call failed: {e}")
        return "Произошла ошибка. Попробуйте позже."

    msg = response.choices[0].message

    # If no tool calls — just a text response
    if not msg.tool_calls:
        reply = msg.content or "Не могу ответить."
        # Save to history
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        await save_user_history(ctx.group_id, user_id, history)
        return reply

    # Whitelist of tools this role may run. We send only role-appropriate tools
    # to the LLM, but a hallucinated/cached tool name must still be blocked here.
    allowed = _ADMIN_TOOL_NAMES | _USER_TOOL_NAMES if is_admin else _USER_TOOL_NAMES

    # Execute tool calls
    tool_results = []
    for tool_call in msg.tool_calls:
        fn_name = tool_call.function.name
        try:
            fn_args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            fn_args = {}

        if fn_name not in allowed:
            logger.warning(
                f"Tool {fn_name!r} not allowed for user {user_id} (admin={is_admin})"
            )
            tool_results.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "content": "У вас нет доступа к этой функции.",
            })
            continue

        executor = TOOL_EXECUTORS.get(fn_name)
        if not executor:
            tool_results.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "content": f"Unknown tool: {fn_name}",
            })
            continue

        try:
            if fn_name in _USER_ID_TOOLS:
                result = await executor(ctx, fn_args, user_id=user_id)
            else:
                result = await executor(ctx, fn_args)
        except Exception as e:
            logger.error(f"Tool {fn_name} failed: {e}")
            result = f"Ошибка выполнения: {e}"

        tool_results.append({
            "tool_call_id": tool_call.id,
            "role": "tool",
            "content": result,
        })

    # Send tool results back to LLM for final response
    followup_messages = messages + [
        msg.model_dump(),
        *tool_results,
    ]

    try:
        followup = await client.chat.completions.create(
            model=model,
            messages=followup_messages,
            extra_headers={
                "HTTP-Referer": "https://github.com/vk-ai-admin",
                "X-Title": "VK AI Admin Bot",
            },
        )
        reply = followup.choices[0].message.content or "Готово."
    except Exception as e:
        logger.error(f"Agent followup failed: {e}")
        # Fallback: return raw tool results
        reply = "\n".join(tr["content"] for tr in tool_results)

    # Save to history (simplified — just user message and final reply)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    await save_user_history(ctx.group_id, user_id, history)

    return reply
