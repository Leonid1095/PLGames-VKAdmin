import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from vkbottle import API

from core.ai_brain import generate_post
from core.crypto import decrypt_token
from database.service import get_all_active_groups, get_setting, set_setting

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _post_job():
    """Auto-post job: iterates over all active groups, respecting per-group interval."""
    groups = await get_all_active_groups()

    for group in groups:
        try:
            enabled = (await get_setting(group.group_id, "autopost_enabled", "false")).lower()
            if enabled != "true":
                continue

            # Check per-group interval
            interval_hours = int(await get_setting(group.group_id, "autopost_interval_hours", "6"))
            last_post_str = await get_setting(group.group_id, "_last_autopost", "")

            if last_post_str:
                try:
                    last_post_time = datetime.fromisoformat(last_post_str)
                    elapsed = (datetime.now(timezone.utc) - last_post_time).total_seconds() / 3600
                    if elapsed < interval_hours:
                        continue
                except ValueError:
                    pass

            logger.info(f"Scheduler: generating auto-post for group {group.group_id}...")
            post_text = await generate_post(group_id=group.group_id)

            token = decrypt_token(group.access_token)
            api = API(token=token)
            owner_id = -group.group_id

            await api.wall.post(owner_id=owner_id, message=post_text)
            await set_setting(group.group_id, "_last_autopost", datetime.now(timezone.utc).isoformat())
            logger.info(f"Scheduler: auto-post published for group {group.group_id}.")
        except Exception as e:
            logger.error(f"Scheduler: failed to auto-post for group {group.group_id}: {e}")


async def start_scheduler():
    """Start the auto-posting scheduler."""
    scheduler.add_job(
        _post_job,
        trigger=IntervalTrigger(hours=1),
        id="autopost",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started: checking auto-post every 1 hour.")
