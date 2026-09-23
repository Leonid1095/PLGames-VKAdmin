"""Mini App: админ группы не пишет служебные ключи, VK-данные экранируются."""

import httpx
from fastapi import FastAPI

import web.miniapp.routes as mini
from core.crypto import encrypt_token
from core.vk_auth import create_miniapp_token
from database.service import (
    add_content_source, create_content_task, create_group, get_setting, set_setting,
)

GID = 236517033
ADMIN = 309736634


def _client():
    app = FastAPI()
    app.include_router(mini.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_settings_endpoint_rejects_internal_keys(db):
    """_last_newsletter_at="" снимал суточный лимит рассылки по всем участникам."""
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    await set_setting(GID, "_last_newsletter_at", "2026-09-23T10:00:00+00:00")
    token = create_miniapp_token(ADMIN, GID)

    async with _client() as c:
        r = await c.post(
            f"/miniapp/group/{GID}/settings?token={token}",
            data={"key": "_last_newsletter_at", "value": ""},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        ok = await c.post(
            f"/miniapp/group/{GID}/settings?token={token}",
            data={"key": "moderation_level", "value": "4"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert r.status_code == 400
    assert await get_setting(GID, "_last_newsletter_at", "") == "2026-09-23T10:00:00+00:00"
    assert ok.status_code == 200
    assert await get_setting(GID, "moderation_level", "") == "4"


async def test_group_page_escapes_source_and_task_types(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)
    await add_content_source(GID, "<img src=x onerror=alert(1)>", "https://example.com/rss")
    await create_content_task(GID, "t1", "<script>x</script>", "0 18 * * 5")
    token = create_miniapp_token(ADMIN, GID)

    async with _client() as c:
        html = (await c.get(f"/miniapp/group/{GID}?token={token}")).text

    assert "<img src=x onerror" not in html
    assert "<script>x</script>" not in html
