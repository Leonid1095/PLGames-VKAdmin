"""Установка виджета с телефона: кнопка — на стартовой странице мини-аппа.

На Android VK открывает внутри себя только стартовую страницу (по подписанным
launch-параметрам); любая ссылка из мини-аппа уходит во внешний Chrome, где
VK Bridge мёртв. Поэтому установка из ⚙️ → «🏆 Виджет» с телефона не проходила.
"""

import httpx
from fastapi import FastAPI

import web.miniapp.routes as mini
from core.crypto import encrypt_token
from core.vk_auth import create_miniapp_token
from database.service import create_group

GID = 236517033
GID2 = 240061584
ADMIN = 309736634


def _client():
    app = FastAPI()
    app.include_router(mini.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _entry():
    token = create_miniapp_token(ADMIN, 0)  # запуск не из сообщества
    async with _client() as c:
        return await c.get(f"/miniapp?token={token}")


async def test_start_page_has_install_button_for_each_admin_group(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    await create_group(GID2, "Twitch", encrypt_token("t"), ADMIN)

    html = (await _entry()).text

    assert f"installWidget({GID}," in html
    assert f"installWidget({GID2}," in html
    assert "VKWebAppGetCommunityToken" in html


async def test_single_group_admin_lands_on_list_with_install_button(db):
    """Раньше при одной группе стартовая сразу уводила в профиль — до кнопки
    установки с телефона было не добраться."""
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)

    r = await _entry()

    assert r.status_code == 200
    assert f"installWidget({GID}," in r.text


async def test_settings_page_uses_the_same_install_script(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    token = create_miniapp_token(ADMIN, GID)
    async with _client() as c:
        html = (await c.get(f"/miniapp/group/{GID}?token={token}")).text

    assert f"installWidget({GID}," in html
    assert html.count("function installWidget(") == 1
