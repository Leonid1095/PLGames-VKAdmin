"""Личный ключ админа (VK ID): хранится зашифрованным и только там, где VK
подтвердил админство; живёт час и сам продлевается; умерший ключ бот
перестаёт использовать и сообщает об этом один раз.

Зачем ключ: ключ сообщества VK не пускает в фото к постам, удаление
комментариев, баны и закрепы (err 27) — бот слал админу ссылку «сделайте руками».
Почему VK ID: oauth.vk.com отвечает «Security Error» на любой запрос ключа
пользователя для нашего приложения (проверено 25.09.2026).
"""

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

import core.admin_key as admin_key
from core.crypto import decrypt_token, encrypt_token
from core.vk_read import VKReadError
from database.service import create_group, get_setting

GID = 236517033
GID2 = 240061584
ADMIN = 309736634


class _VK:
    """api.vk.com (groups.get/users.get) + id.vk.ru (продление пары ключей).
    refresh(n) — ответ на n-е продление: dict или исключение."""

    def __init__(self, monkeypatch, admin_of=(GID,), refresh=None):
        self.refresh_calls = []
        self.refresh = refresh or (lambda n: {
            "access_token": f"acc-{n + 2}", "refresh_token": f"ref-{n + 2}",
            "expires_in": 3600, "user_id": str(ADMIN), "scope": "wall photos groups",
        })

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "id.vk.ru":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                self.refresh_calls.append(form)
                answer = self.refresh(len(self.refresh_calls) - 1)
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(200, json=answer)
            if request.url.path.endswith("/groups.get"):
                return httpx.Response(200, json={"response": {"count": len(admin_of), "items": list(admin_of)}})
            if request.url.path.endswith("/users.get"):
                return httpx.Response(200, json={"response": [
                    {"id": ADMIN, "first_name": "Ленар", "last_name": "Фатыхов"},
                ]})
            return httpx.Response(404)

        monkeypatch.setattr(
            admin_key, "_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        monkeypatch.setattr(admin_key.settings, "VK_APP_ID", "54477693")


def _tokens(access="acc-1", refresh="ref-1", expires_in=3600):
    return admin_key.TokenSet(
        access_token=access, refresh_token=refresh, device_id="dev-1",
        expires_in=expires_in, user_id=ADMIN, scope="wall photos groups",
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


# ─── Подключение / отключение ────────────────────────────────────────────────

async def test_connect_stores_encrypted_keys_only_where_vk_confirms_admin(db, monkeypatch):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID])

    connected = await admin_key.connect_admin_key(_tokens())

    assert connected == [(GID, "WOW")]
    for key, plain in (("admin_user_token", "acc-1"), ("admin_refresh_token", "ref-1")):
        stored = await get_setting(GID, key)
        assert stored and plain not in stored and decrypt_token(stored) == plain
    assert await get_setting(GID, "admin_device_id") == "dev-1"
    assert await get_setting(GID, "admin_user_id") == str(ADMIN)
    assert await get_setting(GID, "admin_user_name") == "Ленар Фатыхов"
    assert await get_setting(GID2, "admin_user_token", "") == ""
    assert await admin_key.get_admin_api(GID) is not None
    assert await admin_key.get_admin_api(GID2) is None


async def test_connect_refuses_account_that_admins_none_of_our_groups(db, monkeypatch):
    await _two_groups()
    _VK(monkeypatch, admin_of=[999])

    with pytest.raises(admin_key.AdminKeyError):
        await admin_key.connect_admin_key(_tokens())

    assert await get_setting(GID, "admin_user_token", "") == ""
    assert await get_setting(GID2, "admin_user_token", "") == ""


async def test_disconnect_forgets_key(db, monkeypatch):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID, GID2])
    await admin_key.connect_admin_key(_tokens())

    await admin_key.disconnect_admin_key(GID)

    assert await admin_key.get_admin_api(GID) is None
    assert await admin_key.admin_token(GID) == ""
    assert await get_setting(GID, "admin_refresh_token", "") == ""
    assert await admin_key.get_admin_api(GID2) is not None  # другая группа не задета


# ─── Умерший ключ ────────────────────────────────────────────────────────────

