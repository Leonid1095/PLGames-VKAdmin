import asyncio
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_all():
    logger.info("Starting verification...")

    # 1. Test imports
    logger.info("Testing imports...")
    try:
        import core.config
        import database.models
        import database.engine
        import database.service
        import core.ai_brain
        import core.crypto
        import core.group_context
        import tasks.scheduler
        import web.app
        import web.vk_callback
        import web.oauth
        logger.info("Imports passed.")
    except Exception as e:
        logger.error(f"Import failed: {e}")
        return

    # 2. Test Database Logic (multi-tenant)
    logger.info("Testing database logic...")
    from database.engine import init_db, engine
    from database.models import Base
    from database.service import (
        create_group, get_group, get_all_active_groups,
        seed_default_settings, get_setting, set_setting,
        get_user_stats, check_and_increment_limit, grant_vip, modify_balance,
    )
    from core.crypto import encrypt_token, decrypt_token

    await init_db()

    # Test crypto
    test_token = "vk1.a.test_token_12345"
    encrypted = encrypt_token(test_token)
    decrypted = decrypt_token(encrypted)
    assert decrypted == test_token, "Token encryption/decryption failed"
    logger.info("Crypto passed.")

    # Test group creation
    test_group_id = 999999
    test_admin_id = 111111
    group = await create_group(
        group_id=test_group_id,
        group_name="Test Group",
        access_token=encrypted,
        admin_vk_id=test_admin_id,
    )
    assert group.group_id == test_group_id
    logger.info("create_group passed.")

    # Test group retrieval
    g = await get_group(test_group_id)
    assert g is not None
    assert g.group_name == "Test Group"
    logger.info("get_group passed.")

    groups = await get_all_active_groups()
    assert any(x.group_id == test_group_id for x in groups)
    logger.info("get_all_active_groups passed.")

    # Test settings (per-group)
    await seed_default_settings(test_group_id)
    model = await get_setting(test_group_id, "active_model", "fallback")
    assert model != "fallback", "Default settings should be seeded"
    logger.info("seed_default_settings passed.")

    await set_setting(test_group_id, "active_model", "test-model")
    model = await get_setting(test_group_id, "active_model")
    assert model == "test-model"
    logger.info("set_setting / get_setting passed.")

    # Test user stats (per-group)
    test_vk_id = 123456
    stats = await get_user_stats(test_group_id, test_vk_id)
    assert not stats.is_vip
    assert stats.balance == 0.0
    assert stats.group_id == test_group_id
    logger.info("get_user_stats passed.")

    allowed = await check_and_increment_limit(test_group_id, test_vk_id)
    assert allowed is True
    stats = await get_user_stats(test_group_id, test_vk_id)
    assert stats.daily_requests == 1
    logger.info("check_and_increment_limit passed.")

    await grant_vip(test_group_id, test_vk_id, 30)
    stats = await get_user_stats(test_group_id, test_vk_id)
    assert stats.is_vip is True
    assert stats.vip_expires is not None
    logger.info("grant_vip passed.")

    new_balance = await modify_balance(test_group_id, test_vk_id, 100.5)
    assert new_balance == 100.5
    logger.info("modify_balance passed.")

    logger.info("All tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_all())
