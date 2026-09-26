"""Личный ключ админа через мини-приложение (VKWebAppGetAuthToken).

VK ID нашему приложению выдаёт только базовые права (vkid.personal_info) —
стену, фото и сообщества молча вычёркивает (25.09.2026). Мини-приложение
запрашивает ключ пользователя с wall/photos/groups через VK Bridge.
"""

import httpx
import pytest
from fastapi import FastAPI

import core.admin_key as admin_key
import web.miniapp.routes as mini
from core.crypto import encrypt_token
from core.vk_auth import create_miniapp_token
from database.service import create_group, get_setting

GID = 236517033
ADMIN = 309736634
_RealAsyncClient = httpx.AsyncClient


def _vk(monkeypatch, owner_id=ADMIN, admin_of=(GID,)):
    def handler(request):
        if request.url.path.endswith("/groups.get"):
            return httpx.Response(200, json={"response": {"count": len(admin_of), "items": list(admin_of)}})
        if request.url.path.endswith("/users.get"):
            return httpx.Response(200, json={"response": [
                {"id": owner_id, "first_name": "Ленар", "last_name": "Фатыхов"},
            ]})
        return httpx.Response(404)

    monkeypatch.setattr(admin_key, "_client",
                        lambda: _RealAsyncClient(transport=httpx.MockTransport(handler)))


def _client():
    app = FastAPI()
    app.include_router(mini.router)
    return _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _send_key(uid=ADMIN, **form):
    token = create_miniapp_token(uid, 0)
    async with _client() as c:
        return await c.post(
            f"/miniapp/admin-key?token={token}",
            data={"access_token": "user-token", "scope": "wall,photos,groups", **form},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )


@pytest.fixture
async def group(db):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)


async def test_start_page_offers_personal_key_button(group):
    token = create_miniapp_token(ADMIN, 0)
    async with _client() as c:
        html = (await c.get(f"/miniapp?token={token}")).text

    assert "connectAdminKey(" in html
    assert "VKWebAppGetAuthToken" in html
    assert "'wall,photos,groups'" in html


async def test_key_from_miniapp_is_verified_and_stored(group, monkeypatch):
    _vk(monkeypatch)

    r = await _send_key()

    assert r.status_code == 200 and r.json()["ok"]
    assert await admin_key.admin_token(GID) == "user-token"
    assert await admin_key.get_admin_api(GID) is not None  # не требует продления сразу


async def test_key_of_another_account_is_refused(group, monkeypatch):
    """В мини-приложение вошёл админ, а ключ — чужой: не принимаем."""
    _vk(monkeypatch, owner_id=777)

    r = await _send_key()

    assert r.status_code == 400
    assert await admin_key.admin_token(GID) == ""


async def test_key_without_needed_rights_is_refused(group, monkeypatch):
    _vk(monkeypatch)

    r = await _send_key(scope="friends")

    assert r.status_code == 400
    assert "Стена" in r.json()["error"]
    assert await admin_key.admin_token(GID) == ""


async def test_account_vk_does_not_confirm_as_admin_is_refused(group, monkeypatch):
    _vk(monkeypatch, owner_id=555, admin_of=())

    r = await _send_key(uid=555)

    assert r.status_code == 400
    assert await get_setting(GID, "admin_user_token", "") == ""


COADMIN = 424242


async def test_coadmin_connects_own_key(group, monkeypatch):
    """Группу подключал владелец, но свой ключ может добавить и другой админ —
    VK подтверждает его админство; бот тогда работает его ключом."""
    _vk(monkeypatch, owner_id=COADMIN, admin_of=(GID,))

    r = await _send_key(uid=COADMIN)

    assert r.status_code == 200 and r.json()["ok"]
    assert await get_setting(GID, "admin_user_id") == str(COADMIN)


async def test_admin_without_own_groups_sees_key_card(group):
    token = create_miniapp_token(COADMIN, 0)
    async with _client() as c:
        html = (await c.get(f"/miniapp?token={token}")).text

    assert "connectAdminKey(" in html


async def _page_for(uid):
    token = create_miniapp_token(uid, 0)
    async with _client() as c:
        return (await c.get(f"/miniapp?token={token}")).text


async def test_start_page_renews_expiring_key_by_itself(group):
    """Ключ из мини-приложения VK выдаёт на сутки: открыл приложение — продлили."""
    import time

    from database.service import set_setting

    await set_setting(GID, "admin_user_token", encrypt_token("user-token"))
    await set_setting(GID, "admin_user_id", str(ADMIN))
    await set_setting(GID, "admin_token_expires_at", str(int(time.time()) + 2 * 3600))

    assert "connectAdminKey(true)" in await _page_for(ADMIN)

    await set_setting(GID, "admin_token_expires_at", str(int(time.time()) + 20 * 3600))
    assert "connectAdminKey(true)" not in await _page_for(ADMIN)


async def test_no_auto_renew_for_someone_elses_key(group):
    import time

    from database.service import set_setting

    await set_setting(GID, "admin_user_token", encrypt_token("user-token"))
    await set_setting(GID, "admin_user_id", str(COADMIN))
    await set_setting(GID, "admin_token_expires_at", str(int(time.time()) + 3600))

    assert "connectAdminKey(true)" not in await _page_for(ADMIN)


async def test_key_requires_miniapp_session(group):
    async with _client() as c:
        r = await c.post("/miniapp/admin-key", data={"access_token": "x"})
    assert r.status_code == 401
