import logging
from vkbottle import API
from core.ai_brain import generate_post
from core.group_context import GroupContext
from database.service import get_setting, set_setting, clear_user_history, grant_vip

logger = logging.getLogger(__name__)


def is_owner(ctx: GroupContext, user_id: int) -> bool:
    """Check if the user is the group admin."""
    return user_id == ctx.admin_vk_id


async def handle_admin_command(ctx: GroupContext, from_id: int, text: str, peer_id: int) -> str | None:
    """
    Process admin commands. Returns reply text or None if not an admin command.
    """
    if not text.startswith("/"):
        return None

    if not is_owner(ctx, from_id):
        return None

    parts = text.split(maxsplit=2)
    cmd = parts[0].lower()

    # ── /помощь ──
    if cmd == "/помощь":
        return (
            "Команды администратора:\n\n"
            "/пост — сгенерировать и опубликовать пост\n"
            "/пост <тема> — пост на конкретную тему\n"
            "/настройка <ключ> <значение> — изменить настройку\n"
            "/посмотреть <ключ> — посмотреть текущую настройку\n"
            "/очистить <vk_id> — очистить память диалога с пользователем\n"
            "/vip <vk_id> <days> — выдать VIP-статус пользователю\n"
            "/помощь — показать это сообщение\n\n"
            "Доступные ключи настроек:\n"
            "active_model — модель ИИ\n"
            "system_prompt — системный промпт бота\n"
            "moderation_aggressiveness — low / medium / high\n"
            "reply_to_comments — true / false\n"
            "autopost_enabled — true / false\n"
            "autopost_interval_hours — число часов\n"
            "autopost_topics — темы для постов через запятую"
        )

    # ── /пост ──
    if cmd == "/пост":
        topic = parts[1] if len(parts) > 1 else ""
        post_text = await generate_post(group_id=ctx.group_id, topic=topic)
        owner_id = -ctx.group_id
        try:
            await ctx.api.wall.post(owner_id=owner_id, message=post_text)
            return f"Пост опубликован!\n\n{post_text}"
        except Exception as e:
            logger.error(f"Failed to publish post: {e}")
            return f"Ошибка публикации: {e}"

    # ── /настройка ──
    if cmd == "/настройка":
        if len(parts) < 3:
            return "Формат: /настройка <ключ> <значение>"
        key = parts[1].strip()
        value = parts[2].strip()
        await set_setting(ctx.group_id, key, value)
        return f"Настройка обновлена:\n{key} = {value}"

    # ── /посмотреть ──
    if cmd == "/посмотреть":
        if len(parts) < 2:
            return "Формат: /посмотреть <ключ>"
        key = parts[1].strip()
        value = await get_setting(ctx.group_id, key, default="(не задано)")
        return f"{key} = {value}"

    # ── /очистить ──
    if cmd == "/очистить":
        if len(parts) < 2:
            return "Формат: /очистить <vk_id>"
        try:
            uid = int(parts[1].strip())
            await clear_user_history(ctx.group_id, uid)
            return f"Память диалога с пользователем {uid} очищена."
        except ValueError:
            return "Укажите корректный числовой VK ID."

    # ── /vip ──
    if cmd == "/vip":
        if len(parts) < 3:
            return "Формат: /vip <vk_id> <days>"
        try:
            sub_parts = parts[1].split()
            uid = int(sub_parts[0].strip())
            d = int(parts[2].strip()) if len(parts) > 2 else int(sub_parts[1].strip())
            await grant_vip(ctx.group_id, uid, d)
            return f"Пользователю {uid} выдан VIP на {d} дней."
        except (ValueError, IndexError):
            return "Ошибка формата. Укажите: /vip <vk_id> <days>"

    return None
