"""«Подключить личный ключ админа»: тот же экран VK и тот же redirect_uri,
что у подключения групп, но свой state и свои права. Групповой поток не задет."""

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
STATE = "admin-state-123"
_RealAsyncClient = httpx.AsyncClient  # до подмены в _vk


def _vk(monkeypatch, exchange=None, admin_of=(GID,)):
    """oauth.vk.com меняет код на ключ; API отвечает, где владелец ключа — админ."""
    exchange = exchange or {"access_token": "user-token", "expires_in": 0, "user_id": ADMIN}

    def oauth_handler(request):
        if request.url.host == "oauth.vk.com":
            return httpx.Response(200, json=exchange)
        return httpx.Response(404)

    def api_handler(request):
        if request.url.path.endswith("/groups.get"):
            return httpx.Response(200, json={"response": {"count": len(admin_of), "items": list(admin_of)}})
        if request.url.path.endswith("/users.get"):
            return httpx.Response(200, json={"response": [
                {"id": ADMIN, "first_name": "Ленар", "last_name": "Фатыхов"},
            ]})
        return httpx.Response(404)

    monkeypatch.setattr(
        oauth.httpx, "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(oauth_handler)),
    )
    monkeypatch.setattr(
        admin_key, "_client",
        lambda: _RealAsyncClient(transport=httpx.MockTransport(api_handler)),
    )


def _client(cookies: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(oauth.router)
    return _RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", cookies=cookies,
    )


def _owner(**extra) -> dict:
    return {COOKIE_NAME: _get_session_token(), **extra}


async def _key() -> str:
    stored = await get_setting(GID, "admin_user_token", "")
    return decrypt_token(stored) if stored else ""


async def test_start_requires_dashboard_login():
    async with _client({}) as c:
        r = await c.get("/api/vk/admin-oauth")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"


async def test_start_sends_owner_to_vk_with_admin_rights(monkeypatch):
    monkeypatch.setattr(oauth.settings, "VK_APP_ID", "54475361")
    async with _client(_owner()) as c:
        r = await c.get("/api/vk/admin-oauth")

    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.netloc, url.path) == ("oauth.vk.com", "/authorize")
    assert q["scope"] == "wall,photos,groups,offline"
    assert q["response_type"] == "code"
    assert q["revoke"] == "1"  # иначе VK может отдать закэшированный отозванный ключ
    assert q["redirect_uri"].endswith("/api/vk/callback")
    assert "group_ids" not in q
    assert r.cookies.get("vkadmin_admin_oauth_state") == q["state"]


async def test_code_from_vk_connects_admin_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        r = await c.get(f"/api/vk/callback?code=c0de&state={STATE}")

    assert r.status_code == 200
    assert "WOW" in r.text
    assert "user-token" not in r.text
    assert await _key() == "user-token"
    group = await get_group(GID)
    assert decrypt_token(group.access_token) == "group-token"  # групповой ключ не тронут


async def test_admin_flow_needs_dashboard_login(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    async with _client({"vkadmin_admin_oauth_state": STATE}) as c:
        r = await c.get(f"/api/vk/callback?code=c0de&state={STATE}")

    assert r.status_code == 303
    assert await _key() == ""


async def test_foreign_state_does_not_touch_admin_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        r = await c.get("/api/vk/callback?code=c0de&state=theirs")

    assert r.status_code in (400, 403)
    assert await _key() == ""


async def test_vk_refusing_code_exchange_shows_reason_and_saves_nothing(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch, exchange={"error": "invalid_grant",
                               "error_description": "Code is invalid or expired."})

    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        r = await c.get(f"/api/vk/callback?code=c0de&state={STATE}")

    assert r.status_code == 400
    assert "Code is invalid or expired." in r.text
    assert await _key() == ""


async def test_account_not_admin_of_our_groups_is_refused(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch, admin_of=(999,))

    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        r = await c.get(f"/api/vk/callback?code=c0de&state={STATE}")

    assert r.status_code == 400
    assert "администратор" in r.text
    assert await _key() == ""


async def test_key_in_fragment_connects_admin_key(db, monkeypatch):
    """Standalone-приложение VK отдаёт ключ во #фрагменте; JS-извлекатель
    пересылает его на /api/vk/callback/token."""
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        r = await c.get(
            f"/api/vk/callback/token?access_token=user-token&user_id={ADMIN}&state={STATE}",
        )

    assert r.status_code == 200
    assert await _key() == "user-token"


# ─── VK ответил не так, как ждали: страница с причиной, а не голая 500 ───────

def _vk_raw(monkeypatch, oauth_handler, api_handler):
    monkeypatch.setattr(
        oauth.httpx, "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(oauth_handler)),
    )
    monkeypatch.setattr(
        admin_key, "_client",
        lambda: _RealAsyncClient(transport=httpx.MockTransport(api_handler)),
    )


def _exchange_ok(request):
    return httpx.Response(200, json={"access_token": "user-token", "expires_in": 0, "user_id": ADMIN})


async def _connect_via_code() -> httpx.Response:
    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        return await c.get(f"/api/vk/callback?code=c0de&state={STATE}")


async def test_empty_users_get_shows_page_not_500(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)

    def api(request):
        if request.url.path.endswith("/groups.get"):
            return httpx.Response(200, json={"response": {"count": 1, "items": [GID]}})
        return httpx.Response(200, json={"response": []})

    _vk_raw(monkeypatch, _exchange_ok, api)

    r = await _connect_via_code()

    assert r.status_code == 400
    assert "панель" in r.text  # ссылка назад, а не Internal Server Error
    assert await _key() == ""


async def test_vk_api_answering_html_shows_page_not_500(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk_raw(monkeypatch, _exchange_ok,
            lambda request: httpx.Response(502, text="<html>Bad Gateway</html>"))

    r = await _connect_via_code()

    assert r.status_code == 400
    assert await _key() == ""


async def test_oauth_timeout_shows_page_not_500(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)

    def timeout(request):
        raise httpx.ConnectTimeout("VK не ответил")

    _vk_raw(monkeypatch, timeout, timeout)

    r = await _connect_via_code()

    assert r.status_code == 400
    assert await _key() == ""


# ─── Ключ из #фрагмента не должен попадать в адрес (логи nginx/uvicorn, история) ──

async def test_fragment_extractor_sends_token_in_post_body():
    async with _client({}) as c:
        html = (await c.get("/api/vk/callback")).text

    assert "'/api/vk/callback/token?'" not in html  # раньше: GET с ключом в адресе
    assert "form.method = 'POST'" in html
    assert "history.replaceState" in html


async def test_fragment_token_posted_in_body_connects_admin_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    _vk(monkeypatch)

    async with _client(_owner(vkadmin_admin_oauth_state=STATE)) as c:
        r = await c.post("/api/vk/callback/token", data={
            "access_token": "user-token", "user_id": str(ADMIN), "state": STATE,
        })

    assert r.status_code == 200
    assert await _key() == "user-token"
