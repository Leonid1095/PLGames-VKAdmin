"""Комментарии на стене: свои не трогаем, провал модерации — сразу к людям.

Ключ сообщества не может wall.deleteComment / groups.ban (VK error 27 —
проверено вживую), поэтому «удаление» токсичного комментария раньше молча
падало в лог: комментарий висел, админ ничего не знал.
"""

from types import SimpleNamespace

import pytest

from core.group_context import GroupContext
from database.service import create_group
from core.crypto import encrypt_token
import handlers.comments as comments

GID = 236517033
ADMIN = 309736634


class _VKGroupAuthError(Exception):
    code = 27


class FakeAPI:
    def __init__(self, delete_fails=True, ban_fails=True):
        self.sent = []
        self.comments = []
        self.deleted = []
        self.banned = []
        api = self

        async def send(**kw):
            api.sent.append(kw)

        async def create_comment(**kw):
            api.comments.append(kw)

        async def delete_comment(**kw):
            if delete_fails:
                raise _VKGroupAuthError("method is unavailable with group auth")
            api.deleted.append(kw)

        async def ban(**kw):
            if ban_fails:
                raise _VKGroupAuthError("method is unavailable with group auth")
            api.banned.append(kw)

        async def users_get(**kw):
            return [SimpleNamespace(first_name="Иван", last_name="Петров")]

        self.messages = SimpleNamespace(send=send)
        self.wall = SimpleNamespace(create_comment=create_comment, delete_comment=delete_comment)
        self.groups = SimpleNamespace(ban=ban)
        self.users = SimpleNamespace(get=users_get)

    async def request(self, method, params):
        assert method == "groups.getMembers"
        return {"response": {"items": [{"id": ADMIN}]}}


def _event(from_id, text="текст", cid=88, post_id=87):
    return {"id": cid, "post_id": post_id, "from_id": from_id, "text": text, "owner_id": -GID}


@pytest.fixture
async def group(db):
    await create_group(GID, "WOW", encrypt_token("t"), ADMIN)


async def test_own_community_comment_is_ignored(group, monkeypatch):
    """Ответ самого бота прилетает обратно как wall_reply_new — нельзя
    модерировать и тем более отвечать самому себе."""
    async def boom(*a, **kw):
        raise AssertionError("LLM must not be called for our own comment")

    monkeypatch.setattr(comments, "analyze_toxicity", boom)
    monkeypatch.setattr(comments, "generate_response", boom)
    api = FakeAPI()

    await comments.handle_wall_comment(GroupContext(GID, api, ADMIN), _event(from_id=-GID))

    assert api.comments == [] and api.sent == []


async def test_failed_delete_notifies_admin_with_link(group, monkeypatch):
    async def toxic(group_id, text):
        return True

    monkeypatch.setattr(comments, "analyze_toxicity", toxic)
    api = FakeAPI(delete_fails=True)

    await comments.handle_wall_comment(
        GroupContext(GID, api, ADMIN), _event(from_id=555, text="плохие слова"),
    )

    assert len(api.sent) == 1
    note = api.sent[0]
    assert note["user_id"] == ADMIN
    assert f"https://vk.com/wall-{GID}_87?reply=88" in note["message"]
    assert "плохие слова" in note["message"]


async def test_successful_delete_does_not_spam_admin(group, monkeypatch):
    async def toxic(group_id, text):
        return True

    monkeypatch.setattr(comments, "analyze_toxicity", toxic)
    api = FakeAPI(delete_fails=False)

    await comments.handle_wall_comment(
        GroupContext(GID, api, ADMIN), _event(from_id=555, text="плохие слова"),
    )

    assert api.deleted and api.sent == []


async def test_admin_comment_is_not_moderated(group, monkeypatch):
    async def toxic(group_id, text):
        raise AssertionError("admin comments must not go through moderation")

    async def no_reply(**kw):
        return "NO_REPLY"

    monkeypatch.setattr(comments, "analyze_toxicity", toxic)
    monkeypatch.setattr(comments, "generate_response", no_reply)
    api = FakeAPI(delete_fails=False)

    await comments.handle_wall_comment(GroupContext(GID, api, ADMIN), _event(from_id=ADMIN, text="жёстко, но по делу"))

    assert api.deleted == []
