"""Сбор статистики постов: лайки/комментарии должны доходить до БД.

Регрессия: сборщик вызывал wall.get ключом сообщества, а VK разрешает этот
метод только ключам user/service → error 27 каждые 6 ч, post_analytics пуста,
дашборд и мини-апп показывали нули при реальных лайках и комментариях.
"""

import httpx

from core import vk_read
from core.crypto import encrypt_token
from database.service import create_group, get_post_analytics
from tasks.analytics import collect_analytics, collect_group_analytics

GID = 236517033


def _post(pid, likes, comments, reposts=0, views=0, date=1757970000):
    return {
        "id": pid, "date": date,
        "likes": {"count": likes}, "comments": {"count": comments},
        "reposts": {"count": reposts}, "views": {"count": views},
    }


def _mock_vk(monkeypatch, handler):
    """Подменить HTTP-транспорт vk_read: запросы уходят в handler, не в сеть."""
    seen = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        vk_read, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_wrapped)),
    )
    return seen


async def test_collects_likes_and_comments_via_service_key(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    seen = _mock_vk(monkeypatch, lambda r: httpx.Response(200, json={
        "response": {"count": 2, "items": [
            _post(87, likes=3, comments=2, reposts=1, views=50),
            _post(84, likes=5, comments=0, views=70, date=1757900000),
        ]},
    }))

    await collect_analytics()

    # Стену читаем сервисным ключом приложения, НЕ ключом сообщества.
    assert len(seen) == 1
    assert seen[0].url.params["access_token"] == "svc-test-key"
    assert seen[0].url.params["owner_id"] == str(-GID)

    rows = {r.vk_post_id: r for r in await get_post_analytics(GID)}
    assert rows[87].likes == 3 and rows[87].comments == 2
    assert rows[87].reposts == 1 and rows[87].views == 50
    assert rows[84].likes == 5 and rows[84].views == 70


async def test_vk_error_is_reported_not_swallowed(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("group-token"), 1)
    _mock_vk(monkeypatch, lambda r: httpx.Response(200, json={
        "error": {"error_code": 15, "error_msg": "Access denied: group is closed"},
    }))

    result = await collect_group_analytics(GID)

    assert result.ok is False
    assert "15" in result.error
    assert await get_post_analytics(GID) == []


async def test_one_group_failure_does_not_block_others(db, monkeypatch):
    await create_group(GID, "WOW", encrypt_token("t1"), 1)
    await create_group(240061584, "Bot", encrypt_token("t2"), 1)

    def handler(request):
        if request.url.params["owner_id"] == str(-GID):
            return httpx.Response(200, json={"error": {"error_code": 30, "error_msg": "private"}})
        return httpx.Response(200, json={"response": {"count": 1, "items": [_post(5, 1, 1)]}})

    _mock_vk(monkeypatch, handler)

    await collect_analytics()

    assert await get_post_analytics(GID) == []
    assert (await get_post_analytics(240061584))[0].likes == 1
