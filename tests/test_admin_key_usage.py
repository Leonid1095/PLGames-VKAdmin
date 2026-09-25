"""Бот пользуется личным ключом админа там, где ключ сообщества VK не пускает:
фото к постам, удаление нарушений, баны, закрепы. Нет ключа — как раньше
(ссылка админу), но с подсказкой, как подключить."""

from types import SimpleNamespace

import httpx
import pytest

import core.admin_key as admin_key
import core.agent as agent
import core.images as images
import handlers.comments as comments
from core.group_context import GroupContext
from tests.test_comments import ADMIN, GID, FakeAPI, _event
from database.service import create_group
from core.crypto import encrypt_token

_RealAsyncClient = httpx.AsyncClient


class _VKError(Exception):
    def __init__(self, code, msg="VK error"):
        super().__init__(msg)
        self.code = code


async def _group_auth_denied(**kw):
    raise _VKError(27, "Group authorization failed: method is unavailable with group auth.")


async def _dead_key(**kw):
    raise _VKError(5, "User authorization failed: invalid access_token")


@pytest.fixture
async def group(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)


@pytest.fixture
def admin_api(monkeypatch):
    """Подставляет «подключённый» ключ админа: set(api) — какой API вернуть."""
    holder = {"api": None}

    async def fake_get_admin_api(group_id):
        return holder["api"]

    monkeypatch.setattr(admin_key, "get_admin_api", fake_get_admin_api)
    return lambda api: holder.__setitem__("api", api)


@pytest.fixture
def reported(monkeypatch):
    seen = []

    async def fake_report(group_id, e):
        seen.append((group_id, getattr(e, "code", None)))

    monkeypatch.setattr(admin_key, "report_admin_key_failure", fake_report)
    return seen


def _photo_api(calls: list, *, fail=None):
    async def get_wall_upload_server(**kw):
        if fail:
            await fail()
        calls.append(("server", kw))
        return SimpleNamespace(upload_url="https://pu.vk.com/upload")

    async def save_wall_photo(**kw):
        calls.append(("save", kw))
        return [SimpleNamespace(owner_id=-GID, id=42)]

    return SimpleNamespace(photos=SimpleNamespace(
        get_wall_upload_server=get_wall_upload_server, save_wall_photo=save_wall_photo,
    ))


@pytest.fixture
def upload_server(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"photo": '[{"photo":"x"}]', "server": 1, "hash": "h"})

    monkeypatch.setattr(
        images.httpx, "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )


# ─── Фото к постам ───────────────────────────────────────────────────────────

async def test_photo_upload_goes_through_admin_key(admin_api, upload_server):
    admin_calls, group_calls = [], []
    admin_api(_photo_api(admin_calls))

    result = await images.upload_photo_to_vk(_photo_api(group_calls, fail=_group_auth_denied), GID, b"img")

    assert result == f"photo-{GID}_42"
    assert [c[0] for c in admin_calls] == ["server", "save"]
    assert admin_calls[0][1]["group_id"] == GID
    assert group_calls == []


async def test_photo_upload_without_admin_key_uses_group_key(admin_api, upload_server):
    group_calls = []

    result = await images.upload_photo_to_vk(_photo_api(group_calls), GID, b"img")

    assert result == f"photo-{GID}_42"
    assert group_calls


async def test_dead_admin_key_on_photo_upload_is_reported(admin_api, upload_server, reported):
    admin_api(_photo_api([], fail=_dead_key))

    result = await images.upload_photo_to_vk(_photo_api([]), GID, b"img")

    assert result is None  # пост выйдет без фото, как раньше
    assert reported == [(GID, 5)]


# ─── Модерация комментариев ──────────────────────────────────────────────────

@pytest.fixture
def toxic(monkeypatch):
    async def always(group_id, text):
        return True

    monkeypatch.setattr(comments, "analyze_toxicity", always)


async def test_moderation_deletes_with_admin_key(group, toxic, admin_api):
    group_api, owner_api = FakeAPI(delete_fails=True), FakeAPI(delete_fails=False)
    admin_api(owner_api)

    await comments.handle_wall_comment(
        GroupContext(GID, group_api, ADMIN), _event(from_id=555, text="плохие слова"),
    )

    assert owner_api.deleted
    assert group_api.sent == []  # руками делать нечего — админа не дёргаем


async def test_without_admin_key_admin_gets_link_and_how_to_connect(group, toxic, admin_api):
    group_api = FakeAPI(delete_fails=True)

    await comments.handle_wall_comment(
        GroupContext(GID, group_api, ADMIN), _event(from_id=555, text="плохие слова"),
    )

    note = group_api.sent[0]["message"]
    assert f"https://vk.com/wall-{GID}_87?reply=88" in note
    assert admin_key.ADMIN_KEY_HINT in note


async def test_dead_admin_key_in_moderation_is_reported_and_admin_gets_link(
    group, toxic, admin_api, reported,
):
    group_api, owner_api = FakeAPI(delete_fails=True), FakeAPI(delete_fails=False)
    owner_api.wall = SimpleNamespace(delete_comment=_dead_key, create_comment=owner_api.wall.create_comment)
    admin_api(owner_api)

    await comments.handle_wall_comment(
        GroupContext(GID, group_api, ADMIN), _event(from_id=555, text="плохие слова"),
    )

    assert reported == [(GID, 5)]
    assert f"https://vk.com/wall-{GID}_87?reply=88" in group_api.sent[0]["message"]


# ─── Инструменты агента ──────────────────────────────────────────────────────

def _group_ctx():
    api = SimpleNamespace(
        groups=SimpleNamespace(ban=_group_auth_denied, unban=_group_auth_denied),
        wall=SimpleNamespace(pin=_group_auth_denied),
    )
    return GroupContext(GID, api, ADMIN)


async def test_agent_bans_and_pins_with_admin_key(db, admin_api):
    done = []

    async def record(name):
        async def call(**kw):
            done.append((name, kw))
        return call

    admin_api(SimpleNamespace(
        groups=SimpleNamespace(ban=await record("ban"), unban=await record("unban")),
        wall=SimpleNamespace(pin=await record("pin")),
    ))

    assert "забанен" in await agent._exec_ban_user(_group_ctx(), {"user_id": 555})
    assert "разбанен" in await agent._exec_unban_user(_group_ctx(), {"user_id": 555})
    assert "закреплён" in await agent._exec_pin_post(_group_ctx(), {"post_id": 87})
    assert [d[0] for d in done] == ["ban", "unban", "pin"]
    assert done[0][1]["group_id"] == GID and done[0][1]["owner_id"] == 555


async def test_agent_without_admin_key_says_how_to_connect(db, admin_api):
    for out in (
        await agent._exec_ban_user(_group_ctx(), {"user_id": 555}),
        await agent._exec_unban_user(_group_ctx(), {"user_id": 555}),
        await agent._exec_pin_post(_group_ctx(), {"post_id": 87}),
    ):
        assert "вручную" in out
        assert admin_key.ADMIN_KEY_HINT in out
