"""Image helpers — find relevant images and upload to VK.

Image sourcing priority:
1. Image from source content (RSS/VK/API) — passed directly as bytes
2. Pexels keyword search (free API, 200 req/hr) — thematic images
3. No image — post goes without attachment (better than random/placeholder)
"""

import io
import logging
import random

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


async def search_image(query: str) -> bytes | None:
    """
    Search for a thematic image by keywords.
    Uses Pexels API (free, 200 requests/hour).
    Returns image bytes or None.
    """
    if not settings.PEXELS_API_KEY:
        return None

    return await _search_pexels(query)


async def _search_pexels(query: str) -> bytes | None:
    """Search Pexels for a photo matching the query."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Search for photos
            resp = await client.get(
                "https://api.pexels.com/v1/search",
                params={
                    "query": query,
                    "per_page": 15,
                    "orientation": "landscape",
                    "size": "medium",
                },
                headers={"Authorization": settings.PEXELS_API_KEY},
            )
            if resp.status_code != 200:
                logger.warning(f"Pexels search failed: HTTP {resp.status_code}")
                return None

            data = resp.json()
            photos = data.get("photos", [])
            if not photos:
                logger.info(f"Pexels: no results for '{query}'")
                return None

            # Pick a random photo from results
            photo = random.choice(photos)
            image_url = photo.get("src", {}).get("large", "")
            if not image_url:
                return None

            # Download the image
            img_resp = await client.get(image_url)
            if img_resp.status_code == 200 and len(img_resp.content) > 5000:
                logger.info(f"Pexels image fetched for '{query}': {len(img_resp.content)} bytes")
                return img_resp.content

    except Exception as e:
        logger.warning(f"Pexels search failed for '{query}': {e}")
    return None


async def extract_topic_keywords(post_text: str) -> str:
    """
    Extract 2-3 keyword topic from post text for image search.
    Simple heuristic: take first meaningful words from post.
    """
    # Remove emoji and special chars
    import re
    clean = re.sub(r'[^\w\s]', '', post_text)
    words = [w for w in clean.split() if len(w) > 3]
    # Take 2-3 keywords from the beginning (topic is usually there)
    keywords = words[:3]
    return " ".join(keywords) if keywords else ""


async def download_image_from_url(url: str) -> bytes | None:
    """Download an image from URL. Returns bytes or None."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)",
            })
            if resp.status_code == 200 and len(resp.content) > 1000:
                content_type = resp.headers.get("content-type", "")
                ext = url.split("?")[0].split(".")[-1].lower()
                if "image" in content_type or ext in ("jpg", "jpeg", "png", "webp", "gif"):
                    return resp.content
    except Exception as e:
        logger.warning(f"Image download failed {url}: {e}")
    return None


async def upload_photo_to_vk(api, group_id: int, image_bytes: bytes) -> str | None:
    """
    Upload a photo to VK and return attachment string like 'photo123_456'.
    Uses VK wall photo upload flow:
    1. Get upload server URL
    2. Upload photo to server
    3. Save wall photo
    """
    try:
        # Step 1: Get upload URL
        upload_server = await api.photos.get_wall_upload_server(group_id=group_id)
        upload_url = upload_server.upload_url

        # Step 2: Upload image
        async with httpx.AsyncClient(timeout=30) as client:
            files = {"photo": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")}
            resp = await client.post(upload_url, files=files)
            upload_data = resp.json()

        if not upload_data.get("photo") or upload_data["photo"] == "[]":
            logger.warning("VK photo upload returned empty photo")
            return None

        # Step 3: Save wall photo
        saved = await api.photos.save_wall_photo(
            group_id=group_id,
            photo=upload_data["photo"],
            server=upload_data["server"],
            hash=upload_data["hash"],
        )

        if saved:
            photo = saved[0]
            return f"photo{photo.owner_id}_{photo.id}"

        return None

    except Exception as e:
        logger.error(f"VK photo upload failed for group {group_id}: {e}")
        return None


async def find_and_upload_image(
    api,
    group_id: int,
    query: str = "",
    post_type: str = "default",
    post_text: str = "",
) -> str | None:
    """
    Find a thematic image and upload to VK.
    Returns VK attachment string or None.

    Priority:
    1. Pexels search by query/post_text keywords
    2. None (no image is better than a random/ugly placeholder)
    """
    # Check if image search is enabled for this group
    from database.service import get_setting
    enabled = (await get_setting(group_id, "image_search_enabled", "true")).lower()
    if enabled != "true":
        return None

    # Build search query
    search_query = query
    if not search_query and post_text:
        search_query = await extract_topic_keywords(post_text)
    if not search_query:
        return None

    image_bytes = await search_image(search_query)
    if not image_bytes:
        return None

    attachment = await upload_photo_to_vk(api, group_id, image_bytes)
    if attachment:
        logger.info(f"Image uploaded for group {group_id}: {attachment}")
    return attachment
