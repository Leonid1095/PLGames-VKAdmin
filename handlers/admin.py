import logging
from vkbottle.bot import Blueprint, Message
from core.ai_brain import generate_post
from database.service import get_setting, set_setting, clear_user_history
from core.config import settings as app_settings

logger = logging.getLogger(__name__)

bp = Blueprint("admin")


def is_owner(user_id: int) -> bool:
    """Check if the user is the bot owner."""
    return str(user_id) == str(app_settings.OWNER_VK_ID)


# ── /помощь ──────────────────────────────────────────────────────────────────
@bp.on.message(text="/помощь")
async def cmd_help(message: Message):
    if not is_owner(message.from_id):
        return
    help_text = (
        "🤖 Команды администратора:\n\n"
        "/пост — сгенерировать и опубликовать пост\n"
        "/пост <тема> — пост на конкретную тему\n"
        "/настройка <ключ> <значение> — изменить настройку\n"
        "/посмотреть <ключ> — посмотреть текущую настройку\n"
        "/очистить <vk_id> — очистить память диалога с пользователем\n"
        "/vip <vk_id> <days> — выдать VIP-статус пользователю\n"
        "/помощь — показать это сообщение\n\n"
        "📋 Доступные ключи настроек:\n"
        "• active_model — модель ИИ (напр: openai/gpt-4o-mini)\n"
        "• system_prompt — системный промпт бота\n"
        "• moderation_aggressiveness — low / medium / high\n"
        "• reply_to_comments — true / false\n"
        "• autopost_enabled — true / false\n"
        "• autopost_interval_hours — число часов\n"
        "• autopost_topics — темы для постов через запятую"
    )
    await message.answer(help_text)


# ── /пост ────────────────────────────────────────────────────────────────────
@bp.on.message(text="/пост <topic>")
async def cmd_post_topic(message: Message, topic: str):
    if not is_owner(message.from_id):
        return
    await message.answer("⏳ Генерирую пост...")
    post_text = await generate_post(topic=topic)
    owner_id = -(int(app_settings.VK_GROUP_ID))
    try:
        await bp.api.wall.post(owner_id=owner_id, message=post_text)
        await message.answer(f"✅ Пост опубликован!\n\n{post_text}")
    except Exception as e:
        logger.error(f"Failed to publish post: {e}")
        await message.answer(f"❌ Ошибка публикации: {e}")


@bp.on.message(text="/пост")
async def cmd_post(message: Message):
    if not is_owner(message.from_id):
        return
    await message.answer("⏳ Генерирую пост на случайную тему из настроек...")
    post_text = await generate_post()
    owner_id = -(int(app_settings.VK_GROUP_ID))
    try:
        await bp.api.wall.post(owner_id=owner_id, message=post_text)
        await message.answer(f"✅ Пост опубликован!\n\n{post_text}")
    except Exception as e:
        logger.error(f"Failed to publish post: {e}")
        await message.answer(f"❌ Ошибка публикации: {e}")


# ── /настройка ───────────────────────────────────────────────────────────────
@bp.on.message(text="/настройка <key> <value>")
async def cmd_set_setting(message: Message, key: str, value: str):
    if not is_owner(message.from_id):
        return
    await set_setting(key.strip(), value.strip())
    await message.answer(f"✅ Настройка обновлена:\n{key} = {value}")


# ── /посмотреть ──────────────────────────────────────────────────────────────
@bp.on.message(text="/посмотреть <key>")
async def cmd_get_setting(message: Message, key: str):
    if not is_owner(message.from_id):
        return
    value = await get_setting(key.strip(), default="(не задано)")
    await message.answer(f"📋 {key} = {value}")


# ── /очистить ────────────────────────────────────────────────────────────────
@bp.on.message(text="/очистить <vk_id>")
async def cmd_clear_memory(message: Message, vk_id: str):
    if not is_owner(message.from_id):
        return
    try:
        uid = int(vk_id.strip())
        await clear_user_history(uid)
        await message.answer(f"✅ Память диалога с пользователем {uid} очищена.")
    except ValueError:
        await message.answer("❌ Укажите корректный числовой VK ID.")

# ── /vip ─────────────────────────────────────────────────────────────────────
@bp.on.message(text="/vip <vk_id> <days>")
async def cmd_grant_vip(message: Message, vk_id: str, days: str):
    if not is_owner(message.from_id):
        return
    try:
        uid = int(vk_id.strip())
        d = int(days.strip())
        from database.service import grant_vip
        await grant_vip(uid, d)
        await message.answer(f"✅ Пользователю {uid} выдан VIP на {d} дней.")
    except ValueError:
        await message.answer("❌ Ошибка форматов. Укажите числовой VK ID и количество дней.")
