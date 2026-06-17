"""VK Callback API endpoint — receives events from all connected groups."""

import asyncio
import logging
import time
from collections import OrderedDict
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from vkbottle import API

from core.group_context import GroupContext
from core.crypto import decrypt_token
from core.agent import run_agent
from core.onboarding import is_onboarding_needed, run_onboarding
from database.service import get_group, get_setting, add_xp_activity
from handlers.comments import handle_wall_comment

logger = logging.getLogger(__name__)
router = APIRouter()

# LRU cache for event deduplication
_processed_events: OrderedDict[str, None] = OrderedDict()
_MAX_EVENTS_CACHE = 10000

# Strong refs to in-flight handlers so they aren't garbage-collected mid-run
# (asyncio only holds a weak ref to tasks). Discarded when each task finishes.
_background_tasks: set[asyncio.Task] = set()

# Basic rate limiting: max events per group per window
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 120  # max events per group per window
_rate_counters: dict[int, tuple[float, int]] = {}  # group_id -> (window_start, count)


def _check_rate_limit(group_id: int) -> bool:
    """Returns True if rate limit exceeded for this group."""
    now = time.monotonic()
    entry = _rate_counters.get(group_id)
    if entry is None or now - entry[0] > _RATE_LIMIT_WINDOW:
        _rate_counters[group_id] = (now, 1)
        return False
    window_start, count = entry
    if count >= _RATE_LIMIT_MAX:
        return True
    _rate_counters[group_id] = (window_start, count + 1)
    return False


def _event_key(group_id: int, event_id: str) -> str:
    return f"{group_id}:{event_id}"


def _check_and_add_event(group_id: int, event_id: str) -> bool:
    """Returns True if event is duplicate (already seen)."""
    key = _event_key(group_id, event_id)
    if key in _processed_events:
        _processed_events.move_to_end(key)
        return True
    _processed_events[key] = None
    while len(_processed_events) > _MAX_EVENTS_CACHE:
        _processed_events.popitem(last=False)
    return False


def _spawn(coro, event_key: str | None) -> None:
    """Run an event handler in the background without losing it or its errors.

    Keeps a strong ref (avoid GC), logs any exception (no silent swallowing),
    and on failure un-marks the event from the dedup cache so a VK redelivery
    of the same event_id is reprocessed instead of being dropped.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "Background event handler failed (event=%s): %r",
                event_key, exc, exc_info=exc,
            )
            if event_key is not None:
                _processed_events.pop(event_key, None)

    task.add_done_callback(_done)


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

    is_admin = (from_id == ctx.admin_vk_id)

    # Onboarding: admin's first messages go through setup dialog
    if is_admin and await is_onboarding_needed(ctx.group_id):
        reply = await run_onboarding(ctx, from_id, text.strip())
        if reply:
            try:
                await ctx.api.messages.send(peer_id=peer_id, message=reply, random_id=0)
            except Exception as e:
                logger.error(f"Failed to send onboarding message to {peer_id}: {e}")
            return

    reply = await run_agent(ctx, user_id=from_id, text=text.strip(), is_admin=is_admin)

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

    # Resolve user name for placeholders and AI
    try:
        users = await ctx.api.users.get(user_ids=[user_id])
        first_name = users[0].first_name if users else "друг"
        last_name = users[0].last_name if users else ""
    except Exception:
        first_name = "друг"
        last_name = ""

    # Support placeholders in static welcome message
    if welcome_msg and not use_ai:
        try:
            members_resp = await ctx.api.groups.get_members(group_id=ctx.group_id, count=0)
            member_count = str(members_resp.count) if members_resp else "?"
        except Exception:
            member_count = "?"
        welcome_msg = (
            welcome_msg
            .replace("{name}", first_name)
            .replace("{username}", first_name)
            .replace("{first_name}", first_name)
            .replace("{last_name}", last_name)
            .replace("{member_count}", member_count)
        )

    if use_ai:
        from core.ai_brain import generate_response
        name = first_name

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

    # Never send an LLM error string as a welcome. If AI generation failed,
    # stay silent rather than greeting newcomers with "Извините, произошла ошибка".
    if welcome_msg and not welcome_msg.startswith("Извините"):
        try:
            await ctx.api.messages.send(user_id=user_id, message=welcome_msg, random_id=0)
            logger.info(f"Welcome message sent to {user_id} in group {ctx.group_id}")
        except Exception as e:
            logger.warning(f"Failed to send welcome to {user_id}: {e}")


async def _process_like(ctx: GroupContext, obj: dict):
    """Award XP when someone likes a post (only if gamification enabled)."""
    if (await get_setting(ctx.group_id, "gamification_enabled", "false")).lower() != "true":
        return
    liker_id = obj.get("liker_id", 0)
    if not liker_id or liker_id < 0:
        return
    try:
        xp = int(await get_setting(ctx.group_id, "xp_per_like", "2"))
    except ValueError:
        xp = 2
    if xp > 0:
        await add_xp_activity(ctx.group_id, liker_id, xp)


async def _process_repost(ctx: GroupContext, obj: dict):
    """Award XP when someone reposts (only if gamification enabled)."""
    if (await get_setting(ctx.group_id, "gamification_enabled", "false")).lower() != "true":
        return
    from_id = obj.get("from_id", 0)
    if not from_id or from_id < 0:
        return
    try:
        xp = int(await get_setting(ctx.group_id, "xp_per_repost", "5"))
    except ValueError:
        xp = 5
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

    # ── Rate limit ──
    if _check_rate_limit(group_id):
        logger.warning(f"Rate limit exceeded for group {group_id}")
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
    event_key = _event_key(group_id, event_id) if event_id else None

    # ── Dispatch event ──
    if event_type == "message_new":
        _spawn(_process_message(ctx, obj), event_key)
    elif event_type == "wall_reply_new":
        _spawn(_process_wall_reply(ctx, obj), event_key)
    elif event_type == "group_join":
        _spawn(_process_group_join(ctx, obj), event_key)
    elif event_type == "group_leave":
        _spawn(_process_group_leave(ctx, obj), event_key)
    elif event_type == "like_add":
        _spawn(_process_like(ctx, obj), event_key)
    elif event_type == "wall_repost":
        _spawn(_process_repost(ctx, obj), event_key)

    return PlainTextResponse("ok")
