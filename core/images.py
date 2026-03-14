"""Image helpers — find relevant images and upload to VK."""

import io
import logging
import httpx

logger = logging.getLogger(__name__)


_COVER_STYLES = {
    "patch_notes": {"bg": "1976d2", "fg": "ffffff", "icon": "🚀", "label": "Обновление"},
    "article": {"bg": "2e7d32", "fg": "ffffff", "icon": "📝", "label": "Статья"},
    "digest": {"bg": "7b1fa2", "fg": "ffffff", "icon": "📰", "label": "Дайджест"},
    "default": {"bg": "455a64", "fg": "ffffff", "icon": "📌", "label": "Новый пост"},
}


async def find_and_download_image(query: str, post_type: str = "default") -> bytes | None:
    """
    Generate a cover image for a post.
    Uses placehold.co to create a branded cover with text.
    Returns image bytes or None.
    """
    from urllib.parse import quote

    style = _COVER_STYLES.get(post_type, _COVER_STYLES["default"])
    text = f"{style['icon']}  {style['label']}"
    encoded_text = quote(text)

    url = f"https://placehold.co/800x400/{style['bg']}/{style['fg']}.jpg?text={encoded_text}&font=roboto"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; VKAdminBot/1.0)",
            })
            if resp.status_code == 200 and len(resp.content) > 1000:
                logger.info(f"Cover image generated: {len(resp.content)} bytes ({post_type})")
                return resp.content

        logger.warning(f"Cover generation failed: HTTP {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"Cover generation failed: {e}")
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


async def find_and_upload_image(api, group_id: int, query: str = "", post_type: str = "default") -> str | None:
    """
    Generate a cover image, upload to VK.
    Returns VK attachment string or None.
    """
    image_bytes = await find_and_download_image(query, post_type)
    if not image_bytes:
        return None

    attachment = await upload_photo_to_vk(api, group_id, image_bytes)
    if attachment:
        logger.info(f"Image uploaded for group {group_id}: {attachment}")
    return attachment
