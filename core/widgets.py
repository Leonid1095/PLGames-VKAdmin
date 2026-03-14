"""VK Community Widget — leaderboard/top users widget for groups."""

import logging
from vkbottle import API

from core.crypto import decrypt_token
from database.service import get_top_users, get_all_active_groups, get_setting, get_group

logger = logging.getLogger(__name__)


def _build_table_widget_code(rows: list[dict]) -> str:
    """Build VKScript code for appWidgets.update with type=table."""
    # VK table widget format:
    # return {
    #   "title": "...",
    #   "head": [{"text": "..."}, ...],
    #   "body": [[{"text": "..."}, ...], ...],
    # };
    import json

    head = [
        {"text": "#"},
        {"text": "Участник"},
        {"text": "Уровень"},
        {"text": "XP"},
    ]

    body = []
    for i, row in enumerate(rows, 1):
        body.append([
            {"text": str(i)},
            {"text": row["name"], "url": f"https://vk.com/id{row['vk_id']}"},
            {"text": str(row["level"])},
            {"text": str(row["xp"])},
        ])

    widget = {
        "title": "🏆 Топ участников",
        "head": head,
        "body": body,
    }

    return f"return {json.dumps(widget, ensure_ascii=False)};"


async def _resolve_user_names(api: API, vk_ids: list[int]) -> dict[int, str]:
    """Resolve VK user IDs to first_name + last_name."""
    if not vk_ids:
        return {}
    try:
        users = await api.users.get(user_ids=vk_ids)
        return {u.id: f"{u.first_name} {u.last_name}" for u in users}
    except Exception as e:
        logger.error(f"Failed to resolve user names: {e}")
        return {uid: f"id{uid}" for uid in vk_ids}


async def update_widget_for_group(group_id: int) -> bool:
    """Update the VK community widget with top users for a group.

    Returns True if widget was updated successfully.
    """
    group = await get_group(group_id)
    if not group:
        return False

    widget_enabled = (await get_setting(group_id, "widget_enabled", "false")).lower()
    if widget_enabled != "true":
        return False

    try:
        token = decrypt_token(group.access_token)
        api = API(token=token)
    except Exception as e:
        logger.error(f"Widget: failed to get API for group {group_id}: {e}")
        return False

    # Get top users
    widget_count = int(await get_setting(group_id, "widget_top_count", "10"))
    widget_sort = await get_setting(group_id, "widget_sort_by", "xp")
    top = await get_top_users(group_id, order_by=widget_sort, limit=widget_count)

    if not top:
        logger.info(f"Widget: no users for group {group_id}, skipping")
        return False

    # Resolve names
    vk_ids = [u.vk_id for u in top]
    names = await _resolve_user_names(api, vk_ids)

    rows = []
    for u in top:
        rows.append({
            "vk_id": u.vk_id,
            "name": names.get(u.vk_id, f"id{u.vk_id}"),
            "level": u.level,
            "xp": u.xp,
        })

    code = _build_table_widget_code(rows)

    try:
        await api.request(
            "appWidgets.update",
            {"code": code, "type": "table"},
        )
        logger.info(f"Widget updated for group {group_id} ({len(rows)} users)")
        return True
    except Exception as e:
        logger.error(f"Widget update failed for group {group_id}: {e}")
        return False


async def update_all_widgets():
    """Update widgets for all active groups that have widgets enabled."""
    groups = await get_all_active_groups()
    for group in groups:
        try:
            await update_widget_for_group(group.group_id)
        except Exception as e:
            logger.error(f"Widget update error for group {group.group_id}: {e}")
