"""Обработчики событий Callback API."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

import web.vk_callback as cb
from core.crypto import encrypt_token
from core.group_context import GroupContext
from database.service import (
    create_group, get_setting, is_human_mode_active, set_human_mode,
)
from database.engine import async_session
from database.models import UserContext
from sqlalchemy import select

GID = 236517033


class FakeAPI:
    def __init__(self):
        self.sent = []
        api = self

        async def users_get(**kw):
            return [SimpleNamespace(first_name="Аня", last_name="К")]

        async def send(**kw):
            api.sent.append(kw)

        self.users = SimpleNamespace(get=users_get)
        self.messages = SimpleNamespace(send=send)


async def test_join_is_counted_even_when_welcome_is_off(db):
    """Дайджест и публичное приветствие опираются на учёт вступлений —
    раньше он шёл только при включённом приветствии (у живой группы — «+0»)."""
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    ctx = GroupContext(GID, FakeAPI(), 1)

    await cb._process_group_join(ctx, {"user_id": 777, "join_type": "join"})

    assert await get_setting(GID, "_joins_since_digest", "0") == "1"
    assert "777" in await get_setting(GID, "_pending_welcome", "[]")


async def test_join_request_is_not_a_join(db):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    ctx = GroupContext(GID, FakeAPI(), 1)

    await cb._process_group_join(ctx, {"user_id": 777, "join_type": "request"})

    assert await get_setting(GID, "_joins_since_digest", "0") == "0"


async def test_manual_admin_reply_does_not_shorten_escalation(db):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    long_until = datetime.now(timezone.utc) + timedelta(hours=24)
    await set_human_mode(GID, 555, long_until)
    ctx = GroupContext(GID, FakeAPI(), 1)

    await cb._process_message_reply(ctx, {"message": {"admin_author_id": 1, "peer_id": 555}})

    async with async_session() as s:
        until = (await s.execute(
            select(UserContext.human_mode_until).where(UserContext.vk_id == 555)
        )).scalar_one()
    assert until.replace(tzinfo=None) >= long_until.replace(tzinfo=None) - timedelta(seconds=1)


async def test_manual_admin_reply_starts_handoff(db):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    ctx = GroupContext(GID, FakeAPI(), 1)

    await cb._process_message_reply(ctx, {"message": {"admin_author_id": 1, "peer_id": 556}})

    assert await is_human_mode_active(GID, 556)


def _app():
    app = FastAPI()
    app.include_router(cb.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_events_for_unknown_groups_are_not_tracked(db):
    cb._rate_counters.clear()
    async with _app() as c:
        for gid in (1, 2, 3):
            await c.post("/api/vk/events", json={"type": "message_new", "group_id": gid, "object": {}})
    assert cb._rate_counters == {}


async def test_wrong_secret_is_dropped(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("t"), 1, secret_key="s3cret")
    spawned = []
    monkeypatch.setattr(cb, "_spawn", lambda coro, key: (spawned.append(key), coro.close()))
    async with _app() as c:
        await c.post("/api/vk/events", json={
            "type": "like_add", "group_id": GID, "secret": "nope", "event_id": "e1", "object": {},
        })
        await c.post("/api/vk/events", json={
            "type": "like_add", "group_id": GID, "secret": "s3cret", "event_id": "e2", "object": {},
        })
    assert spawned == [f"{GID}:e2"]
