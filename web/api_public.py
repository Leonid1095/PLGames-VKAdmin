"""Public API — allows external clients to fetch content from connected groups."""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Query, HTTPException

from database.service import (
    get_group, get_all_active_groups,
    get_post_analytics, get_content_plan,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


@router.get("/groups")
async def list_groups():
    """List all active groups (public info only)."""
    groups = await get_all_active_groups()
    return {
        "groups": [
            {
                "group_id": g.group_id,
                "group_name": g.group_name or f"Group {g.group_id}",
                "connected_at": g.connected_at.isoformat() if g.connected_at else None,
            }
            for g in groups
        ]
    }


@router.get("/groups/{group_id}/feed")
async def group_feed(
    group_id: int,
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Get recent posts with analytics for a group.
    External services can use this to display or redistribute content.
    """
    group = await get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    analytics = await get_post_analytics(group_id, limit=limit)

    return {
        "group_id": group_id,
        "group_name": group.group_name,
        "posts": [
            {
                "vk_post_id": p.vk_post_id,
                "published_at": p.published_at.isoformat() if p.published_at else None,
                "likes": p.likes,
                "reposts": p.reposts,
                "comments": p.comments,
                "views": p.views,
                "vk_url": f"https://vk.com/wall-{group_id}_{p.vk_post_id}",
            }
            for p in analytics
        ],
    }


@router.get("/groups/{group_id}/schedule")
async def group_schedule(group_id: int):
    """Get today's content plan for a group."""
    group = await get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    now = datetime.now(timezone.utc)
    posts = await get_content_plan(group_id, now)

    return {
        "group_id": group_id,
        "date": now.strftime("%Y-%m-%d"),
        "posts": [
            {
                "id": p.id,
                "text": p.text[:200] + "..." if len(p.text) > 200 else p.text,
                "scheduled_at": p.scheduled_at.isoformat(),
                "status": p.status,
                "source": p.source,
            }
            for p in posts
        ],
    }
