"""Scheduler — all periodic background jobs."""

import logging
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from vkbottle import API

from core.ai_brain import generate_post
from core.crypto import decrypt_token
from database.service import (
    get_all_active_groups, get_setting, set_setting,
    get_due_posts, mark_post_published, mark_post_failed,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ─── Job 1: Auto-post (AI-generated, per-group interval) ────────────────────

async def _autopost_job():
    groups = await get_all_active_groups()

    for group in groups:
        try:
            enabled = (await get_setting(group.group_id, "autopost_enabled", "false")).lower()
            if enabled != "true":
                continue

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

            logger.info(f"Auto-post: generating for group {group.group_id}...")
            post_text = await generate_post(group_id=group.group_id)

            token = decrypt_token(group.access_token)
            api = API(token=token)
            await api.wall.post(owner_id=-group.group_id, message=post_text)

            await set_setting(group.group_id, "_last_autopost", datetime.now(timezone.utc).isoformat())
            logger.info(f"Auto-post: published for group {group.group_id}.")
        except Exception as e:
            logger.error(f"Auto-post failed for group {group.group_id}: {e}")


# ─── Job 2: Scheduled posts publisher ───────────────────────────────────────

async def _scheduled_posts_job():
    posts = await get_due_posts()
    if not posts:
        return

    # Group posts by group_id to reuse API instances
    from collections import defaultdict
    by_group = defaultdict(list)
    for p in posts:
        by_group[p.group_id].append(p)

    groups = await get_all_active_groups()
    group_map = {g.group_id: g for g in groups}

    for gid, group_posts in by_group.items():
        group = group_map.get(gid)
        if not group:
            continue

        try:
            token = decrypt_token(group.access_token)
            api = API(token=token)

            for p in group_posts:
                try:
                    result = await api.wall.post(owner_id=-gid, message=p.text)
                    vk_post_id = result.post_id if result else 0
                    await mark_post_published(p.id, vk_post_id)
                    logger.info(f"Scheduled post #{p.id} published for group {gid}")
                except Exception as e:
                    logger.error(f"Failed to publish scheduled post #{p.id}: {e}")
                    await mark_post_failed(p.id)
        except Exception as e:
            logger.error(f"Scheduled posts error for group {gid}: {e}")


# ─── Job 3: Content parser ──────────────────────────────────────────────────

async def _content_parse_job():
    from tasks.content_parser import fetch_and_schedule

    groups = await get_all_active_groups()
    for group in groups:
        try:
            sources_exist = bool(await get_setting(group.group_id, "_has_sources", ""))
            # Check every time (lightweight)
            count = await fetch_and_schedule(group.group_id)
            if count:
                logger.info(f"Content parser: scheduled {count} posts for group {group.group_id}")
        except Exception as e:
            logger.error(f"Content parser error for group {group.group_id}: {e}")


# ─── Job 4: Analytics collector ──────────────────────────────────────────────

async def _analytics_job():
    from tasks.analytics import collect_analytics
    await collect_analytics()


# ─── Scheduler startup ──────────────────────────────────────────────────────

async def start_scheduler():
    # Auto-posting: check every hour
    scheduler.add_job(
        _autopost_job,
        trigger=IntervalTrigger(hours=1),
        id="autopost", replace_existing=True,
    )

    # Scheduled posts: check every 5 minutes
    scheduler.add_job(
        _scheduled_posts_job,
        trigger=IntervalTrigger(minutes=5),
        id="scheduled_posts", replace_existing=True,
    )

    # Content parser: check every 4 hours
    scheduler.add_job(
        _content_parse_job,
        trigger=IntervalTrigger(hours=4),
        id="content_parser", replace_existing=True,
    )

    # Analytics: collect every 6 hours
    scheduler.add_job(
        _analytics_job,
        trigger=IntervalTrigger(hours=6),
        id="analytics", replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: autopost(1h), scheduled_posts(5m), "
        "content_parser(4h), analytics(6h)"
    )
