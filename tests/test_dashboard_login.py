"""Вход в панель с чистого браузера должен проходить с первой попытки.

Регрессия (25.09.2026, владелец шёл подключать личный ключ): при первом
визите куки CSRF ещё нет, а get_csrf_token вызывался дважды — в форму и в
куку уходили два РАЗНЫХ случайных токена, и вход всегда падал с «Форма
устарела — страница обновлена, попробуйте ещё раз».
"""

import re

import httpx
from fastapi import FastAPI

import web.dashboard.routes as dashboard
from core.auth import COOKIE_NAME


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(dashboard.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t")  # куки панели — secure


async def test_first_login_from_fresh_browser_succeeds():
    async with _client() as c:  # ни одной куки
        page = await c.get("/dashboard/login")
        form_token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)

        r = await c.post("/dashboard/login", data={"_csrf": form_token, "password": "test-password"})

    assert r.status_code == 303
    assert "error" not in r.headers["location"]
    assert COOKIE_NAME in r.cookies
