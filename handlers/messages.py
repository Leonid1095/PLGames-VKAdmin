import logging
from vkbottle.bot import Blueprint, Message
from core.ai_brain import chat_with_memory, generate_response
from database.service import get_user_stats, get_user_history

logger = logging.getLogger(__name__)

bp = Blueprint("messages")


@bp.on.message(text=["!профиль", "!лк", "!кабинет"])
async def cmd_profile(message: Message):
    """Show user's gamification profile and monetization info."""
    stats = await get_user_stats(message.from_id)
    
    vip_status = "👑 Обычный"
    if stats.is_vip:
        expires = stats.vip_expires.strftime("%d.%m.%Y") if stats.vip_expires else "Навсегда"
        vip_status = f"💎 VIP (до {expires})"

    requests_left = "Безлимит" if stats.is_vip else max(0, 10 - stats.daily_requests)

    text = (
        f"👤 Ваш Личный Кабинет:\n"
        f"Статус: {vip_status}\n"
        f"Баланс: {stats.balance} коинов 💰\n"
        f"Осталось ИИ-запросов на сегодня: {requests_left}\n\n"
        f"📊 Игровая статистика:\n"
        f"Уровень: {stats.level} 🎖\n"
        f"Опыт: {stats.xp} XP 🌟\n"
        f"Репутация: {stats.reputation} ❤️\n"
        f"Предупреждений: {stats.warnings}/3 ⚠️\n\n"
        f"🛒 Напиши `!купить` для покупки VIP или коинов."
    )
    await message.answer(text)

@bp.on.message(text="!купить")
async def cmd_buy(message: Message):
    """Monetization stub command."""
    text = (
        "🛒 Магазин ВКонтакте\n\n"
        "1️⃣ VIP-статус (Безлимитный ИИ, премиум-модели) — 150 руб/мес\n"
        "2️⃣ 1000 коинов — 100 руб\n\n"
        "Для оплаты переведите нужную сумму по реквизитам: 1234 5678 9012 3456 "
        "(Сбербанк) и напишите администратору группы. Скоро здесь появится "
        "автоматическая оплата через ЮKassa/Robokassa!"
    )
    await message.answer(text)


@bp.on.message(text="!гороскоп")
async def cmd_horoscope(message: Message):
    """Generate a fun AI horoscope."""
    await message.answer("✨ Звезды выстраиваются, секундочку...")
    prompt = "Напиши один короткий, смешной и абсурдный гороскоп на сегодня для пользователя ВКонтакте."
    reply = await generate_response(prompt=prompt)
    await message.answer(reply)


@bp.on.message(text="!кто я")
async def cmd_who_am_i(message: Message):
    """AI-powered personality analysis based on chat history."""
    await message.answer("🕵️‍♂️ Анализирую твою историю сообщений...")
    history = await get_user_history(message.from_id)
    if not history:
        await message.answer(
            "Мы еще слишком мало общались, чтобы я понял, кто ты! "
            "Напиши мне что-нибудь ещё."
        )
        return

    # Extract only user messages
    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    if not user_msgs:
        await message.answer("Не нашёл твоих сообщений в истории. Давай поболтаем!")
        return

    context_text = "\n".join(user_msgs[-10:])

    system_prompt = (
        "Ты психолог-комик. Прочитай последние сообщения пользователя и "
        "сделай шуточный, ироничный, но не обидный вывод о его характере (2-3 предложения)."
    )
    reply = await generate_response(
        prompt=f"Мои сообщения:\n{context_text}\n\nОпиши, кто я?",
        system_prompt=system_prompt,
    )
    await message.answer(f"ИИ-Анализ личности:\n\n{reply}")


@bp.on.message(text="<text>")
async def message_handler(message: Message, text: str):
    """Default handler — AI chat with memory."""
    user_id = message.from_id
    text = text.strip()

    # Skip commands handled by other blueprints
    if not text or text.startswith("!") or text.startswith("/"):
        return

    logger.info(f"[MSG] user={user_id}: {text[:80]}")

    reply = await chat_with_memory(vk_id=user_id, user_text=text)
    await message.answer(reply)
