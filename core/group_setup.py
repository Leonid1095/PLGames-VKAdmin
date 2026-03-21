"""Auto-setup AI personality for a newly connected group."""

import json
import logging
import httpx

from core.ai_brain import generate_response
from database.service import set_setting, get_setting

logger = logging.getLogger(__name__)


async def setup_group_ai(group_id: int, access_token: str) -> bool:
    """
    Analyze a VK group and generate tailored AI settings.
    Called after OAuth connect or manual /обновить command.
    Returns True on success.
    """
    logger.info(f"[SETUP] Starting AI setup for group {group_id}...")

    # 1. Fetch group info from VK
    group_info = await _fetch_group_info(group_id, access_token)
    if not group_info:
        logger.warning(f"[SETUP] Could not fetch info for group {group_id}")
        return False

    # 2. Fetch recent wall posts for tone analysis
    recent_posts = await _fetch_recent_posts(group_id, access_token)

    # 3. Build analysis prompt
    analysis = _build_analysis_text(group_info, recent_posts)

    # 4. Generate AI settings via LLM
    ai_settings = await _generate_ai_settings(analysis, group_id)
    if not ai_settings:
        logger.warning(f"[SETUP] AI settings generation failed for group {group_id}")
        return False

    # 5. Save to DB
    for key, value in ai_settings.items():
        await set_setting(group_id, key, value)

    logger.info(f"[SETUP] AI setup complete for group {group_id}: {list(ai_settings.keys())}")
    return True


async def _fetch_group_info(group_id: int, token: str) -> dict | None:
    """Fetch extended group info from VK API."""
    from core.http_retry import http_request_with_retry
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await http_request_with_retry(
                client, "GET",
                "https://api.vk.com/method/groups.getById",
                params={
                    "group_id": group_id,
                    "fields": "description,activity,status,members_count,counters",
                    "access_token": token,
                    "v": "5.199",
                },
            )
        data = resp.json()
        groups = data.get("response", {}).get("groups", data.get("response", []))
        if isinstance(groups, list) and groups:
            return groups[0]
        if isinstance(groups, dict):
            return groups
    except Exception as e:
        logger.error(f"Failed to fetch group info for {group_id}: {e}")
    return None


async def _fetch_recent_posts(group_id: int, token: str, count: int = 10) -> list[str]:
    """Fetch recent wall posts text for tone/topic analysis."""
    from core.http_retry import http_request_with_retry
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await http_request_with_retry(
                client, "GET",
                "https://api.vk.com/method/wall.get",
                params={
                    "owner_id": -group_id,
                    "count": count,
                    "access_token": token,
                    "v": "5.199",
                },
            )
        data = resp.json()
        posts = []
        for item in data.get("response", {}).get("items", []):
            text = item.get("text", "").strip()
            if text and len(text) > 20:
                posts.append(text[:500])
        return posts
    except Exception as e:
        logger.error(f"Failed to fetch posts for group {group_id}: {e}")
        return []


def _build_analysis_text(group_info: dict, recent_posts: list[str]) -> str:
    """Build a text block for LLM to analyze."""
    name = group_info.get("name", "")
    description = group_info.get("description", "")
    activity = group_info.get("activity", "")
    status = group_info.get("status", "")
    members = group_info.get("members_count", 0)

    parts = [f"Название группы: {name}"]
    if description:
        parts.append(f"Описание: {description[:500]}")
    if activity:
        parts.append(f"Тематика: {activity}")
    if status:
        parts.append(f"Статус: {status}")
    if members:
        parts.append(f"Участников: {members}")

    if recent_posts:
        parts.append("\nПоследние посты на стене:")
        for i, post in enumerate(recent_posts[:7], 1):
            parts.append(f"{i}. {post[:300]}")

    return "\n".join(parts)


async def _generate_ai_settings(analysis: str, group_id: int) -> dict | None:
    """Ask LLM to generate tailored settings based on group analysis."""
    system_prompt = (
        "Ты эксперт по настройке ИИ-администраторов для групп ВКонтакте. "
        "Проанализируй информацию о группе и сгенерируй настройки для ИИ-бота. "
        "Ответь СТРОГО в формате JSON (без markdown, без ```), содержащем следующие ключи:\n\n"
        '1. "ai_system_prompt" — системный промпт для чат-бота этой группы. '
        "Бот должен знать кто он (админ конкретной группы), разбираться в теме группы, "
        "общаться в соответствующем стиле. 3-5 предложений.\n\n"
        '2. "ai_moderation_rules" — правила модерации для этой группы. '
        "Что допустимо, что нет. Учитывай тематику. 2-3 предложения.\n\n"
        '3. "ai_content_topics" — 5-7 тем для генерации постов, через запятую. '
        "Темы должны соответствовать тематике группы.\n\n"
        '4. "ai_group_description" — краткое описание группы в 1 предложение '
        "(для контекста в других промптах).\n\n"
        '5. "ai_tone" — стиль общения одним словом: formal, casual, gaming, professional, friendly.\n\n'
        "Отвечай ТОЛЬКО JSON, никакого дополнительного текста."
    )

    result = await generate_response(
        prompt=f"Вот информация о группе:\n\n{analysis}",
        system_prompt=system_prompt,
        group_id=group_id,
    )

    # Parse JSON from response
    try:
        # Try to extract JSON if wrapped in markdown
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if "```" in cleaned:
                cleaned = cleaned[:cleaned.rfind("```")]
            cleaned = cleaned.strip()

        settings = json.loads(cleaned)

        # Validate required keys
        required = ["ai_system_prompt", "ai_moderation_rules", "ai_content_topics",
                     "ai_group_description", "ai_tone"]
        for key in required:
            if key not in settings:
                logger.warning(f"[SETUP] Missing key '{key}' in AI-generated settings")
                return None

        # Ensure all values are strings
        return {k: str(v) for k, v in settings.items() if k in required}

    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"[SETUP] Failed to parse AI settings JSON: {e}\nRaw: {result[:500]}")
        return None
