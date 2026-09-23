"""Предложка: одно предложение — одна публикация, сбой — можно повторить."""

import asyncio
from types import SimpleNamespace

import core.agent as agent
from core.crypto import encrypt_token
from core.group_context import GroupContext
from database.service import create_group, create_suggested_post, get_suggestion

GID = 236517033


def _ctx(post_impl):
    async def send(**kw):
        return None

    api = SimpleNamespace(wall=SimpleNamespace(post=post_impl), messages=SimpleNamespace(send=send))
    return GroupContext(GID, api, 1)


async def test_double_accept_publishes_once(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    sug = await create_suggested_post(GID, 555, "Текст от подписчика")
    posted = []

    async def post(**kw):
        await asyncio.sleep(0.01)
        posted.append(kw["message"])
        return SimpleNamespace(post_id=10)

    async def no_tg(*a, **kw):
        return False

    monkeypatch.setattr("core.telegram.send_to_telegram", no_tg)
    ctx = _ctx(post)
    await asyncio.gather(
        agent._exec_review_suggestion(ctx, {"suggestion_id": sug.id, "action": "accept"}),
        agent._exec_review_suggestion(ctx, {"suggestion_id": sug.id, "action": "accept"}),
    )

    assert posted == ["Текст от подписчика"]
    assert (await get_suggestion(sug.id)).status == "published"


async def test_failed_publish_can_be_retried(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("t"), 1)
    sug = await create_suggested_post(GID, 555, "Текст")

    async def broken(**kw):
        raise RuntimeError("VK down")

    await agent._exec_review_suggestion(_ctx(broken), {"suggestion_id": sug.id, "action": "accept"})

    assert (await get_suggestion(sug.id)).status == "pending"


async def test_miniapp_second_approve_does_not_repost(db, monkeypatch):
    import httpx
    from fastapi import FastAPI
    import web.miniapp.routes as mini
    from core.vk_auth import create_miniapp_token

    await create_group(GID, "WOW", encrypt_token("t"), 1)
    sug = await create_suggested_post(GID, 555, "Текст")
    posted = []

    class FakeAPI:
        def __init__(self, token):
            async def post(**kw):
                posted.append(kw)
                return SimpleNamespace(post_id=11)
            self.wall = SimpleNamespace(post=post)

    async def no_tg(*a, **kw):
        return False

    monkeypatch.setattr("vkbottle.API", FakeAPI)
    monkeypatch.setattr("core.telegram.send_to_telegram", no_tg)
    app = FastAPI()
    app.include_router(mini.router)
    token = create_miniapp_token(1, GID)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(2):
            await c.post(
                f"/miniapp/admin/api/review-suggestion?token={token}&gid={GID}",
                json={"suggestion_id": sug.id, "action": "approve"},
            )

    assert len(posted) == 1
