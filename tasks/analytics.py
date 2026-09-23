"""Post analytics collector — fetches stats from VK wall posts.

Стена читается сервисным ключом (core.vk_read): ключ сообщества wall.get не
может (VK error 27), из-за чего раньше статистика не собиралась вообще.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from core.vk_read import VKReadError, wall_get
from database.service import get_all_active_groups, upsert_post_analytics

logger = logging.getLogger(__name__)

POSTS_TO_TRACK = 20


@dataclass
class CollectResult:
    ok: bool
    posts: int = 0
    error: str = ""


def _count(post: dict, field: str) -> int:
    value = post.get(field) or {}
    return int(value.get("count", 0) or 0) if isinstance(value, dict) else 0


async def collect_group_analytics(group_id: int) -> CollectResult:
    """Обновить статистику последних постов одной группы."""
    try:
        items = await wall_get(-group_id, count=POSTS_TO_TRACK)
    except VKReadError as e:
        logger.warning(f"Analytics: wall.get failed for group {group_id}: {e}")
        return CollectResult(ok=False, error=str(e))
    except Exception as e:
        logger.warning(f"Analytics: wall.get failed for group {group_id}: {e!r}")
        return CollectResult(ok=False, error=repr(e))

    for post in items:
        published = (
            datetime.fromtimestamp(post["date"], tz=timezone.utc) if post.get("date") else None
        )
        await upsert_post_analytics(
            group_id=group_id,
            vk_post_id=post["id"],
            likes=_count(post, "likes"),
            reposts=_count(post, "reposts"),
            comments=_count(post, "comments"),
            views=_count(post, "views"),
            published_at=published,
        )

    logger.info(f"Analytics collected for group {group_id}: {len(items)} posts")
    return CollectResult(ok=True, posts=len(items))


async def collect_analytics() -> None:
    """Fetch recent post stats for all active groups."""
    for group in await get_all_active_groups():
        try:
            await collect_group_analytics(group.group_id)
        except Exception as e:
            logger.error(f"Analytics error for group {group.group_id}: {e!r}")
