import logging
from vkbottle.bot import Blueprint
from vkbottle import GroupEventType, GroupTypes
from core.ai_brain import analyze_toxicity, generate_response
from database.service import get_setting, add_xp, add_warning, clear_warnings, modify_reputation

logger = logging.getLogger(__name__)

bp = Blueprint("comments")


@bp.on.raw_event(GroupEventType.WALL_REPLY_NEW, dataclass=GroupTypes.WallReplyNew)
async def new_comment_handler(event: GroupTypes.WallReplyNew):
    """
    Triggered on every new wall comment.
    1. Handle reputation (+/-) if replying to another user.
    2. Check toxicity → delete + strike. If 3 strikes → ban.
    3. If clean → give XP, optionally answer with AI.
    """
    obj = event.object
    comment_id = obj.id
    post_id = obj.post_id
    from_id = obj.from_id
    text = obj.text or ""
    owner_id = obj.owner_id

    # Safely get reply_to_user (may not exist on all event types)
    reply_to_user = getattr(obj, "reply_to_user", None) or 0

    stripped = text.strip()
    if not stripped:
        return

    logger.info(f"[COMMENT] post={post_id} from={from_id}: {stripped[:80]}")

    # ── Reputation (+ / -) ──────────────────────────────────────────────────
    if reply_to_user > 0 and from_id != reply_to_user:
        if stripped == "+":
            new_rep = await modify_reputation(reply_to_user, 1)
            try:
                await bp.api.wall.create_comment(
                    owner_id=owner_id, post_id=post_id, reply_to_comment=comment_id,
                    message=f"🌟 Репутация пользователя повышена! (Текущая: {new_rep})"
                )
            except Exception as e:
                logger.warning(f"Failed to reply about rep+: {e}")
            return
        elif stripped == "-":
            new_rep = await modify_reputation(reply_to_user, -1)
            try:
                await bp.api.wall.create_comment(
                    owner_id=owner_id, post_id=post_id, reply_to_comment=comment_id,
                    message=f"💔 Репутация пользователя понижена! (Текущая: {new_rep})"
                )
            except Exception as e:
                logger.warning(f"Failed to reply about rep-: {e}")
            return

    # ── Moderation & Strikes ────────────────────────────────────────────────
    is_toxic = await analyze_toxicity(stripped)
    if is_toxic:
        logger.info(f"[MODERATE] Deleting comment {comment_id} from {from_id}")
        try:
            await bp.api.wall.delete_comment(owner_id=owner_id, comment_id=comment_id)
        except Exception as e:
            logger.error(f"Failed to delete comment {comment_id}: {e}")

        try:
            warnings = await add_warning(from_id)
            if warnings >= 3:
                logger.info(f"[BAN] User {from_id} reached {warnings} strikes. Banning.")
                await bp.api.groups.ban(
                    group_id=abs(owner_id),
                    owner_id=from_id,
                    reason=0,
                    comment="Автобан ИИ за систематические нарушения",
                    comment_visible=1,
                )
                await clear_warnings(from_id)
        except Exception as e:
            logger.error(f"Failed to issue warning/ban for {from_id}: {e}")
        return

    # ── Gamification: Award XP ──────────────────────────────────────────────
    xp_gained = min(5, max(1, len(stripped) // 20))
    new_level, leveled_up = await add_xp(from_id, xp_gained)

    if leveled_up:
        try:
            await bp.api.wall.create_comment(
                owner_id=owner_id,
                post_id=post_id,
                reply_to_comment=comment_id,
                message=f"🎉 Уровень повышен! Текущий уровень: {new_level}.",
            )
        except Exception as e:
            logger.warning(f"Failed to congratulate level-up: {e}")

    # ── Optional: AI reply ──────────────────────────────────────────────────
    should_reply = (await get_setting("reply_to_comments", "true")).lower() == "true"
    if not should_reply:
        return

    system_prompt = (
        "Ты администратор группы ВКонтакте. Напиши краткий, дружелюбный ответ "
        "на комментарий пользователя (1-2 предложения)."
    )
    reply_text = await generate_response(
        prompt=f"Комментарий: «{stripped}»",
        system_prompt=system_prompt,
    )

    try:
        await bp.api.wall.create_comment(
            owner_id=owner_id,
            post_id=post_id,
            reply_to_comment=comment_id,
            message=reply_text,
        )
    except Exception as e:
        logger.error(f"Failed to reply to comment {comment_id}: {e}")
