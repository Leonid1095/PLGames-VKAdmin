"""Личный ключ админа: хранится зашифрованным и только там, где VK подтвердил
админство; умерший ключ бот перестаёт использовать и сообщает об этом один раз.

Зачем ключ: ключ сообщества VK не пускает в фото к постам, удаление
комментариев, баны и закрепы (err 27) — бот слал админу ссылку «сделайте руками».
"""

import httpx
import pytest

import core.admin_key as admin_key
from core.crypto import decrypt_token, encrypt_token
from core.vk_read import VKReadError
from database.service import create_group, get_setting

GID = 236517033
GID2 = 240061584
ADMIN = 309736634


def _vk(monkeypatch, admin_of):
    """groups.get(filter=admin) отвечает admin_of, users.get — именем владельца."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/groups.get"):
            return httpx.Response(200, json={"response": {"count": len(admin_of), "items": admin_of}})
        if request.url.path.endswith("/users.get"):
            return httpx.Response(200, json={"response": [
                {"id": ADMIN, "first_name": "Ленар", "last_name": "Фатыхов"},
            ]})
        return httpx.Response(404)

    monkeypatch.setattr(
        admin_key, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def notes(monkeypatch):
    """ЛС менеджерам — перехватываем, в VK не шлём."""
    sent = []

    async def fake_notify(group_id, text):
        sent.append((group_id, text))
        return True

    monkeypatch.setattr(admin_key, "_notify_admin", fake_notify)
    return sent


async def _two_groups():
    await create_group(GID, "WOW", encrypt_token("g"), ADMIN)
    await create_group(GID2, "Twitch", encrypt_token("g"), ADMIN)


async def test_connect_stores_encrypted_key_only_where_vk_confirms_admin(db, monkeypatch):
    await _two_groups()
    _vk(monkeypatch, admin_of=[GID])

    connected = await admin_key.connect_admin_key("user-token", ADMIN)

    assert connected == [(GID, "WOW")]
    stored = await get_setting(GID, "admin_user_token")
    assert stored and "user-token" not in stored
    assert decrypt_token(stored) == "user-token"
    assert await get_setting(GID, "admin_user_id") == str(ADMIN)
    assert await get_setting(GID, "admin_user_name") == "Ленар Фатыхов"
    assert await get_setting(GID2, "admin_user_token", "") == ""
    assert await admin_key.get_admin_api(GID) is not None
    assert await admin_key.get_admin_api(GID2) is None


async def test_connect_refuses_account_that_admins_none_of_our_groups(db, monkeypatch):
    await _two_groups()
    _vk(monkeypatch, admin_of=[999])

    with pytest.raises(admin_key.AdminKeyError):
        await admin_key.connect_admin_key("user-token", ADMIN)

    assert await get_setting(GID, "admin_user_token", "") == ""
    assert await get_setting(GID2, "admin_user_token", "") == ""


async def test_dead_key_is_reported_once_and_no_longer_used(db, monkeypatch, notes):
    await _two_groups()
    _vk(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key("user-token", ADMIN)

    dead = VKReadError(5, "User authorization failed: invalid access_token")
    await admin_key.report_admin_key_failure(GID, dead)
    await admin_key.report_admin_key_failure(GID, dead)  # следующая попытка — без спама

    assert len(notes) == 1
    assert "Подключить" in notes[0][1]
    assert "invalid access_token" in await get_setting(GID, "admin_key_error")
    assert await admin_key.get_admin_api(GID) is None


async def test_other_vk_errors_do_not_kill_key(db, monkeypatch, notes):
    """Нет прав на конкретный комментарий, человек уже в бане — ключ жив."""
    await _two_groups()
    _vk(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key("user-token", ADMIN)

    await admin_key.report_admin_key_failure(GID, VKReadError(15, "Access denied"))

    assert notes == []
    assert await admin_key.get_admin_api(GID) is not None


async def test_reconnect_revives_dead_key(db, monkeypatch, notes):
    await _two_groups()
    _vk(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key("old-token", ADMIN)
    await admin_key.report_admin_key_failure(GID, VKReadError(5, "User authorization failed"))

    await admin_key.connect_admin_key("new-token", ADMIN)

    assert await get_setting(GID, "admin_key_error") == ""
    assert await admin_key.admin_token(GID) == "new-token"
    assert await admin_key.get_admin_api(GID) is not None


async def test_disconnect_forgets_key(db, monkeypatch):
    await _two_groups()
    _vk(monkeypatch, admin_of=[GID, GID2])
    await admin_key.connect_admin_key("user-token", ADMIN)

    await admin_key.disconnect_admin_key(GID)

    assert await admin_key.get_admin_api(GID) is None
    assert await admin_key.admin_token(GID) == ""
    assert await admin_key.get_admin_api(GID2) is not None  # другая группа не задета
