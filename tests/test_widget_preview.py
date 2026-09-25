"""Окно предпросмотра при установке показывает тот же вид, что и ежечасное
обновление: тип виджета приходит с сервера, а не зашит в JS."""

import json

import httpx
import pytest
from fastapi import FastAPI

import core.widgets as widgets
import web.miniapp.routes as mini
from core.crypto import encrypt_token
from core.vk_auth import create_miniapp_token
from database.service import add_xp, create_group

GID = 236517033
ADMIN = 309736634


@pytest.fixture(autouse=True)
def vk_names(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": [
            {"id": 11, "first_name": "Иван", "last_name": "Петров"},
        ]})

    monkeypatch.setattr(
        widgets, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _client():
    app = FastAPI()
    app.include_router(mini.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _preview():
    token = create_miniapp_token(ADMIN, GID)
    async with _client() as c:
        data = (await c.get(f"/miniapp/group/{GID}/widget/code?token={token}")).json()
    return data["type"], json.loads(data["code"][len("return "):-1])


async def test_preview_is_the_same_widget_as_hourly_update(db):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    await add_xp(GID, 11, 10)

    wtype, widget = await _preview()

    assert wtype == "table"
    assert widget["body"][0][0]["text"] == "🥇 Иван Петров"


async def test_preview_without_members_is_a_table_placeholder(db):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)

    wtype, widget = await _preview()

    assert wtype == "table"
    assert widget["body"] and widget["title"] == "🏆 Топ участников"
    assert len(widget["body"][0]) == len(widget["head"])


async def test_install_button_takes_widget_type_from_server(db):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    token = create_miniapp_token(ADMIN, GID)
    async with _client() as c:
        html = (await c.get(f"/miniapp/group/{GID}?token={token}")).text

    assert "type: data.type" in html
    assert "type: 'table'" not in html
