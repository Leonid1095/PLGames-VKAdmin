"""Content parser — fetch posts from external sources and create real content."""

import hashlib
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
    create_scheduled_post, get_setting, set_setting,
)

logger = logging.getLogger(__name__)


def _item_hash(item: dict) -> str:
    """Generate a short hash of an item for deduplication."""
    key = (item.get("title", "") + item.get("link", "") + item.get("text", "")[:200]).strip()
    return hashlib.md5(key.encode()).hexdigest()[:12]


async def _get_used_hashes(group_id: int) -> set[str]:
    """Get set of already-used content hashes for a group."""
    raw = await get_setting(group_id, "_used_content_hashes", "")
    if not raw:
        return set()
    return set(raw.split(","))


async def _save_used_hash(group_id: int, h: str) -> None:
    """Append a hash to the used set (keep last 200)."""
    existing = await _get_used_hashes(group_id)
    existing.add(h)
    # Keep only last 200 hashes
    trimmed = list(existing)[-200:]
    await set_setting(group_id, "_used_content_hashes", ",".join(trimmed))


async def parse_rss(url: str) -> list[dict]:
    """Fetch and parse an RSS feed. Returns list of {title, text, link, image_url}."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)",
            })
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:10]:
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))
            # Extract first image from HTML summary before stripping tags
            image_url = ""
            img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary or "")
            if img_match:
                image_url = img_match.group(1)
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            link = entry.get("link", "")

            # Try media:content or enclosure for image
            if not image_url:
                media = entry.get("media_content", [])
                if media and isinstance(media, list):
                    for m in media:
                        if m.get("medium") == "image" or (m.get("url", "").split("?")[0].split(".")[-1] in ("jpg", "jpeg", "png", "webp")):
                            image_url = m.get("url", "")
                            break
            if not image_url:
                enclosures = entry.get("enclosures", [])
                for enc in enclosures:
                    if enc.get("type", "").startswith("image/"):
                        image_url = enc.get("href", enc.get("url", ""))
                        break

            if title:
                items.append({
                    "title": title, "text": summary[:500],
                    "link": link, "image_url": image_url,
                })
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
                    "count": 10,
                    "access_token": settings.VK_APP_SERVICE_KEY,
                    "v": "5.199",
                },
            )
        data = resp.json()
        items = []
        for post in data.get("response", {}).get("items", []):
            text = post.get("text", "").strip()
            if text and len(text) > 30:
                # Extract first photo attachment
                image_url = ""
                for att in post.get("attachments", []):
                    if att.get("type") == "photo":
                        sizes = att.get("photo", {}).get("sizes", [])
                        # Pick largest size
                        if sizes:
                            best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                            image_url = best.get("url", "")
                        break

                items.append({
                    "title": "", "text": text[:1000],
                    "link": "", "image_url": image_url,
                })
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
        for entry in data[:10]:
            title = entry.get("title") or entry.get("title_ru") or ""
            text = entry.get("text") or entry.get("text_ru") or entry.get("description") or ""
            text = re.sub(r"<[^>]+>", "", text).strip()
            link = entry.get("link") or entry.get("url") or ""
            image_url = entry.get("image") or entry.get("image_url") or entry.get("thumbnail") or ""
            if title or text:
                items.append({
                    "title": title, "text": text[:500],
                    "link": link, "image_url": image_url,
                })
        return items
    except Exception as e:
        logger.error(f"API parse error for {url}: {e}")
        return []


async def parse_web(url: str) -> list[dict]:
    """Fetch a web page and return as single source item."""
    content = await read_url(url)
    if content.startswith("Ошибка") or len(content.strip()) < 50:
        return []
    return [{"title": "", "text": content[:3000], "link": url, "image_url": ""}]


async def _download_image(url: str) -> bytes | None:
    """Download an image from URL, return bytes or None."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)",
            })
            if resp.status_code == 200 and len(resp.content) > 1000:
                content_type = resp.headers.get("content-type", "")
                if "image" in content_type or url.split("?")[0].split(".")[-1] in ("jpg", "jpeg", "png", "webp", "gif"):
                    return resp.content
    except Exception as e:
        logger.warning(f"Image download failed {url}: {e}")
    return None


async def fetch_and_schedule(group_id: int) -> int:
    """
    Fetch all sources for a group, create quality content with images, schedule posts.
    Deduplicates by content hash to avoid repeats.
    """
    sources = await get_content_sources(group_id)
    if not sources:
        return 0

    used_hashes = await _get_used_hashes(group_id)
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

        # Filter out already-used items
        fresh_items = [it for it in items if _item_hash(it) not in used_hashes]
        if not fresh_items:
            logger.info(f"Source #{source.id}: all {len(items)} items already used, skipping")
            continue

        # Pick a random item from fresh ones (weighted towards longer content)
        fresh_items.sort(key=lambda it: len(it.get("text", "")), reverse=True)
        # Pick from top 3 longest items randomly
        candidates = fresh_items[:min(3, len(fresh_items))]
        chosen = random.choice(candidates)

        # Mark as used
        await _save_used_hash(group_id, _item_hash(chosen))

        # If item has a link and text is short — fetch full content from link
        source_text = chosen.get("text", "")
        title = chosen.get("title", "")
        link = chosen.get("link", "")
        image_url = chosen.get("image_url", "")

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
            instruction=(
                "Напиши пост для группы ВКонтакте на основе этого материала. "
                "Используй ТОЛЬКО факты из исходного материала — не придумывай ничего от себя. "
                "Перескажи своими словами, выдели самое интересное и полезное."
            ),
        )

        if not post_text or post_text.startswith("Извините") or len(post_text.strip()) < 50:
            continue

        # Upload image: source image > Pexels search > no image
        attachment = ""
        try:
            from core.images import upload_photo_to_vk, find_and_upload_image, download_image_from_url
            from database.service import get_group
            from core.crypto import decrypt_token
            from vkbottle import API

            group = await get_group(group_id)
            if group:
                token = decrypt_token(group.access_token)
                api = API(token=token)

                # Try source image first
                if image_url:
                    image_bytes = await download_image_from_url(image_url)
                    if image_bytes:
                        attachment = await upload_photo_to_vk(api, group_id, image_bytes) or ""

                # Fallback: Pexels search by post keywords
                if not attachment:
                    attachment = await find_and_upload_image(api, group_id, post_text=post_text) or ""
        except Exception as img_err:
            logger.warning(f"Image handling failed for source #{source.id}: {img_err}")

        # Schedule 1-4 hours from now
        delay_hours = random.randint(1, 4)
        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=delay_hours)

        await create_scheduled_post(
            group_id=group_id, text=post_text,
            scheduled_at=scheduled_at, source="parsed",
            attachments=attachment,
        )
        scheduled += 1
        logger.info(f"Scheduled post from source #{source.id} for group {group_id} (image: {'yes' if attachment else 'no'})")

        # Max 1 post per source per cycle to avoid spam
        break

    return scheduled
