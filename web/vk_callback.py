"""VK Callback API endpoint — receives events from all connected groups."""

import asyncio
import logging
from collections import OrderedDict
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from vkbottle import API

from core.group_context import GroupContext
from core.crypto import decrypt_token
from database.service import get_group, get_setting, add_xp_activity
from handlers.admin import handle_admin_command
from handlers.messages import handle_message
from handlers.comments import handle_wall_comment

logger = logging.getLogger(__name__)
router = APIRouter()

# LRU cache for event deduplication
_processed_events: OrderedDict[str, None] = OrderedDict()
_MAX_EVENTS_CACHE = 10000


def _check_and_add_event(group_id: int, event_id: str) -> bool:
    """Returns True if event is duplicate (already seen)."""
    key = f"{group_id}:{event_id}"
    if key in _processed_events:
        _processed_events.move_to_end(key)
        return True
    _processed_events[key] = None
    while len(_processed_events) > _MAX_EVENTS_CACHE:
        _processed_events.popitem(last=False)
    return False


async def _build_context(group_id: int) -> GroupContext | None:
    group = await get_group(group_id)
    if not group:
        logger.warning(f"Received event for unknown/inactive group {group_id}")
        return None

    try:
        token = decrypt_token(group.access_token)
    except Exception as e:
        logger.error(f"Failed to decrypt token for group {group_id}: {e}")
        return None

    api = API(token=token)
    return GroupContext(group_id=group_id, api=api, admin_vk_id=group.admin_vk_id)


# ─── Event processors ───────────────────────────────────────────────────────

async def _process_message(ctx: GroupContext, obj: dict):
    message = obj.get("message", obj)
    from_id = message.get("from_id", 0)
    text = message.get("text", "")
    peer_id = message.get("peer_id", from_id)

    if not text.strip():
        return

    reply = await handle_admin_command(ctx, from_id, text, peer_id)
    if reply is None:
        reply = await handle_message(ctx, from_id, text, peer_id)

    if reply:
        try:
            await ctx.api.messages.send(peer_id=peer_id, message=reply, random_id=0)
        except Exception as e:
            logger.error(f"Failed to send message to {peer_id}: {e}")


async def _process_wall_reply(ctx: GroupContext, obj: dict):
    await handle_wall_comment(ctx, obj)


async def _process_group_join(ctx: GroupContext, obj: dict):
    """Welcome new group member."""
    user_id = obj.get("user_id", 0)
    if not user_id:
        return

    welcome_msg = await get_setting(ctx.group_id, "welcome_message", "")
    use_ai = (await get_setting(ctx.group_id, "welcome_ai", "false")).lower() == "true"

    if not welcome_msg and not use_ai:
        return

    if use_ai:
        from core.ai_brain import generate_response
        try:
            # Get user name for personalized greeting
            users = await ctx.api.users.get(user_ids=[user_id])
            name = users[0].first_name if users else "друг"
        except Exception:
            name = "друг"

        # Use group-aware prompt for welcome
        from core.ai_brain import _get_group_ai_context
        ai_ctx = await _get_group_ai_context(ctx.group_id)

        if ai_ctx["ai_system_prompt"]:
            welcome_system = (
                f"{ai_ctx['ai_system_prompt']}\n"
                "Напиши короткое, тёплое приветствие для нового участника (2-3 предложения). "
                "Расскажи что интересного есть в группе."
            )
        else:
            welcome_system = (
                "Ты дружелюбный администратор группы ВКонтакте. Напиши короткое, "
                "тёплое приветствие для нового участника (2-3 предложения). "
                "Расскажи что интересного есть в группе."
            )

        welcome_msg = await generate_response(
            prompt=f"Пользователь {name} вступил в группу. Поприветствуй его!",
            system_prompt=welcome_system,
            group_id=ctx.group_id,
        )

    if welcome_msg:
        try:
            await ctx.api.messages.send(user_id=user_id, message=welcome_msg, random_id=0)
            logger.info(f"Welcome message sent to {user_id} in group {ctx.group_id}")
        except Exception as e:
            logger.warning(f"Failed to send welcome to {user_id}: {e}")


async def _process_like(ctx: GroupContext, obj: dict):
    """Award XP when someone likes a post."""
    liker_id = obj.get("liker_id", 0)
    if not liker_id or liker_id < 0:
        return
    xp = int(await get_setting(ctx.group_id, "xp_per_like", "2"))
    if xp > 0:
        await add_xp_activity(ctx.group_id, liker_id, xp)


async def _process_repost(ctx: GroupContext, obj: dict):
    """Award XP when someone reposts."""
    from_id = obj.get("from_id", 0)
    if not from_id or from_id < 0:
        return
    xp = int(await get_setting(ctx.group_id, "xp_per_repost", "5"))
    if xp > 0:
        await add_xp_activity(ctx.group_id, from_id, xp)


async def _process_group_leave(ctx: GroupContext, obj: dict):
    """Log when a member leaves."""
    user_id = obj.get("user_id", 0)
    if user_id:
        logger.info(f"User {user_id} left group {ctx.group_id}")


# ─── Main callback endpoint ─────────────────────────────────────────────────

@router.post("/api/vk/events")
async def vk_callback(request: Request):
    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("ok")

    event_type = data.get("type", "")
    group_id = data.get("group_id", 0)
    event_id = data.get("event_id", "")

    # ── Confirmation ──
    if event_type == "confirmation":
        group = await get_group(group_id)
        if group and group.confirmation_code:
            return PlainTextResponse(group.confirmation_code)
        return PlainTextResponse("error")

    # ── Verify secret key ──
    secret = data.get("secret", "")
    group = await get_group(group_id)
    if group and group.secret_key and secret != group.secret_key:
        logger.warning(f"Invalid secret for group {group_id}")
        return PlainTextResponse("ok")

    # ── Deduplicate ──
    if event_id:
        if _check_and_add_event(group_id, event_id):
            return PlainTextResponse("ok")

    # ── Build context ──
    ctx = await _build_context(group_id)
    if not ctx:
        return PlainTextResponse("ok")

    obj = data.get("object", {})

    # ── Dispatch event ──
    if event_type == "message_new":
        asyncio.create_task(_process_message(ctx, obj))
    elif event_type == "wall_reply_new":
        asyncio.create_task(_process_wall_reply(ctx, obj))
    elif event_type == "group_join":
        asyncio.create_task(_process_group_join(ctx, obj))
    elif event_type == "group_leave":
        asyncio.create_task(_process_group_leave(ctx, obj))
    elif event_type == "like_add":
        asyncio.create_task(_process_like(ctx, obj))
    elif event_type == "wall_repost":
        asyncio.create_task(_process_repost(ctx, obj))

    return PlainTextResponse("ok")
