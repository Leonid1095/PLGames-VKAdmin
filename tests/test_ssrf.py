"""SSRF: внутренние адреса недоступны ни напрямую, ни через редирект.

Раньше is_safe_public_url проверял только первый URL, а httpx сам шёл по
редиректам (follow_redirects=True) — внешний сайт мог увести бота на
127.0.0.1:<порт> и получить ответ внутреннего сервиса в пост.
"""

import httpx
import pytest

import core.web_reader as web_reader
from core.web_reader import UnsafeURLError, safe_get

PUBLIC = "http://93.184.216.34"  # IP-литерал: проверка без DNS


def _mock(monkeypatch, handler):
    seen = []

    def wrapped(request):
        seen.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        web_reader, "_http_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(wrapped), timeout=timeout),
    )
    return seen


async def test_redirect_to_loopback_is_blocked(monkeypatch):
    seen = _mock(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1:8091/admin"}))

    with pytest.raises(UnsafeURLError):
        await safe_get(f"{PUBLIC}/feed")

    assert seen == [f"{PUBLIC}/feed"]  # до 127.0.0.1 запрос не дошёл


async def test_public_redirect_is_followed(monkeypatch):
    def handler(r):
        if r.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="ok")

    _mock(monkeypatch, handler)

    resp = await safe_get(f"{PUBLIC}/old")
    assert resp.status_code == 200 and resp.text == "ok"


async def test_direct_private_url_is_blocked(monkeypatch):
    seen = _mock(monkeypatch, lambda r: httpx.Response(200))
    with pytest.raises(UnsafeURLError):
        await safe_get("http://169.254.169.254/latest/meta-data")
    assert seen == []


async def test_image_download_is_guarded(monkeypatch):
    from core.images import download_image_from_url

    seen = _mock(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://10.0.0.5/x.jpg"}))
    assert await download_image_from_url(f"{PUBLIC}/pic.jpg") is None
    assert seen == [f"{PUBLIC}/pic.jpg"]
