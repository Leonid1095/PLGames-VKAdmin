"""Карточка «Ключи и доступы» в дашборде: какой ключ работает и где взять новый.

Зачем: ключей у бота несколько (сообщества, сервисный, виджета), и каждый раз
при поломке владелец заново искал, какой из них умер и где его получить.
Карточка проверяет ключи живым запросом к VK и пишет, куда нажать.
"""

import httpx

from core import key_status, vk_read
from core.config import settings
from core.crypto import encrypt_token
from database.service import create_group, set_setting
from tests.test_dashboard_stats import _client as dashboard_client

GID = 236517033

_PERMS_GROUP = {"response": {"mask": 12292, "permissions": [
    {"name": "messages", "setting": 4096}, {"name": "wall", "setting": 8192},
]}}
_PERMS_WIDGET = {"response": {"mask": 64, "permissions": [
    {"name": "app_widget", "setting": 64},
]}}


def _mock_vk(monkeypatch, by_token: dict):
    """Ответ VK выбирается по access_token запроса — так видно, каким ключом звали."""
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.method == "POST":
            params = httpx.QueryParams(request.content.decode())
        return httpx.Response(200, json=by_token[params["access_token"]])

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    monkeypatch.setattr(key_status, "_client", factory)
    monkeypatch.setattr(vk_read, "_client", factory)


def _row(html: str, key: str) -> str:
    start = html.index(f'data-key="{key}"')
    return html[start:html.index("</tr>", start)]


async def test_card_shows_live_status_of_every_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await set_setting(GID, "widget_enabled", "true")
    await set_setting(GID, "widget_token", "widget-token")
    _mock_vk(monkeypatch, {
        "group-token": _PERMS_GROUP,
        "svc-test-key": {"response": {"count": 5, "items": []}},
        "widget-token": _PERMS_WIDGET,
    })

    async with dashboard_client() as c:
        html = (await c.get(f"/dashboard/group/{GID}")).text

    assert "Ключи и доступы" in html
    assert 'data-state="ok"' in _row(html, "group")
    assert "messages" in _row(html, "group")
    assert 'data-state="ok"' in _row(html, "service")
    assert 'data-state="ok"' in _row(html, "widget")
    assert 'data-state="off"' in _row(html, "admin")


