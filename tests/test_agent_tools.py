"""Инструменты агента, которые VK не пускает с ключом сообщества (error 27)."""

from types import SimpleNamespace

import core.agent as agent
from core.group_context import GroupContext

GID = 236517033


class _GroupAuthError(Exception):
    code = 27


async def _denied(**kw):
    raise _GroupAuthError("Group authorization failed: method is unavailable with group auth.")


def _ctx():
    api = SimpleNamespace(
        groups=SimpleNamespace(ban=_denied, unban=_denied),
        wall=SimpleNamespace(pin=_denied),
    )
    return GroupContext(GID, api, 1)


async def test_ban_explains_manual_path(db):
    out = await agent._exec_ban_user(_ctx(), {"user_id": 555})
    assert "вручную" in out and "vk.com/id555" in out
    assert "Group authorization failed" not in out


async def test_pin_explains_manual_path(db):
    out = await agent._exec_pin_post(_ctx(), {"post_id": 87})
    assert "вручную" in out and f"vk.com/wall-{GID}_87" in out
