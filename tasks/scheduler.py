import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from core.ai_brain import generate_post
from database.service import get_setting

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_api = None  # Injected from main.py


def set_api(api):
    """Set the VK API instance for the scheduler to use."""
    global _api
    _api = api


async def _post_job():
    """The actual scheduled job that generates and publishes a post."""
    if _api is None:
        logger.error("Scheduler: VK API not set!")
        return

    enabled = (await get_setting("autopost_enabled", "false")).lower()
    if enabled != "true":
        return

    logger.info("Scheduler: generating auto-post...")
    post_text = await generate_post()

    from core.config import settings
    owner_id = -(int(settings.VK_GROUP_ID))

    try:
        await _api.wall.post(owner_id=owner_id, message=post_text)
        logger.info("Scheduler: auto-post published successfully.")
    except Exception as e:
        logger.error(f"Scheduler: failed to publish post: {e}")


async def start_scheduler():
    """Read interval from DB and start the APScheduler."""
    interval_str = await get_setting("autopost_interval_hours", "6")
    try:
        interval_hours = float(interval_str)
    except ValueError:
        interval_hours = 6.0

    scheduler.add_job(
        _post_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="autopost",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started: auto-post every {interval_hours} hours.")
