import logging
import time
from core.ai_brain import analyze_toxicity, generate_response
from core.group_context import GroupContext
from database.service import get_setting, add_xp, add_warning, clear_warnings, modify_reputation

logger = logging.getLogger(__name__)

# XP cooldown: prevent XP farming via comment spam
# Key: (group_id, user_id) -> last_xp_time (monotonic)
_xp_cooldowns: dict[tuple[int, int], float] = {}
_XP_COOLDOWN_SEC = 60  # 1 XP award per 60 seconds per user


async def handle_wall_comment(ctx: GroupContext, event_object: dict) -> None:
    """
    Process a wall comment event.
    event_object is the raw 'object' dict from VK Callback API.
    """
    comment_id = event_object.get("id", 0)
    post_id = event_object.get("post_id", 0)
    from_id = event_object.get("from_id", 0)
    text = event_object.get("text", "")
    owner_id = event_object.get("owner_id", 0)
    reply_to_user = event_object.get("reply_to_user", 0)

    stripped = text.strip()
    if not stripped:
        return

    logger.info(f"[COMMENT] group={ctx.group_id} post={post_id} from={from_id}: {stripped[:80]}")

    # ── Reputation (+ / -) ──
    if reply_to_user and reply_to_user > 0 and from_id != reply_to_user:
        if stripped == "+":
            new_rep = await modify_reputation(ctx.group_id, reply_to_user, 1)
            try:
                await ctx.api.wall.create_comment(
                    owner_id=owner_id, post_id=post_id, reply_to_comment=comment_id,
                    message=f"Репутация пользователя повышена! (Текущая: {new_rep})"
                )
            except Exception as e:
                logger.warning(f"Failed to reply about rep+: {e}")
            return
        elif stripped == "-":
            new_rep = await modify_reputation(ctx.group_id, reply_to_user, -1)
            try:
                await ctx.api.wall.create_comment(
                    owner_id=owner_id, post_id=post_id, reply_to_comment=comment_id,
                    message=f"Репутация пользователя понижена! (Текущая: {new_rep})"
                )
            except Exception as e:
                logger.warning(f"Failed to reply about rep-: {e}")
            return

    # ── Moderation: keyword pre-filter + AI ──
    # Quick keyword check before expensive AI call
    banned_words_str = await get_setting(ctx.group_id, "banned_words", "")
    is_toxic = False
    if banned_words_str:
        banned_words = [w.strip().lower() for w in banned_words_str.split(",") if w.strip()]
        text_lower = stripped.lower()
        if any(bw in text_lower for bw in banned_words):
            is_toxic = True
    if not is_toxic:
        is_toxic = await analyze_toxicity(ctx.group_id, stripped)
    if is_toxic:
        logger.info(f"[MODERATE] Deleting comment {comment_id} from {from_id}")
        try:
            await ctx.api.wall.delete_comment(owner_id=owner_id, comment_id=comment_id)
        except Exception as e:
            logger.error(f"Failed to delete comment {comment_id}: {e}")

        try:
            warnings = await add_warning(ctx.group_id, from_id)
            if warnings >= 3:
                logger.info(f"[BAN] User {from_id} reached {warnings} strikes. Banning.")
                await ctx.api.groups.ban(
                    group_id=abs(owner_id),
                    owner_id=from_id,
                    reason=0,
                    comment="Автобан ИИ за систематические нарушения",
                    comment_visible=1,
                )
                await clear_warnings(ctx.group_id, from_id)
        except Exception as e:
            logger.error(f"Failed to issue warning/ban for {from_id}: {e}")
        return

    # ── Gamification: Award XP (with cooldown) ──
    cooldown_sec = int(await get_setting(ctx.group_id, "xp_cooldown_sec", "60"))
    cooldown_key = (ctx.group_id, from_id)
    now = time.monotonic()
    last_xp = _xp_cooldowns.get(cooldown_key, 0)
    if cooldown_sec > 0 and now - last_xp < cooldown_sec:
        # Cooldown active — no XP, but still process AI reply below
        leveled_up = False
        new_level = 0
    else:
        _xp_cooldowns[cooldown_key] = now
        xp_gained = min(5, max(1, len(stripped) // 20))
        new_level, leveled_up = await add_xp(ctx.group_id, from_id, xp_gained)
        # Cleanup old entries periodically
        if len(_xp_cooldowns) > 5000:
            cutoff = now - _XP_COOLDOWN_SEC * 2
            _xp_cooldowns.clear()  # simple cleanup

    if leveled_up:
        try:
            await ctx.api.wall.create_comment(
                owner_id=owner_id,
                post_id=post_id,
                reply_to_comment=comment_id,
                message=f"Уровень повышен! Текущий уровень: {new_level}.",
            )
        except Exception as e:
            logger.warning(f"Failed to congratulate level-up: {e}")

    # ── Optional: AI reply ──
    should_reply = (await get_setting(ctx.group_id, "reply_to_comments", "true")).lower() == "true"
    if not should_reply:
        return

    # Build group-aware reply prompt
    from core.ai_brain import _get_group_ai_context
    ai_ctx = await _get_group_ai_context(ctx.group_id)

    if ai_ctx["ai_system_prompt"]:
        system_prompt = (
            f"{ai_ctx['ai_system_prompt']}\n"
            "Напиши краткий ответ на комментарий пользователя (1-2 предложения). "
            "Отвечай в стиле и тематике группы."
        )
    else:
        system_prompt = (
            "Ты администратор группы ВКонтакте. Напиши краткий, дружелюбный ответ "
            "на комментарий пользователя (1-2 предложения)."
        )

    reply_text = await generate_response(
        prompt=f"Комментарий: «{stripped}»",
        system_prompt=system_prompt,
        group_id=ctx.group_id,
    )

    try:
        await ctx.api.wall.create_comment(
            owner_id=owner_id,
            post_id=post_id,
            reply_to_comment=comment_id,
            message=reply_text,
        )
    except Exception as e:
        logger.error(f"Failed to reply to comment {comment_id}: {e}")
