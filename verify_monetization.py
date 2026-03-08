import asyncio
import logging
from datetime import datetime, timezone

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_all():
    logger.info("Starting verification...")
    
    # 1. Test imports to catch circular dependencies
    logger.info("Testing imports...")
    try:
        import core.config
        import database.models
        import database.engine
        import database.service
        import core.ai_brain
        import tasks.scheduler
        # Note: handlers and main import vkbottle Bot which calls VK API on init
        # We test database/core logic independently
        logger.info("Imports passed.")
    except Exception as e:
        logger.error(f"Import failed: {e}")
        return

    # 2. Test Database Logic
    logger.info("Testing database logic...")
    from database.engine import init_db
    from database.service import get_user_stats, check_and_increment_limit, grant_vip, modify_balance
    from core.config import settings

    # Override DB URL to in-memory SQLite for testing
    settings.DATABASE_URL = "sqlite+aiosqlite:///:memory:"
    
    await init_db()
    
    test_vk_id = 123456
    
    # Test getting stats (creates user)
    stats = await get_user_stats(test_vk_id)
    assert not stats.is_vip, "Should not be VIP"
    assert stats.balance == 0.0, "Balance should be 0"
    assert stats.daily_requests == 0, "Requests should be 0"
    logger.info("get_user_stats passed.")
    
    # Test checking limit
    allowed = await check_and_increment_limit(test_vk_id)
    assert allowed is True, "First request should be allowed"
    
    stats = await get_user_stats(test_vk_id)
    assert stats.daily_requests == 1, "Requests should be incremented"
    logger.info("check_and_increment_limit passed.")
    
    # Test VIP
    await grant_vip(test_vk_id, 30)
    stats = await get_user_stats(test_vk_id)
    assert stats.is_vip is True, "Should be VIP after grant_vip"
    assert stats.vip_expires is not None, "VIP expiration should be set"
    logger.info("grant_vip passed.")
    
    # Test Balance
    new_balance = await modify_balance(test_vk_id, 100.5)
    assert new_balance == 100.5, "Balance should add correctly"
    stats = await get_user_stats(test_vk_id)
    assert stats.balance == 100.5, "Balance should persist"
    logger.info("modify_balance passed.")
    
    # Test Handlers Logic indirectly by simulating commands?
    # We can just verify the functions don't crash when called.
    
    logger.info("All tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_all())
