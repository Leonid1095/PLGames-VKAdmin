"""OAuth: готовые токены принимаются только в рамках начатого нами OAuth.

/api/vk/callback/token — GET, а сессионная кука SameSite=Lax уходит при
переходе по ссылке с чужого сайта: одна ссылка, открытая залогиненным
владельцем, подменяла токен и админа любой группы (CSRF).
"""

import httpx
from fastapi import FastAPI

import core.group_setup  # noqa: F401 — openai подклассирует httpx.AsyncClient при импорте: грузим до подмены
import web.oauth as oauth
from core.auth import COOKIE_NAME, _get_session_token
from core.crypto import encrypt_token
from database.service import create_group, get_group

GID = 236517033
_RealAsyncClient = httpx.AsyncClient  # до подмены в _vk_accepts_any_token


def _vk_accepts_any_token(monkeypatch):
    """Атакующий подсовывает ВАЛИДНЫЙ токен (свой) — VK его принимает."""
    def handler(request):
        if "groups.getById" in request.url.path:
            return httpx.Response(200, json={"response": {"groups": [{"id": GID, "name": "WOW"}]}})
        return httpx.Response(200, json={"response": {"code": "abc123"}})

    monkeypatch.setattr(
        oauth.httpx, "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def noop(*a, **kw):
        return True

    monkeypatch.setattr(oauth, "_setup_callback_api", noop)
    monkeypatch.setattr("core.group_setup.setup_group_ai", noop)


def _client(cookies: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(oauth.router)
    return _RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", cookies=cookies,
    )


async def test_token_callback_without_state_is_rejected(db, monkeypatch):
    _vk_accepts_any_token(monkeypatch)
    await create_group(GID, "WOW", encrypt_token("legit-token"), 309736634, secret_key="s")

    async with _client({COOKIE_NAME: _get_session_token()}) as c:
        resp = await c.get(
            f"/api/vk/callback/token?access_token_{GID}=attacker-token&user_id=666",
        )

    assert resp.status_code in (400, 403)
    group = await get_group(GID)
    assert group.admin_vk_id == 309736634
    assert group.secret_key == "s"


async def test_token_callback_with_foreign_state_is_rejected(db, monkeypatch):
    _vk_accepts_any_token(monkeypatch)
    await create_group(GID, "WOW", encrypt_token("legit-token"), 309736634, secret_key="s")

    async with _client({
        COOKIE_NAME: _get_session_token(), "vkadmin_oauth_state": "ours",
    }) as c:
        resp = await c.get(
            f"/api/vk/callback/token?access_token_{GID}=x&user_id=666&state=theirs",
        )

    assert resp.status_code in (400, 403)
    assert (await get_group(GID)).admin_vk_id == 309736634


async def test_fragment_extractor_forwards_state():
    """JS-извлекатель из #fragment обязан передать state дальше — иначе
    легитимный поток упрётся в проверку state."""
    async with _client({}) as c:
        html = (await c.get("/api/vk/callback")).text
    assert "'/api/vk/callback?code=' + code" not in html
    assert "window.location.hash.substring(1)" in html


async def test_reconnect_reuses_existing_secret(db):
    """Секрет в БД не должен расходиться с сервером в VK: раньше новый секрет
    писался в БД до (медленной) перенастройки VK — события в этом окне, а при
    сбое editCallbackServer навсегда, отбрасывались как «Invalid secret»."""
    await create_group(GID, "WOW", encrypt_token("t"), 1, secret_key="keep-me")
    assert await oauth._stable_secret(GID) == "keep-me"
    fresh = await oauth._stable_secret(999)
    assert len(fresh) == 32 and fresh != "keep-me"


async def test_callback_setup_reports_vk_errors(monkeypatch):
    def handler(request):
        if "getCallbackServers" in request.url.path:
            return httpx.Response(200, json={"response": {"count": 0, "items": []}})
        return httpx.Response(200, json={"error": {"error_code": 15, "error_msg": "Access denied"}})

    monkeypatch.setattr(
        oauth.httpx, "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await oauth._setup_callback_api("tok", GID, "sec") is False
