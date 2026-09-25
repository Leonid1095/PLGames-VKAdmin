"""«Подключить личный ключ админа»: вход через VK ID (OAuth 2.1 + PKCE), тот
же redirect_uri, что у подключения групп, но свой state и свои права.

oauth.vk.com для нашего приложения отвечает «Security Error» на любой запрос
ключа пользователя (проверено 25.09.2026), VK ID — принимает.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI

import core.group_setup  # noqa: F401 — openai подклассирует httpx.AsyncClient при импорте: грузим до подмены
import core.admin_key as admin_key
import web.oauth as oauth
from core.auth import COOKIE_NAME, _get_session_token
from core.crypto import decrypt_token, encrypt_token
from database.service import create_group, get_group, get_setting

GID = 236517033
ADMIN = 309736634
STATE = "admin-state-0123456789abcdefghijklmnopq"
VERIFIER = "v" * 64
_RealAsyncClient = httpx.AsyncClient


def _vk(monkeypatch, exchange=None, admin_of=(GID,), users=None):
    """id.vk.ru меняет код на пару ключей; API отвечает, где владелец — админ.
    exchange: dict-ответ VK ID или исключение. Запросы к VK ID — в возвращаемом списке."""
    calls = []
    exchange = exchange if exchange is not None else {
        "access_token": "user-token", "refresh_token": "refresh-token",
        "expires_in": 3600, "user_id": str(ADMIN), "scope": "wall photos groups",
    }
    users = users if users is not None else [{"id": ADMIN, "first_name": "Ленар", "last_name": "Фатыхов"}]

    def handler(request):
        if request.url.host == "id.vk.ru":
            calls.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            if isinstance(exchange, Exception):
                raise exchange
            return httpx.Response(200, json=exchange)
        if request.url.path.endswith("/groups.get"):
            return httpx.Response(200, json={"response": {"count": len(admin_of), "items": list(admin_of)}})
        if request.url.path.endswith("/users.get"):
            return httpx.Response(200, json={"response": users})
        return httpx.Response(404)

    monkeypatch.setattr(
        admin_key, "_client",
        lambda: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(admin_key.settings, "VK_APP_ID", "54477693")
    return calls


def _client(cookies: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(oauth.router)
    return _RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", cookies=cookies,
    )


def _owner(**extra) -> dict:
    return {COOKIE_NAME: _get_session_token(),
            "vkadmin_admin_oauth_state": STATE, "vkadmin_admin_pkce": VERIFIER, **extra}


async def _key() -> str:
    stored = await get_setting(GID, "admin_user_token", "")
    return decrypt_token(stored) if stored else ""


async def _return_from_vk(cookies=None, query=f"code=c0de&device_id=dev-1&state={STATE}"):
    async with _client(cookies if cookies is not None else _owner()) as c:
        return await c.get(f"/api/vk/callback?{query}")


# ─── Старт ───────────────────────────────────────────────────────────────────

async def test_start_requires_dashboard_login():
    async with _client({}) as c:
        r = await c.get("/api/vk/admin-oauth")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"


async def test_start_sends_owner_to_vk_id_with_pkce(monkeypatch):
    monkeypatch.setattr(oauth.settings, "VK_APP_ID", "54477693")
    async with _client({COOKIE_NAME: _get_session_token()}) as c:
        r = await c.get("/api/vk/admin-oauth")

    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.netloc, url.path) == ("id.vk.ru", "/authorize")
    assert q["response_type"] == "code"
    assert q["client_id"] == "54477693"
    assert q["redirect_uri"].endswith("/api/vk/callback")
    assert set(q["scope"].split()) == {"wall", "photos", "groups"}
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 32
    assert r.cookies.get("vkadmin_admin_oauth_state") == q["state"]
    verifier = r.cookies.get("vkadmin_admin_pkce")
    assert 43 <= len(verifier) <= 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == challenge


# ─── Возврат от VK ID ────────────────────────────────────────────────────────

async def test_code_from_vk_id_connects_admin_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    calls = _vk(monkeypatch)

    r = await _return_from_vk()

    assert r.status_code == 200
    assert "WOW" in r.text
    assert "user-token" not in r.text and "refresh-token" not in r.text
    assert await _key() == "user-token"
    assert decrypt_token(await get_setting(GID, "admin_refresh_token")) == "refresh-token"
    assert await get_setting(GID, "admin_device_id") == "dev-1"
    exchange = calls[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "c0de"
    assert exchange["code_verifier"] == VERIFIER
    assert exchange["device_id"] == "dev-1"
    assert exchange["state"] == STATE
    group = await get_group(GID)
    assert decrypt_token(group.access_token) == "group-token"  # групповой ключ не тронут


async def test_admin_flow_needs_dashboard_login(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    r = await _return_from_vk({"vkadmin_admin_oauth_state": STATE, "vkadmin_admin_pkce": VERIFIER})

    assert r.status_code == 303
    assert await _key() == ""


async def test_foreign_state_does_not_touch_admin_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    r = await _return_from_vk(query="code=c0de&device_id=dev-1&state=theirs")

    assert r.status_code in (400, 403)
    assert await _key() == ""


async def test_return_without_device_id_or_verifier_is_refused(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    no_device = await _return_from_vk(query=f"code=c0de&state={STATE}")
    no_verifier = await _return_from_vk({COOKIE_NAME: _get_session_token(),
                                         "vkadmin_admin_oauth_state": STATE})

    assert no_device.status_code == 400 and no_verifier.status_code == 400
    assert await _key() == ""


async def test_vk_id_refusing_exchange_shows_reason_and_saves_nothing(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch, exchange={"error": "invalid_grant", "error_description": "Code is invalid or expired."})

    r = await _return_from_vk()

    assert r.status_code == 400
    assert "Code is invalid or expired." in r.text
    assert await _key() == ""


async def test_vk_id_error_in_return_url_is_shown(db, monkeypatch):
    """Владелец нажал «Отмена» — VK ID возвращает error в адресе."""
    _vk(monkeypatch)

    r = await _return_from_vk(query=f"error=access_denied&error_description=User+denied&state={STATE}")

    assert r.status_code == 400
    assert "User denied" in r.text


async def test_account_not_admin_of_our_groups_is_refused(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch, admin_of=(999,))

    r = await _return_from_vk()

    assert r.status_code == 400
    assert "администратор" in r.text
    assert await _key() == ""


# ─── VK ответил не так, как ждали: страница с причиной, а не голая 500 ───────

async def test_empty_users_get_shows_page_not_500(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch, users=[])

    r = await _return_from_vk()

    assert r.status_code == 400
    assert "панель" in r.text  # ссылка назад, а не Internal Server Error
    assert await _key() == ""


async def test_vk_id_timeout_shows_page_not_500(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch, exchange=httpx.ConnectTimeout("VK ID не ответил"))

    r = await _return_from_vk()

    assert r.status_code == 400
    assert await _key() == ""


# ─── Ключ из #фрагмента не должен попадать в адрес (логи nginx/uvicorn, история) ──

async def test_fragment_extractor_sends_token_in_post_body():
    async with _client({}) as c:
        html = (await c.get("/api/vk/callback")).text

    assert "'/api/vk/callback/token?'" not in html  # раньше: GET с ключом в адресе
    assert "form.method = 'POST'" in html
    assert "history.replaceState" in html


async def test_token_callback_no_longer_takes_admin_keys(db, monkeypatch):
    """VK ID отдаёт код в адресе; ключ во фрагменте — не наш путь: без
    продления такой ключ умер бы через час."""
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    async with _client(_owner()) as c:
        r = await c.post("/api/vk/callback/token", data={
            "access_token": "user-token", "user_id": str(ADMIN), "state": STATE,
        })

    assert r.status_code in (400, 403)
    assert await _key() == ""
