"""Дашборд показывает статистику стены: лайки, комментарии, просмотры."""

from datetime import datetime, timezone

import httpx
from fastapi import FastAPI

from core.auth import COOKIE_NAME, CSRF_COOKIE_NAME, _get_session_token
from core.crypto import encrypt_token
from database.service import create_group, upsert_post_analytics
from web.dashboard import routes as dashboard_routes

GID = 236517033


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(dashboard_routes.router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={COOKIE_NAME: _get_session_token(), CSRF_COOKIE_NAME: "csrf-x"},
    )


async def test_group_page_shows_wall_stats(db):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    await upsert_post_analytics(
        GID, 87, likes=3, comments=2, reposts=1, views=50,
        published_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    await upsert_post_analytics(
        GID, 84, likes=5, comments=0, views=70,
        published_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )

    async with _client() as c:
        html = (await c.get(f"/dashboard/group/{GID}")).text

    assert "Статистика стены" in html
    assert 'data-stat="likes">8<' in html
    assert 'data-stat="comments">2<' in html
    assert 'data-stat="views">120<' in html
    assert f"https://vk.com/wall-{GID}_87" in html


async def test_group_page_without_stats_explains_why(db):
    await create_group(GID, "WOW", encrypt_token("t"), 1)

    async with _client() as c:
        html = (await c.get(f"/dashboard/group/{GID}")).text

    assert "Статистика стены" in html
    assert "ещё не собрана" in html


async def test_refresh_button_collects_now(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    called = []

    async def fake_collect(group_id):
        called.append(group_id)
        from tasks.analytics import CollectResult
        return CollectResult(ok=True, posts=3)

    monkeypatch.setattr("tasks.analytics.collect_group_analytics", fake_collect)

    async with _client() as c:
        resp = await c.post(
            f"/dashboard/group/{GID}/analytics/refresh", data={"_csrf": "csrf-x"},
        )

    assert called == [GID]
    assert resp.status_code == 303
    assert "stats=ok" in resp.headers["location"]


async def test_refresh_requires_csrf(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    called = []

    async def fake_collect(group_id):
        called.append(group_id)

    monkeypatch.setattr("tasks.analytics.collect_group_analytics", fake_collect)

    async with _client() as c:
        await c.post(f"/dashboard/group/{GID}/analytics/refresh", data={"_csrf": "wrong"})

    assert called == []
