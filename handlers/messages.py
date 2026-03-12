import logging
from core.ai_brain import chat_with_memory, generate_response
from core.group_context import GroupContext
from database.service import get_user_stats, get_user_history
from handlers.suggestions import handle_suggestion

logger = logging.getLogger(__name__)


async def handle_message(ctx: GroupContext, from_id: int, text: str, peer_id: int) -> str | None:
    """
    Process user messages. Returns reply text or None if not handled.
    Admin commands (/) are handled by admin handler, not here.
    """
    stripped = text.strip()
    lower = stripped.lower()

    # ── !профиль / !лк / !кабинет ──
    if lower in ("!профиль", "!лк", "!кабинет"):
        stats = await get_user_stats(ctx.group_id, from_id)

        vip_status = "Обычный"
        if stats.is_vip:
            expires = stats.vip_expires.strftime("%d.%m.%Y") if stats.vip_expires else "Навсегда"
            vip_status = f"VIP (до {expires})"

        requests_left = "Безлимит" if stats.is_vip else max(0, 10 - stats.daily_requests)

        return (
            f"Ваш Личный Кабинет:\n"
            f"Статус: {vip_status}\n"
            f"Баланс: {stats.balance} коинов\n"
            f"Осталось ИИ-запросов на сегодня: {requests_left}\n\n"
            f"Игровая статистика:\n"
            f"Уровень: {stats.level}\n"
            f"Опыт: {stats.xp} XP\n"
            f"Репутация: {stats.reputation}\n"
            f"Предупреждений: {stats.warnings}/3\n\n"
            f"Напиши !купить для покупки VIP или коинов."
        )

    # ── !купить ──
    if lower == "!купить":
        return (
            "Магазин:\n\n"
            "1. VIP-статус (Безлимитный ИИ, премиум-модели) — 150 руб/мес\n"
            "2. 1000 коинов — 100 руб\n\n"
            "Для оплаты переведите нужную сумму по реквизитам и напишите "
            "администратору группы. Скоро здесь появится автоматическая оплата!"
        )

    # ── !предложить ──
    if lower.startswith("!предложить"):
        content = stripped[len("!предложить"):].strip()
        return await handle_suggestion(ctx, from_id, content, peer_id)

    # ── !команды / !помощь ──
    if lower in ("!команды", "!помощь"):
        return (
            "Доступные команды:\n\n"
            "!профиль — ваш личный кабинет\n"
            "!купить — магазин VIP и коинов\n"
            "!предложить <текст> — предложить пост\n"
            "!гороскоп — шуточный гороскоп\n"
            "!кто я — ИИ-анализ личности\n\n"
            "Или просто напишите мне — я отвечу!"
        )

    # ── !гороскоп ──
    if lower == "!гороскоп":
        prompt = "Напиши один короткий, смешной и абсурдный гороскоп на сегодня для пользователя ВКонтакте."
        return await generate_response(prompt=prompt, group_id=ctx.group_id)

    # ── !кто я ──
    if lower == "!кто я":
        history = await get_user_history(ctx.group_id, from_id)
        if not history:
            return "Мы еще слишком мало общались, чтобы я понял, кто ты! Напиши мне что-нибудь ещё."

        user_msgs = [m["content"] for m in history if m.get("role") == "user"]
        if not user_msgs:
            return "Не нашёл твоих сообщений в истории. Давай поболтаем!"

        context_text = "\n".join(user_msgs[-10:])
        system_prompt = (
            "Ты психолог-комик. Прочитай последние сообщения пользователя и "
            "сделай шуточный, ироничный, но не обидный вывод о его характере (2-3 предложения)."
        )
        reply = await generate_response(
            prompt=f"Мои сообщения:\n{context_text}\n\nОпиши, кто я?",
            system_prompt=system_prompt,
            group_id=ctx.group_id,
        )
        return f"ИИ-Анализ личности:\n\n{reply}"

    # ── Skip other commands ──
    if stripped.startswith("!") or stripped.startswith("/"):
        return None

    if not stripped:
        return None

    # ── Default: AI chat with memory ──
    logger.info(f"[MSG] group={ctx.group_id} user={from_id}: {stripped[:80]}")
    return await chat_with_memory(group_id=ctx.group_id, vk_id=from_id, user_text=stripped)
