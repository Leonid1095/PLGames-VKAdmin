import logging
from datetime import datetime, timezone
from openai import AsyncOpenAI
from core.config import settings

logger = logging.getLogger(__name__)

# ─── OpenRouter client ───────────────────────────────────────────────────────

def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.OPENROUTER_BASE_URL,
        api_key=settings.OPENROUTER_API_KEY,
    )

# ─── Core AI call ─────────────────────────────────────────────────────────────

async def _call_llm(messages: list[dict], model: str = None, group_id: int = None) -> str:
    """Low-level call to AI provider. Fetches model from DB if not provided."""
    if not model:
        from database.service import get_setting
        if group_id:
            model = await get_setting(group_id, "active_model", settings.DEFAULT_MODEL)
        else:
            model = settings.DEFAULT_MODEL
    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            extra_headers={
                "HTTP-Referer": "https://github.com/vk-ai-admin",
                "X-Title": "VK AI Admin Bot",
            }
        )
        content = response.choices[0].message.content
        if content is None:
            return "ИИ вернул пустой ответ."
        return content
    except Exception as e:
        logger.error(f"AI provider error: {e}")
        return "Извините, произошла ошибка при обращении к ИИ. Попробуйте позже."

# ─── Public: Chat with memory ─────────────────────────────────────────────────

async def chat_with_memory(group_id: int, vk_id: int, user_text: str) -> str:
    """Main chat function with per-group memory."""
    from database.service import (
        get_setting, get_user_history, save_user_history,
        check_and_increment_limit, get_user_stats,
    )

    can_request = await check_and_increment_limit(group_id, vk_id)
    if not can_request:
        return (
            "Ох, сервера устали! Мой начальник выдал мне лимит на бесплатные "
            "беседы, и на сегодня он исчерпан (10 запросов). "
            "Подключи VIP (напиши !купить), и мы сможем общаться без остановки!"
        )

    stats = await get_user_stats(group_id, vk_id)

    system_prompt = await get_setting(
        group_id, "system_prompt",
        "Ты вежливый и отзывчивый помощник-администратор группы ВКонтакте."
    )

    is_vip_active = stats.is_vip and (
        not stats.vip_expires or stats.vip_expires > datetime.now(timezone.utc)
    )
    if is_vip_active:
        system_prompt += (
            "\nВАЖНО: Ты общаешься с пользователем со статусом VIP. "
            "Будь к нему максимально почтителен и услужлив."
        )

    model = await get_setting(group_id, "active_model", settings.DEFAULT_MODEL)

    history = await get_user_history(group_id, vk_id)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    reply = await _call_llm(messages, model=model)

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    await save_user_history(group_id, vk_id, history)

    return reply

# ─── Public: One-shot generation (no memory) ─────────────────────────────────

async def generate_response(prompt: str, system_prompt: str = "", model: str = None, group_id: int = None) -> str:
    """One-shot generation without memory."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return await _call_llm(messages, model=model, group_id=group_id)

# ─── Public: Moderation ───────────────────────────────────────────────────────

_MODERATION_PROMPTS = {
    "low":    "Удаляй только явный мат и прямые угрозы. Во всём остальном отвечай НЕТ.",
    "medium": "Удаляй мат, оскорбления, спам и ссылки на сторонние ресурсы.",
    "high":   "Удаляй мат, оскорбления, спам, ссылки, жалобы, негатив любого рода и рекламу.",
}

async def analyze_toxicity(group_id: int, text: str) -> bool:
    """Returns True if the comment should be deleted."""
    from database.service import get_setting

    aggressiveness = await get_setting(group_id, "moderation_aggressiveness", "medium")
    extra = _MODERATION_PROMPTS.get(aggressiveness, _MODERATION_PROMPTS["medium"])

    system_prompt = (
        f"Ты строгий модератор сообщества ВКонтакте. {extra} "
        "Ответь ТОЛЬКО одним словом: ДА (если надо удалить) или НЕТ (если оставить)."
    )
    result = await generate_response(prompt=text, system_prompt=system_prompt, group_id=group_id)
    return bool(result and "ДА" in result.strip().upper())

# ─── Public: Post generation ──────────────────────────────────────────────────

async def generate_post(group_id: int, topic: str = "") -> str:
    """Generate a ready-to-publish VK wall post."""
    from database.service import get_setting

    topics = topic or await get_setting(group_id, "autopost_topics", "интересные факты")
    system_prompt = (
        "Ты контент-менеджер группы ВКонтакте. Напиши увлекательный, живой пост "
        "для публикации на стене. Без хэштегов в начале. Текст должен быть от 3 до 10 предложений."
    )
    return await generate_response(
        prompt=f"Напиши пост на одну из следующих тем: {topics}",
        system_prompt=system_prompt,
        group_id=group_id,
    )
