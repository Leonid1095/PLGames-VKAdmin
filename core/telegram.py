"""Telegram Bot API client — cross-posting from VK to Telegram."""

import logging
import httpx

from database.service import get_setting

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}"


async def send_to_telegram(
    group_id: int,
    text: str,
    vk_post_id: int = 0,
) -> bool:
    """
    Send a post to the linked Telegram channel/chat.
    Returns True if sent successfully.
    """
    enabled = (await get_setting(group_id, "telegram_enabled", "false")).lower()
    if enabled != "true":
        return False

    bot_token = await get_setting(group_id, "telegram_bot_token", "")
    if not bot_token:
        # Fallback to global token
        from core.config import settings
        bot_token = settings.TELEGRAM_BOT_TOKEN
    if not bot_token:
        logger.warning(f"Telegram: no bot token for group {group_id}")
        return False

    chat_id = await get_setting(group_id, "telegram_chat_id", "")
    if not chat_id:
        logger.warning(f"Telegram: no chat_id for group {group_id}")
        return False

    # Build message text
    message = text

    # Optionally append VK post link
    link_enabled = (await get_setting(group_id, "telegram_add_vk_link", "true")).lower()
    if link_enabled == "true" and vk_post_id:
        vk_url = f"https://vk.com/wall-{group_id}_{vk_post_id}"
        message += f"\n\n🔗 Пост ВК: {vk_url}"

    # Telegram limits message to 4096 chars
    if len(message) > 4096:
        message = message[:4090] + "\n..."

    try:
        url = f"{_TG_API.format(token=bot_token)}/sendMessage"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })

        data = resp.json()
        if data.get("ok"):
            logger.info(f"Telegram: sent to {chat_id} for group {group_id}")
            return True
        else:
            # Retry without HTML parse mode (in case text has unescaped HTML)
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": False,
                })
            data = resp.json()
            if data.get("ok"):
                logger.info(f"Telegram: sent to {chat_id} (no HTML) for group {group_id}")
                return True
            logger.error(f"Telegram API error for group {group_id}: {data}")
            return False

    except Exception as e:
        logger.error(f"Telegram send failed for group {group_id}: {e}")
        return False


async def check_bot_token(bot_token: str) -> dict | None:
    """Verify a Telegram bot token and return bot info, or None if invalid."""
    try:
        url = f"{_TG_API.format(token=bot_token)}/getMe"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        return None
    except Exception:
        return None
