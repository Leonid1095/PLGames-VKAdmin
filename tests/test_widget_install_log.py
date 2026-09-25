"""Установка виджета отчитывается в журнал сервиса.

VK Bridge работает на телефоне админа: сбой «Установить виджет» раньше не
доходил до сервера вовсе — с июля в логах не было ни одного следа попыток.
"""

import logging

import httpx
from fastapi import FastAPI

import web.miniapp.routes as mini
from core import key_status
from core.crypto import encrypt_token
from core.vk_auth import create_miniapp_token
from database.service import create_group, get_setting, set_setting

GID = 236517033
ADMIN = 309736634

BRIDGE_ERROR = ('{"error_type":"client_error","error_data":'
                '{"error_code":1,"error_reason":"Unknown error"}}')


def _client():
    app = FastAPI()
    app.include_router(mini.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_phone_side_install_error_reaches_log_and_keys_card(db, caplog):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    await set_setting(GID, "widget_enabled", "true")
    token = create_miniapp_token(ADMIN, GID)

    with caplog.at_level(logging.INFO):
        async with _client() as c:
            r = await c.post(
                f"/miniapp/group/{GID}/widget/client-log?token={token}",
                data={"step": "token", "error": BRIDGE_ERROR},
            )

    assert r.status_code == 200
    assert "Unknown error" in caplog.text
    assert "step=token" in caplog.text

    statuses = {s.key: s for s in await key_status.check_group_keys(GID)}
    assert "Unknown error" in statuses["widget"].detail


async def test_install_start_logs_where_page_is_open(db, caplog):
    """Bridge молча висит вне VK — в журнале должно быть видно, где открыта страница."""
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    token = create_miniapp_token(ADMIN, GID)

    with caplog.at_level(logging.INFO):
        async with _client() as c:
            await c.post(
                f"/miniapp/group/{GID}/widget/client-log?token={token}",
                data={"step": "start", "env": '{"embedded":false,"ua":"Mozilla/5.0"}'},
            )

    assert '"embedded":false' in caplog.text


async def test_install_button_does_not_hang_forever(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    token = create_miniapp_token(ADMIN, GID)

    async with _client() as c:
        html = (await c.get(f"/miniapp/group/{GID}?token={token}")).text

    assert "isEmbedded()" in html
    assert "VK не ответил" in html
    assert f"vk.com/app{mini.settings.VK_MINIAPP_ID}_-{GID}" in html
    # На Android ссылки мини-аппа уходят во внешний Chrome, где Bridge мёртв:
    # «откройте через ВКонтакте» вело по кругу. Рабочий путь — компьютер.
    assert "на компьютере" in html


async def test_successful_install_clears_old_error(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    await set_setting(GID, "widget_install_error", "шаг «token»: old")
    token = create_miniapp_token(ADMIN, GID)

    async with _client() as c:
        await c.post(f"/miniapp/group/{GID}/widget/client-log?token={token}",
                     data={"step": "done"})

    assert await get_setting(GID, "widget_install_error") == ""


async def test_install_log_only_from_group_admin(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    stranger = create_miniapp_token(111, GID)

    async with _client() as c:
        r = await c.post(f"/miniapp/group/{GID}/widget/client-log?token={stranger}",
                         data={"step": "token", "error": "x"})

    assert r.status_code == 403
    assert await get_setting(GID, "widget_install_error") == ""


async def test_install_button_reports_each_step(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    token = create_miniapp_token(ADMIN, GID)

    async with _client() as c:
        html = (await c.get(f"/miniapp/group/{GID}?token={token}")).text

    assert f"/miniapp/group/{GID}/widget/client-log" in html
    # сбой сохранения токена больше не проглатывается
    assert "saveResult.ok" in html
