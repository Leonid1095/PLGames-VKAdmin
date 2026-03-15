"""Post analytics collector — fetches stats from VK wall posts."""

import logging
from datetime import datetime, timezone

from vkbottle import API

from core.crypto import decrypt_token
from database.service import get_all_active_groups, upsert_post_analytics

logger = logging.getLogger(__name__)


async def collect_analytics():
    """Fetch recent post stats for all active groups."""
    groups = await get_all_active_groups()

    for group in groups:
        try:
            token = decrypt_token(group.access_token)
            api = API(token=token)

            try:
                resp = await api.wall.get(owner_id=-group.group_id, count=20)
            except Exception as wall_err:
                logger.warning(f"wall.get failed for group {group.group_id}: {wall_err}")
                continue
            if not resp or not resp.items:
                continue

            for post in resp.items:
                likes = post.likes.count if post.likes else 0
                reposts = post.reposts.count if post.reposts else 0
                comments = post.comments.count if post.comments else 0
                views = post.views.count if post.views else 0
                published = datetime.fromtimestamp(post.date, tz=timezone.utc) if post.date else None

                await upsert_post_analytics(
                    group_id=group.group_id,
                    vk_post_id=post.id,
                    likes=likes, reposts=reposts,
                    comments=comments, views=views,
                    published_at=published,
                )

            logger.info(f"Analytics collected for group {group.group_id}: {len(resp.items)} posts")
        except Exception as e:
            logger.error(f"Analytics error for group {group.group_id}: {e}")
