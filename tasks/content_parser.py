"""Content parser — fetch posts from external sources and create real content."""

import re
import logging
import random
from datetime import datetime, timezone, timedelta

import feedparser
import httpx

from core.config import settings
from core.content_writer import write_from_source, write_from_multiple_sources
from core.web_reader import read_url
from database.service import (
    get_content_sources, update_source_fetched,
    create_scheduled_post,
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
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            link = entry.get("link", "")
            if title:
                items.append({"title": title, "text": summary[:500], "link": link})
        return items
    except Exception as e:
        logger.error(f"RSS parse error for {url}: {e}")
        return []


async def parse_vk_group(source_url: str) -> list[dict]:
    """Fetch recent posts from a VK group wall."""
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


async def parse_api(url: str) -> list[dict]:
    """Fetch news from a JSON API."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("items", data.get("news", data.get("data", [])))
        if not isinstance(data, list):
            return []
        items = []
        for entry in data[:5]:
            title = entry.get("title") or entry.get("title_ru") or ""
            text = entry.get("text") or entry.get("text_ru") or entry.get("description") or ""
            text = re.sub(r"<[^>]+>", "", text).strip()
            link = entry.get("link") or entry.get("url") or ""
            if title or text:
                items.append({"title": title, "text": text[:500], "link": link})
        return items
    except Exception as e:
        logger.error(f"API parse error for {url}: {e}")
        return []


async def parse_web(url: str) -> list[dict]:
    """Fetch a web page and return as single source item."""
    content = await read_url(url)
    if content.startswith("Ошибка") or len(content.strip()) < 50:
        return []
    return [{"title": "", "text": content[:3000], "link": url}]


async def fetch_and_schedule(group_id: int) -> int:
    """
    Fetch all sources for a group, create quality content, schedule posts.
    Key change: reads FULL source content, writes proper posts (not 3-sentence rewrites).
    """
    sources = await get_content_sources(group_id)
    if not sources:
        return 0

    scheduled = 0
    for source in sources:
        try:
            if source.source_type == "rss":
                items = await parse_rss(source.source_url)
            elif source.source_type == "vk_group":
                items = await parse_vk_group(source.source_url)
            elif source.source_type == "api":
                items = await parse_api(source.source_url)
            elif source.source_type == "web":
                items = await parse_web(source.source_url)
            else:
                continue
        except Exception as e:
            logger.error(f"Parse error for source #{source.id}: {e}")
            continue

        await update_source_fetched(source.id)

        if not items:
            continue

        # Filter by keywords if set
        keywords = [k.strip().lower() for k in source.filter_keywords.split(",") if k.strip()]
        if keywords:
            items = [
                it for it in items
                if any(kw in (it.get("text", "") + it.get("title", "")).lower() for kw in keywords)
            ]

        if not items:
            continue

        # Pick best item (first one that has enough content)
        best_item = items[0]
        for item in items:
            if len(item.get("text", "")) > len(best_item.get("text", "")):
                best_item = item

        # If item has a link and text is short — fetch full content from link
        source_text = best_item.get("text", "")
        title = best_item.get("title", "")
        link = best_item.get("link", "")

        if link and len(source_text) < 300:
            try:
                full_content = await read_url(link)
                if not full_content.startswith("Ошибка") and len(full_content) > len(source_text):
                    source_text = full_content[:4000]
            except Exception:
                pass

        # Build full material for the writer
        material = ""
        if title:
            material += f"Заголовок: {title}\n\n"
        material += source_text
        if link:
            material += f"\n\nИсточник: {link}"

        if len(material.strip()) < 100:
            continue

        # Write a real post using the content writer
        post_text = await write_from_source(
            group_id=group_id,
            source_material=material,
            instruction="Напиши пост для группы ВКонтакте на основе этого материала. "
                       "Перескажи своими словами, выдели главное.",
        )

        if not post_text or post_text.startswith("Извините") or len(post_text.strip()) < 50:
            continue

        # Schedule 1-4 hours from now
        delay_hours = random.randint(1, 4)
        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=delay_hours)

        await create_scheduled_post(
            group_id=group_id, text=post_text,
            scheduled_at=scheduled_at, source="parsed",
        )
        scheduled += 1

        # Max 1 post per source per cycle to avoid spam
        break

    return scheduled
