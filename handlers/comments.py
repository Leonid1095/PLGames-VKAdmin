import logging
import time
from core.ai_brain import analyze_toxicity, generate_response
from core.group_context import GroupContext
from core.text_guard import is_llm_failure
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
    # Ответы самого бота приходят обратно как wall_reply_new (from_id = -group_id):
    # их нельзя модерировать и на них нельзя отвечать — иначе бот спорит сам с собой.
    if from_id == -ctx.group_id:
        return

    logger.info(f"[COMMENT] group={ctx.group_id} post={post_id} from={from_id}: {stripped[:80]}")

    # ── Reputation (+ / -) — only if gamification enabled ──
    gamification_on = (await get_setting(ctx.group_id, "gamification_enabled", "false")).lower() == "true"
    if gamification_on and from_id > 0 and reply_to_user and reply_to_user > 0 and from_id != reply_to_user:
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
    # Владельца группы не модерируем: бот не удаляет комментарии админа
    # и не копит ему «страйки» к автобану.
    if from_id == ctx.admin_vk_id:
        return
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
        await _moderate(ctx, owner_id, post_id, comment_id, from_id, stripped)
        return

    # ── Gamification: Award XP (only if enabled; people only, not communities) ──
    if gamification_on and from_id > 0:
        try:
            cooldown_sec = int(await get_setting(ctx.group_id, "xp_cooldown_sec", "60"))
        except ValueError:
            cooldown_sec = 60
        cooldown_key = (ctx.group_id, from_id)
        now = time.monotonic()
        last_xp = _xp_cooldowns.get(cooldown_key, 0)
        if cooldown_sec > 0 and now - last_xp < cooldown_sec:
            pass  # Cooldown active
        else:
            _xp_cooldowns[cooldown_key] = now
            xp_gained = min(5, max(1, len(stripped) // 20))
            new_level, leveled_up = await add_xp(ctx.group_id, from_id, xp_gained)
            if len(_xp_cooldowns) > 5000:
                _xp_cooldowns.clear()
            if leveled_up:
                try:
                    await ctx.api.wall.create_comment(
                        owner_id=owner_id, post_id=post_id, reply_to_comment=comment_id,
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

    # Живой админ не поддакивает под каждым комментом: отвечаем на вопросы,
    # претензии и обращения к сообществу; на реплики/эмоции — молчим.
    no_reply_rule = (
        "\nЕсли комментарий НЕ требует ответа администратора (просто эмоция, "
        "реплика, мем, «спасибо», разговор пользователей между собой) — ответь "
        "ровно одним словом: NO_REPLY."
    )
    if ai_ctx["ai_system_prompt"]:
        system_prompt = (
            f"{ai_ctx['ai_system_prompt']}\n"
            "Напиши краткий ответ на комментарий пользователя (1-2 предложения). "
            "Отвечай в стиле и тематике группы."
            + no_reply_rule
        )
    else:
        system_prompt = (
            "Ты администратор группы ВКонтакте. Напиши краткий, дружелюбный ответ "
            "на комментарий пользователя (1-2 предложения)."
            + no_reply_rule
        )

    reply_text = await generate_response(
        prompt=f"Комментарий: «{stripped}»",
        system_prompt=system_prompt,
        group_id=ctx.group_id,
    )

    # Don't post an LLM error string as a public comment.
    if is_llm_failure(reply_text):
        return
    if "NO_REPLY" in reply_text.upper()[:30]:
        logger.info(f"[COMMENT] group={ctx.group_id} comment={comment_id}: no reply needed")
        return

    try:
        await ctx.api.wall.create_comment(
            owner_id=owner_id,
            post_id=post_id,
            reply_to_comment=comment_id,
            message=reply_text,
        )
    except Exception as e:
        logger.error(f"Failed to reply to comment {comment_id}: {e}")


async def _moderate(
    ctx: GroupContext, owner_id: int, post_id: int, comment_id: int, from_id: int, text: str,
) -> None:
    """Удалить нарушение и считать страйки; что не вышло — отдать людям.

    Ключ сообщества VK не пускает в wall.deleteComment и groups.ban (error 27),
    поэтому провал здесь — штатная ситуация, а не повод молча писать в лог:
    админ получает ссылку на комментарий и делает руками.
    """
    from core import admin_key

    logger.info(f"[MODERATE] Deleting comment {comment_id} from {from_id}")
    admin = await admin_key.get_admin_api(ctx.group_id)  # личный ключ — может всё
    api = admin or ctx.api
    deleted = False
    try:
        await api.wall.delete_comment(owner_id=owner_id, comment_id=comment_id)
        deleted = True
    except Exception as e:
        logger.warning(f"[MODERATE] Can't delete comment {comment_id}: {e}")
        if admin:
            await admin_key.report_admin_key_failure(ctx.group_id, e)

    ban_failed = False
    strikes = 0
    if from_id > 0:
        try:
            strikes = await add_warning(ctx.group_id, from_id)
            if strikes >= 3:
                logger.info(f"[BAN] User {from_id} reached {strikes} strikes. Banning.")
                try:
                    await api.groups.ban(
                        group_id=abs(owner_id),
                        owner_id=from_id,
                        reason=0,
                        comment="Автобан ИИ за систематические нарушения",
                        comment_visible=1,
                    )
                    await clear_warnings(ctx.group_id, from_id)
                except Exception as e:
                    ban_failed = True
                    logger.warning(f"[BAN] Can't ban {from_id}: {e}")
                    if admin:
                        await admin_key.report_admin_key_failure(ctx.group_id, e)
        except Exception as e:
            logger.error(f"Failed to issue warning for {from_id}: {e}")

    if deleted and not ban_failed:
        return

    link = f"https://vk.com/wall{owner_id}_{post_id}?reply={comment_id}"
    lines = ["🛡 Модерация: нужна ваша рука"]
    if not deleted:
        lines.append("Бот счёл комментарий нарушением, но удалить не смог — "
                     + ("VK отказал и личному ключу админа." if admin else
                        "VK не даёт ключу сообщества удалять комментарии."))
    lines += [
        f"Автор: vk.com/id{from_id}" if from_id > 0 else f"Автор: vk.com/club{-from_id}",
        f"Текст: «{text[:300]}»",
        f"Комментарий: {link}",
    ]
    if not admin:
        lines.append(f"Чтобы бот удалял и банил сам, {admin_key.ADMIN_KEY_HINT}.")
    if ban_failed:
        lines.append(f"У автора {strikes} нарушений — стоит заблокировать его "
                     "вручную (Управление → Участники → Чёрный список).")
    from core.escalation import notify_managers
    await notify_managers(ctx, "\n".join(lines))
