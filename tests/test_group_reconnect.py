"""Переподключение группы не должно затирать рабочие данные пустыми."""

from core.crypto import encrypt_token
from database.service import create_group, get_group

GID = 240061584


async def test_reconnect_keeps_manual_confirmation_code_and_admin(db):
    # Код взят из UI VK руками (токен не может getCallbackConfirmationCode — err 1051).
    await create_group(GID, "Bot", encrypt_token("t1"), 309736634, confirmation_code="a1b2c3d4", secret_key="s1")

    # Повторный OAuth: VK не отдал код и user_id.
    await create_group(GID, "Bot", encrypt_token("t2"), 0, confirmation_code="", secret_key="s2")

    g = await get_group(GID)
    assert g.confirmation_code == "a1b2c3d4"
    assert g.admin_vk_id == 309736634
    assert g.secret_key == "s2"  # новый секрет — это нормально, он уходит в VK


async def test_reconnect_updates_real_new_values(db):
    await create_group(GID, "Bot", encrypt_token("t1"), 1, confirmation_code="old")
    await create_group(GID, "Bot2", encrypt_token("t2"), 2, confirmation_code="new")
    g = await get_group(GID)
    assert (g.confirmation_code, g.admin_vk_id, g.group_name) == ("new", 2, "Bot2")
