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


def _client() -> httpx.AsyncClient:
    # Отдельная фабрика — точка подмены транспорта в тестах.
    return httpx.AsyncClient(timeout=15)


async def _notify_admin(group_id: int, text: str) -> bool:
    """ЛС менеджерам группы от её имени. True — хоть кому-то доставлено."""
    from core.escalation import notify_managers
    from web.vk_callback import _build_context

    ctx = await _build_context(group_id)
    if not ctx:
        return False
    return await notify_managers(ctx, text) > 0


def widget_install_hint() -> str:
    """Где админу нажать «Установить виджет» — чтобы не вспоминать каждый раз.
    С телефона — только кнопка на первом экране: на Android остальные страницы
    мини-аппа уходят во внешний браузер, и запрос прав VK оттуда не проходит."""
    app = f"https://vk.com/app{settings.VK_MINIAPP_ID}" if settings.VK_MINIAPP_ID else "Mini App"
    return (f"на компьютере: {app} → ⚙️ у группы → вкладка «🏆 Виджет» → «Установить виджет»; "
            f"с телефона: {app} → кнопка «🏆 Виджет» у группы на первом экране")


async def _alert_once(group_id: int, problem: str, text: str) -> None:
    """Одно ЛС на каждую новую проблему, а не каждый час. Сбрасывается
    успешным обновлением — следующая поломка снова дойдёт до админа."""
    if await get_setting(group_id, "widget_alerted", "") == problem:
        return
    if await _notify_admin(group_id, text):
        await set_setting(group_id, "widget_alerted", problem)


WIDGET_TYPE = "table"
TABLE_MAX_ROWS = 10  # больше VK в table не принимает (плюс строка заголовков)
WIDGET_TITLE = "🏆 Топ участников"
_HEAD = [
    {"text": "Участник"},
    {"text": "Уровень", "align": "center"},
    {"text": "Опыт", "align": "center"},
    {"text": "Сообщения", "align": "center"},
]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def _table_code(body: list[list[dict]], group_id: int) -> str:
    widget = {"title": WIDGET_TITLE, "head": _HEAD, "body": body}
    if settings.VK_MINIAPP_ID:
        widget["title_url"] = f"https://vk.com/app{settings.VK_MINIAPP_ID}_-{group_id}"
    return f"return {json.dumps(widget, ensure_ascii=False)};"


def _build_table_widget_code(rows: list[dict], group_id: int) -> str:
    """VKScript для appWidgets.update с type=table. Иконку VK разрешает только
    в первой ячейке строки — поэтому аватарка, место и имя живут вместе."""
    body = []
    for i, row in enumerate(rows[:TABLE_MAX_ROWS], 1):
        rank = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        body.append([
            {
                "text": f"{rank} {row['name']}",
                "url": f"https://vk.com/id{row['vk_id']}",
                "icon_id": f"id{row['vk_id']}",  # VK подставляет аватарку участника
            },
            {"text": str(row["level"])},
            {"text": str(row["xp"])},
            {"text": str(row["messages"])},
        ])
    return _table_code(body, group_id)


def demo_widget_code(group_id: int) -> str:
    """Заглушка для предпросмотра, пока в рейтинге никого нет."""
    return _table_code([[{"text": "Пока никого нет"}, {"text": "—"}, {"text": "—"}, {"text": "—"}]], group_id)


async def build_top_widget(group_id: int, token: str) -> tuple[str, int] | None:
    """(VKScript виджета «Топ участников», сколько в нём человек) или None,
    если рейтинга ещё нет.

    token — любой ключ, которым можно вызвать users.get (нужны имена)."""
    widget_count = int(await get_setting(group_id, "widget_top_count", "10"))
    widget_sort = await get_setting(group_id, "widget_sort_by", "xp")
    top = await get_top_users(
        group_id, order_by=widget_sort, limit=min(widget_count, TABLE_MAX_ROWS),
    )
    if not top:
        return None

    names = await _resolve_user_names(token, [u.vk_id for u in top])
    rows = [{
        "vk_id": u.vk_id,
        "name": names.get(u.vk_id, f"id{u.vk_id}"),
        "level": u.level,
        "xp": u.xp,
        "messages": u.messages_count,
        "reputation": u.reputation,
    } for u in top]
    return _build_table_widget_code(rows, group_id), len(rows)


async def _resolve_user_names(api_or_token, vk_ids: list[int]) -> dict[int, str]:
    """Resolve VK user IDs to first_name + last_name.

    Accepts either a vkbottle API instance or an access token string.
    """
    if not vk_ids:
        return {}

    # If it's a string token, use httpx directly
    if isinstance(api_or_token, str):
        try:
            async with _client() as client:
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
        await _alert_once(
            group_id, "no_token",
            "🏆 Виджет «Топ участников» включён, но у бота нет токена — "
            "рейтинг в группе не обновляется.\n"
            f"Где установить: {widget_install_hint()}",
        )
        return False, "Сначала нажмите «Установить виджет» — это даст боту права на обновление данных"

    # Имена резолвим тем же токеном виджета
    built = await build_top_widget(group_id, widget_token)
    if built is None:
        return False, "Нет данных об участниках. Пользователи появятся когда начнут писать сообщения/комментарии"
    code, shown = built

    # Call appWidgets.update via httpx (widget_token has app_widget scope)
    try:
        async with _client() as client:
            resp = await client.get(
                "https://api.vk.com/method/appWidgets.update",
                params={
                    "code": code,
                    "type": WIDGET_TYPE,
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
                await set_setting(group_id, "widget_last_error", f"[{error_code}] {error_msg}")

                # Токен НЕ стираем: раньше одна ошибка VK молча удаляла его, и
                # виджет месяцами стоял без обновлений. Админ узнаёт сам, один раз.
                if error_code in (5, 15, 27):
                    await _alert_once(
                        group_id, f"vk_{error_code}",
                        f"🏆 VK не принимает обновление виджета «Топ участников» "
                        f"(ошибка {error_code}: {error_msg}).\n"
                        f"Если не пройдёт и дальше — установите виджет заново: "
                        f"{widget_install_hint()}",
                    )
                    return False, (f"VK отклонил токен виджета (ошибка {error_code}). "
                                   "Нажмите «Установить виджет» заново")

                return False, f"VK API: {error_msg}"

            await set_setting(group_id, "widget_last_error", "")
            await set_setting(group_id, "widget_alerted", "")
            logger.info(f"Widget updated for group {group_id} ({shown} users)")
            return True, f"Обновлено ({shown} {_plural(shown, 'участник', 'участника', 'участников')})"

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
