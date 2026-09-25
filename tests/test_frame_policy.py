"""Mini App встраивается в VK: VK переехал на vk.ru, и старый frame-ancestors
(только vk.com) не давал открыть приложение с компьютера — браузер резал iframe."""

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from web.app import VKFrameMiddleware


def _client():
    app = FastAPI()

    @app.get("/miniapp")
    async def miniapp():
        return HTMLResponse("ok")

    @app.get("/dashboard")
    async def dashboard():
        return HTMLResponse("ok")

    app.add_middleware(VKFrameMiddleware)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_miniapp_can_be_framed_by_vk_ru_and_vk_com():
    async with _client() as c:
        csp = (await c.get("/miniapp")).headers["content-security-policy"]

    for origin in ("https://vk.com", "https://*.vk.com", "https://vk.ru", "https://*.vk.ru"):
        assert origin in csp.split(), f"{origin} не может встроить Mini App: {csp}"


async def test_dashboard_still_not_frameable():
    async with _client() as c:
        r = await c.get("/dashboard")

    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
