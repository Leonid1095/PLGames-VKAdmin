"""Content parser — fetch posts from external sources (RSS, VK groups)."""

import logging
from datetime import datetime, timezone

import feedparser
import httpx

from core.ai_brain import generate_response
from core.config import settings
from database.service import (
    get_content_sources, update_source_fetched,
    create_scheduled_post, get_setting,
)

logger = logging.getLogger(__name__)


async def parse_rss(url: str) -> list[dict]:
    """Fetch and parse an RSS feed. Returns list of {title, text, link}."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))
            # Strip HTML tags
            import re
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            link = entry.get("link", "")
            if title:
                items.append({"title": title, "text": summary[:500], "link": link})
        return items
    except Exception as e:
        logger.error(f"RSS parse error for {url}: {e}")
        return []


async def parse_vk_group(source_url: str) -> list[dict]:
    """Fetch recent posts from a VK group wall. source_url is group ID or short name."""
    try:
        group_id = source_url.strip().lstrip("-")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.vk.com/method/wall.get",
                params={
                    "domain": group_id,
                    "count": 5,
                    "access_token": settings.VK_APP_SERVICE_KEY,
                    "v": "5.199",
                },
            )
        data = resp.json()
        items = []
        for post in data.get("response", {}).get("items", []):
            text = post.get("text", "").strip()
            if text and len(text) > 30:
                items.append({"title": "", "text": text[:1000], "link": ""})
        return items
    except Exception as e:
        logger.error(f"VK group parse error for {source_url}: {e}")
        return []


async def fetch_and_schedule(group_id: int) -> int:
    """Fetch all sources for a group, rewrite via AI, schedule posts. Returns count."""
    sources = await get_content_sources(group_id)
    if not sources:
        return 0

    scheduled = 0
    for source in sources:
        if source.source_type == "rss":
            items = await parse_rss(source.source_url)
        elif source.source_type == "vk_group":
            items = await parse_vk_group(source.source_url)
        else:
            continue

        await update_source_fetched(source.id)

        # Filter by keywords if set
        keywords = [k.strip().lower() for k in source.filter_keywords.split(",") if k.strip()]

        for item in items[:3]:
            raw_text = item.get("text", "") or item.get("title", "")
            if not raw_text:
                continue

            if keywords and not any(kw in raw_text.lower() for kw in keywords):
                continue

            # Rewrite via AI
            rewritten = await generate_response(
                prompt=f"Перепиши этот текст своими словами для поста в группе ВКонтакте. "
                       f"Сделай его живым и интересным. Исходный текст:\n\n{raw_text}",
                system_prompt="Ты контент-менеджер группы ВКонтакте. Перепиши текст уникально, "
                              "без копирования. 3-8 предложений. Без хэштегов.",
                group_id=group_id,
            )

            # Schedule 2-6 hours from now
            from datetime import timedelta
            import random
            delay_hours = random.randint(2, 6)
            scheduled_at = datetime.now(timezone.utc) + timedelta(hours=delay_hours)

            await create_scheduled_post(
                group_id=group_id, text=rewritten,
                scheduled_at=scheduled_at, source="parsed",
            )
            scheduled += 1

    return scheduled
