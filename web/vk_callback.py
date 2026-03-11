"""VK Callback API endpoint — receives events from all connected groups."""

import asyncio
import logging
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from vkbottle import API

from core.group_context import GroupContext
from core.crypto import decrypt_token
from database.service import get_group
from handlers.admin import handle_admin_command
from handlers.messages import handle_message
from handlers.comments import handle_wall_comment

logger = logging.getLogger(__name__)
router = APIRouter()

# Track processed events to avoid duplicates on VK retries
_processed_events: set[str] = set()
_MAX_EVENTS_CACHE = 10000


def _make_event_key(group_id: int, event_id: str) -> str:
    return f"{group_id}:{event_id}"


def _cleanup_events_cache():
    global _processed_events
    if len(_processed_events) > _MAX_EVENTS_CACHE:
        _processed_events = set()


async def _build_context(group_id: int) -> GroupContext | None:
    """Build a GroupContext from DB for the given group."""
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
    return GroupContext(
        group_id=group_id,
        api=api,
        admin_vk_id=group.admin_vk_id,
    )


async def _process_message(ctx: GroupContext, obj: dict):
    """Process a message_new event in background."""
    message = obj.get("message", obj)
    from_id = message.get("from_id", 0)
    text = message.get("text", "")
    peer_id = message.get("peer_id", from_id)

    if not text.strip():
        return

    # Try admin commands first
    reply = await handle_admin_command(ctx, from_id, text, peer_id)
    if reply is None:
        reply = await handle_message(ctx, from_id, text, peer_id)

    if reply:
        try:
            await ctx.api.messages.send(
                peer_id=peer_id,
                message=reply,
                random_id=0,
            )
        except Exception as e:
            logger.error(f"Failed to send message to {peer_id}: {e}")


async def _process_wall_reply(ctx: GroupContext, obj: dict):
    """Process a wall_reply_new event in background."""
    await handle_wall_comment(ctx, obj)


@router.post("/api/vk/events")
async def vk_callback(request: Request):
    """
    Main Callback API endpoint.
    VK sends JSON with type, group_id, object, secret, event_id.
    Must respond with 'ok' within 3 seconds.
    """
    try:
        data = await request.json()
    except Exception:
        return PlainTextResponse("ok")

    event_type = data.get("type", "")
    group_id = data.get("group_id", 0)
    event_id = data.get("event_id", "")

    # ── Confirmation (VK verifying the server) ──
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
        event_key = _make_event_key(group_id, event_id)
        if event_key in _processed_events:
            return PlainTextResponse("ok")
        _processed_events.add(event_key)
        _cleanup_events_cache()

    # ── Build context ──
    ctx = await _build_context(group_id)
    if not ctx:
        return PlainTextResponse("ok")

    obj = data.get("object", {})

    # ── Dispatch event to background task ──
    if event_type == "message_new":
        asyncio.create_task(_process_message(ctx, obj))
    elif event_type == "wall_reply_new":
        asyncio.create_task(_process_wall_reply(ctx, obj))

    # Respond immediately so VK doesn't retry
    return PlainTextResponse("ok")
