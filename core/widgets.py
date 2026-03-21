"""VK Community Widget — leaderboard/top users widget for groups.

Widget update requires an app_widget token (obtained via VKWebAppGetCommunityAuthToken
in the Mini App), NOT the regular group access token.
"""

import json
import logging
import httpx

from core.config import settings
from database.service import get_top_users, get_all_active_groups, get_setting, get_group, set_setting

logger = logging.getLogger(__name__)


def _build_table_widget_code(rows: list[dict], sort_by: str = "xp") -> str:
    """Build VKScript code for appWidgets.update with type=table.

    VK requires this exact format:
    return {"title": "...", "head": [...], "body": [...]};
    """
    sort_labels = {
        "xp": "XP",
        "level": "Ур.",
        "messages": "Сообщ.",
        "rep": "Репут.",
    }
    value_label = sort_labels.get(sort_by, "XP")
    value_key = {
        "xp": "xp",
        "level": "level",
        "messages": "messages",
        "rep": "reputation",
    }.get(sort_by, "xp")

    head = [
        {"text": "#", "align": "center"},
        {"text": "Участник"},
        {"text": value_label, "align": "right"},
    ]

    body = []
    for i, row in enumerate(rows, 1):
        # Medal for top-3
        rank = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else str(i)
        value = row.get(value_key, row.get("xp", 0))
        body.append([
            {"text": rank, "icon_id": f"id{row['vk_id']}"},
            {"text": row["name"], "url": f"https://vk.com/id{row['vk_id']}"},
            {"text": str(value)},
        ])

    widget = {
        "title": "🏆 Топ участников",
        "title_url": f"https://vk.com/app{settings.VK_MINIAPP_ID}" if settings.VK_MINIAPP_ID else "",
        "head": head,
        "body": body,
    }

    # Remove empty title_url
    if not widget["title_url"]:
        del widget["title_url"]

    return f"return {json.dumps(widget, ensure_ascii=False)};"


async def _resolve_user_names(api_or_token, vk_ids: list[int]) -> dict[int, str]:
    """Resolve VK user IDs to first_name + last_name.

    Accepts either a vkbottle API instance or an access token string.
    """
    if not vk_ids:
        return {}

    # If it's a string token, use httpx directly
    if isinstance(api_or_token, str):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.vk.com/method/users.get",
                    params={
                        "user_ids": ",".join(str(uid) for uid in vk_ids),
                        "access_token": api_or_token,
                        "v": "5.199",
                    },
                )
                data = resp.json()
                users = data.get("response", [])
                return {u["id"]: f"{u['first_name']} {u['last_name']}" for u in users}
        except Exception as e:
            logger.error(f"Failed to resolve user names via token: {e}")
            return {uid: f"id{uid}" for uid in vk_ids}

    # vkbottle API instance
    try:
        users = await api_or_token.users.get(user_ids=vk_ids)
        return {u.id: f"{u.first_name} {u.last_name}" for u in users}
    except Exception as e:
        logger.error(f"Failed to resolve user names: {e}")
        return {uid: f"id{uid}" for uid in vk_ids}


async def update_widget_for_group(group_id: int) -> tuple[bool, str]:
    """Update the VK community widget with top users for a group.

    Uses the widget_token (obtained via VKWebAppGetCommunityToken)
    to call appWidgets.update.

    Returns (success: bool, message: str).
    """
    group = await get_group(group_id)
    if not group:
        return False, "Группа не найдена"

    widget_enabled = (await get_setting(group_id, "widget_enabled", "false")).lower()
    if widget_enabled != "true":
        return False, "Виджет выключен в настройках"

    # Get the widget token (app_widget scope) — saved when user installs widget via Mini App
    widget_token = await get_setting(group_id, "widget_token", "")

    if not widget_token:
        return False, "Сначала нажмите «Установить виджет» — это даст боту права на обновление данных"

    # Get top users
    widget_count = int(await get_setting(group_id, "widget_top_count", "10"))
    widget_sort = await get_setting(group_id, "widget_sort_by", "xp")
    top = await get_top_users(group_id, order_by=widget_sort, limit=widget_count)

    if not top:
        return False, "Нет данных об участниках. Пользователи появятся когда начнут писать сообщения/комментарии"

    # Resolve names using the widget token
    vk_ids = [u.vk_id for u in top]
    names = await _resolve_user_names(widget_token, vk_ids)

    rows = []
    for u in top:
        rows.append({
            "vk_id": u.vk_id,
            "name": names.get(u.vk_id, f"id{u.vk_id}"),
            "level": u.level,
            "xp": u.xp,
            "messages": u.messages_count,
            "reputation": u.reputation,
        })

    code = _build_table_widget_code(rows, sort_by=widget_sort)

    # Call appWidgets.update via httpx (widget_token has app_widget scope)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.vk.com/method/appWidgets.update",
                params={
                    "code": code,
                    "type": "table",
                    "access_token": widget_token,
                    "v": "5.199",
                },
            )
            data = resp.json()

            if "error" in data:
                error = data["error"]
                error_code = error.get("error_code", 0)
                error_msg = error.get("error_msg", "unknown")
                logger.error(f"Widget API error for group {group_id}: [{error_code}] {error_msg}")

                # Token expired or invalid — clear it
                if error_code in (5, 15, 27):
                    logger.warning(f"Widget token invalid for group {group_id}, clearing.")
                    await set_setting(group_id, "widget_token", "")
                    return False, "Токен виджета устарел. Нажмите «Установить виджет» заново"

                return False, f"VK API: {error_msg}"

            logger.info(f"Widget updated for group {group_id} ({len(rows)} users)")
            return True, f"Обновлено ({len(rows)} участников)"

    except Exception as e:
        logger.error(f"Widget update failed for group {group_id}: {e}")
        return False, f"Ошибка соединения: {e}"


async def update_all_widgets():
    """Update widgets for all active groups that have widgets enabled."""
    groups = await get_all_active_groups()
    for group in groups:
        try:
            success, msg = await update_widget_for_group(group.group_id)
            if not success and "выключен" not in msg.lower():
                logger.info(f"Widget skip for group {group.group_id}: {msg}")
        except Exception as e:
            logger.error(f"Widget update error for group {group.group_id}: {e}")