async def test_card_says_where_to_get_missing_widget_token(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await set_setting(GID, "widget_enabled", "true")
    _mock_vk(monkeypatch, {
        "group-token": _PERMS_GROUP,
        "svc-test-key": {"response": {"count": 0, "items": []}},
    })

    async with dashboard_client() as c:
        html = (await c.get(f"/dashboard/group/{GID}")).text

    row = _row(html, "widget")
    assert 'data-state="missing"' in row
    assert "Установить виджет" in row


async def test_card_shows_vk_rejecting_group_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    _mock_vk(monkeypatch, {
        "group-token": {"error": {"error_code": 5, "error_msg": "User authorization failed"}},
        "svc-test-key": {"response": {"count": 0, "items": []}},
    })

    async with dashboard_client() as c:
        html = (await c.get(f"/dashboard/group/{GID}")).text

    row = _row(html, "group")
    assert 'data-state="fail"' in row
    assert "ошибка 5" in row
    assert "Работа с API" in row


async def test_widget_token_without_app_widget_right_is_flagged(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await set_setting(GID, "widget_enabled", "true")
    await set_setting(GID, "widget_token", "widget-token")
    _mock_vk(monkeypatch, {
        "group-token": _PERMS_GROUP,
        "svc-test-key": {"response": {"count": 0, "items": []}},
        "widget-token": _PERMS_GROUP,  # токен есть, но без app_widget
    })

    statuses = {s.key: s for s in await key_status.check_group_keys(GID)}

    assert statuses["widget"].state == "fail"
    assert "app_widget" in statuses["widget"].detail


# ─── Личный ключ админа: подключить / отключить из карточки ──────────────────

_BASE_KEYS = {
    "group-token": _PERMS_GROUP,
    "svc-test-key": {"response": {"count": 5, "items": []}},
}
_OWNER = {"response": [{"id": 309736634, "first_name": "Ленар", "last_name": "Фатыхов"}]}


async def _with_admin_key(**extra):
    await set_setting(GID, "admin_user_token", encrypt_token("admin-token"))
    await set_setting(GID, "admin_user_id", "309736634")
    await set_setting(GID, "admin_user_name", "Ленар Фатыхов")
    await set_setting(GID, "admin_token_expires_at", "9999999999")  # ключ VK ID ещё жив
    for key, value in extra.items():
        await set_setting(GID, key, value)


async def _admin_row(monkeypatch) -> str:
    _mock_vk(monkeypatch, {**_BASE_KEYS, "admin-token": _OWNER})
    async with dashboard_client() as c:
        html = (await c.get(f"/dashboard/group/{GID}")).text
    return _row(html, "admin")


async def test_no_admin_key_offers_connect_button(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)

    row = await _admin_row(monkeypatch)

    assert 'data-state="off"' in row
    # VK ID даёт только базовые права — подключаем в мини-приложении
    assert f'href="https://vk.com/app{settings.VK_MINIAPP_ID}"' in row


async def test_connected_admin_key_is_checked_live_and_can_be_disconnected(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await _with_admin_key()

    row = await _admin_row(monkeypatch)

    assert 'data-state="ok"' in row
    assert "Ленар Фатыхов" in row
    assert f'action="/dashboard/group/{GID}/admin-key/disconnect"' in row
    assert 'name="_csrf"' in row
    assert "admin-token" not in row


async def test_dead_admin_key_is_flagged_with_reconnect(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await _with_admin_key(admin_key_error="VK error 5: User authorization failed")

    row = await _admin_row(monkeypatch)

    assert 'data-state="fail"' in row
    assert "User authorization failed" in row
    # VK ID даёт только базовые права — подключаем в мини-приложении
    assert f'href="https://vk.com/app{settings.VK_MINIAPP_ID}"' in row


async def test_disconnect_requires_csrf(db):
    from core.admin_key import admin_token

    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await _with_admin_key()

    async with dashboard_client() as c:
        await c.post(f"/dashboard/group/{GID}/admin-key/disconnect", data={"_csrf": "wrong"})
    assert await admin_token(GID) == "admin-token"

    async with dashboard_client() as c:
        await c.post(f"/dashboard/group/{GID}/admin-key/disconnect", data={"_csrf": "csrf-x"})
    assert await admin_token(GID) == ""


async def test_rendered_disconnect_form_actually_disconnects(db, monkeypatch):
    """Кнопка из настоящей страницы, а не рукописный токен: раньше форма
    заворачивала готовое поле _csrf во второе, и «Отключить» молча не работало."""
    import html as html_lib
    import re

    from core.admin_key import admin_token

    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await _with_admin_key()
    _mock_vk(monkeypatch, {**_BASE_KEYS, "admin-token": _OWNER})

    async with dashboard_client() as c:
        row = _row((await c.get(f"/dashboard/group/{GID}")).text, "admin")
        form = re.search(r'<form[^>]*admin-key/disconnect.*?</form>', row, re.S).group(0)
        fields = {
            m.group(1): html_lib.unescape(m.group(2))
            for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form)
        }
        await c.post(f"/dashboard/group/{GID}/admin-key/disconnect", data=fields)

    assert await admin_token(GID) == ""


async def test_expired_key_that_vk_id_cannot_refresh_now_is_not_killed(db, monkeypatch):
    """Ключ VK ID живёт час; если продлить сейчас не вышло (VK ID молчит),
    карточка честно говорит «истёк», но ключ не хоронит."""
    from core.admin_key import admin_token
    from database.service import get_setting

    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await _with_admin_key(admin_token_expires_at="0",
                          admin_refresh_token=encrypt_token("ref"), admin_device_id="dev")

    row = await _admin_row(monkeypatch)  # id.vk.ru в тестах недоступен (conftest)

    assert 'data-state="fail"' in row
    assert "истёк" in row
    assert await get_setting(GID, "admin_key_error") == ""
    assert await admin_token(GID) == "admin-token"


async def test_expired_miniapp_key_says_open_miniapp_to_renew(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    await _with_admin_key(admin_token_expires_at="0")  # без refresh — ключ из мини-приложения

    row = await _admin_row(monkeypatch)

    assert 'data-state="fail"' in row
    assert "мини-приложение" in row and "продл" in row