async def test_dead_key_is_reported_once_and_no_longer_used(db, monkeypatch, notes):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key(_tokens())

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
    _VK(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key(_tokens())

    await admin_key.report_admin_key_failure(GID, VKReadError(15, "Access denied"))

    assert notes == []
    assert await admin_key.get_admin_api(GID) is not None


async def test_reconnect_revives_dead_key(db, monkeypatch, notes):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key(_tokens("old", "old-ref"))
    await admin_key.report_admin_key_failure(GID, VKReadError(5, "User authorization failed"))

    await admin_key.connect_admin_key(_tokens("new", "new-ref"))

    assert await get_setting(GID, "admin_key_error") == ""
    assert await admin_key.admin_token(GID) == "new"
    assert await admin_key.get_admin_api(GID) is not None


# ─── Продление: ключ VK ID живёт час ─────────────────────────────────────────

async def test_valid_key_is_used_without_refresh(db, monkeypatch):
    await _two_groups()
    vk = _VK(monkeypatch, admin_of=[GID])
    await admin_key.connect_admin_key(_tokens(expires_in=3600))

    assert await admin_key.fresh_token(GID) == "acc-1"
    assert vk.refresh_calls == []


async def test_expiring_key_is_refreshed_for_every_group_of_the_admin(db, monkeypatch):
    """После продления старая пара недействительна — копия в другой группе
    со старым refresh-токеном была бы мертва."""
    await _two_groups()
    vk = _VK(monkeypatch, admin_of=[GID, GID2])
    await admin_key.connect_admin_key(_tokens(expires_in=60))

    assert await admin_key.fresh_token(GID) == "acc-2"

    call = vk.refresh_calls[0]
    assert call["grant_type"] == "refresh_token"
    assert call["refresh_token"] == "ref-1"
    assert call["device_id"] == "dev-1"
    assert call["client_id"] == "54477693"
    assert len(call["state"]) >= 32
    assert await admin_key.admin_token(GID2) == "acc-2"
    assert decrypt_token(await get_setting(GID2, "admin_refresh_token")) == "ref-2"
    assert await admin_key.fresh_token(GID2) == "acc-2"
    assert len(vk.refresh_calls) == 1


async def test_concurrent_callers_refresh_once(db, monkeypatch):
    await _two_groups()
    vk = _VK(monkeypatch, admin_of=[GID, GID2])
    await admin_key.connect_admin_key(_tokens(expires_in=60))

    results = await asyncio.gather(
        admin_key.fresh_token(GID), admin_key.fresh_token(GID2), admin_key.fresh_token(GID),
    )

    assert results == ["acc-2", "acc-2", "acc-2"]
    assert len(vk.refresh_calls) == 1


async def test_rejected_refresh_kills_key_and_alerts_once(db, monkeypatch, notes):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID, GID2], refresh=lambda n: {
        "error": "invalid_grant", "error_description": "refresh token is revoked",
    })
    await admin_key.connect_admin_key(_tokens(expires_in=60))

    assert await admin_key.get_admin_api(GID) is None
    assert await admin_key.get_admin_api(GID2) is None  # общий refresh — умер для обеих
    assert "invalid_grant" in await get_setting(GID, "admin_key_error")
    assert len(notes) == 1


@pytest.mark.parametrize("failure", [
    {"error": "server_error", "error_description": "try later"},
    httpx.ConnectTimeout("VK ID не ответил"),
])
async def test_transient_refresh_failure_keeps_key(db, monkeypatch, notes, failure):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID], refresh=lambda n: failure)
    await admin_key.connect_admin_key(_tokens(expires_in=120))  # ещё жив, но пора продлевать

    assert await admin_key.fresh_token(GID) == "acc-1"
    assert await get_setting(GID, "admin_key_error") == ""
    assert notes == []


async def test_transient_failure_with_expired_key_gives_no_key_but_does_not_kill_it(db, monkeypatch, notes):
    await _two_groups()
    _VK(monkeypatch, admin_of=[GID], refresh=lambda n: {"error": "temporarily_unavailable"})
    await admin_key.connect_admin_key(_tokens(expires_in=-10))

    assert await admin_key.get_admin_api(GID) is None
    assert await get_setting(GID, "admin_key_error") == ""
    assert notes == []
