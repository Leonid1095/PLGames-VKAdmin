import logging
from openai import AsyncOpenAI
from core.config import settings

logger = logging.getLogger(__name__)

# ─── OpenRouter client ───────────────────────────────────────────────────────

def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.OPENROUTER_API_KEY,
    )

# ─── Core AI call ─────────────────────────────────────────────────────────────

async def _call_llm(messages: list[dict], model: str = None) -> str:
    """Low-level call to OpenRouter. Fetches model from DB if not provided."""
    if not model:
        # Lazy import to avoid circular dependency (ai_brain ↔ service)
        from database.service import get_setting
        model = await get_setting("active_model", settings.DEFAULT_MODEL)
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
        logger.error(f"OpenRouter error: {e}")
        return "Извините, произошла ошибка при обращении к ИИ. Попробуйте позже."

# ─── Public: Chat with memory ─────────────────────────────────────────────────

async def chat_with_memory(vk_id: int, user_text: str) -> str:
    """
    Main chat function. Loads user history, calls LLM, saves updated history.
    Returns the AI reply.
    """
    from database.service import get_setting, get_user_history, save_user_history, check_and_increment_limit, get_user_stats

    # ── Limits & Monetization Check ──
    can_request = await check_and_increment_limit(vk_id)
    if not can_request:
        return (
            "Ох, сервера устали! 😅 Мой начальник выдал мне лимит на бесплатные "
            "беседы, и на сегодня он исчерпан (10 запросов). "
            "Подключи VIP (напиши !купить), и мы сможем общаться без остановки, "
            "а я стану еще умнее!"
        )

    stats = await get_user_stats(vk_id)

    system_prompt = await get_setting(
        "system_prompt",
        "Ты вежливый и отзывчивый помощник-администратор группы ВКонтакте."
    )
    
    if stats.is_vip:
        system_prompt += (
            "\nВАЖНО: Ты общаешься с пользователем со статусом VIP 👑. "
            "Будь к нему максимально почтителен и услужлив."
        )

    model = await get_setting("active_model", settings.DEFAULT_MODEL)

    # Build message list: system + history + new user message
    history = await get_user_history(vk_id)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    reply = await _call_llm(messages, model=model)

    # Save updated history (append user message + assistant reply)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    await save_user_history(vk_id, history)

    return reply

# ─── Public: One-shot generation (no memory) ─────────────────────────────────

async def generate_response(prompt: str, system_prompt: str = "", model: str = None) -> str:
    """One-shot generation without memory (used for moderation, posts etc.)"""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return await _call_llm(messages, model=model)

# ─── Public: Moderation ───────────────────────────────────────────────────────

_MODERATION_PROMPTS = {
    "low":    "Удаляй только явный мат и прямые угрозы. Во всём остальном отвечай НЕТ.",
    "medium": "Удаляй мат, оскорбления, спам и ссылки на сторонние ресурсы.",
    "high":   "Удаляй мат, оскорбления, спам, ссылки, жалобы, негатив любого рода и рекламу.",
}

async def analyze_toxicity(text: str) -> bool:
    """
    Returns True if the comment should be deleted, False if it's acceptable.
    Respects the `moderation_aggressiveness` setting.
    """
    from database.service import get_setting

    aggressiveness = await get_setting("moderation_aggressiveness", "medium")
    extra = _MODERATION_PROMPTS.get(aggressiveness, _MODERATION_PROMPTS["medium"])

    system_prompt = (
        f"Ты строгий модератор сообщества ВКонтакте. {extra} "
        "Ответь ТОЛЬКО одним словом: ДА (если надо удалить) или НЕТ (если оставить)."
    )
    result = await generate_response(prompt=text, system_prompt=system_prompt)
    return bool(result and "ДА" in result.strip().upper())

# ─── Public: Post generation ──────────────────────────────────────────────────

async def generate_post(topic: str = "") -> str:
    """Generate a ready-to-publish VK wall post on the given topic."""
    from database.service import get_setting

    topics = topic or await get_setting("autopost_topics", "интересные факты")
    system_prompt = (
        "Ты контент-менеджер группы ВКонтакте. Напиши увлекательный, живой пост "
        "для публикации на стене. Без хэштегов в начале. Текст должен быть от 3 до 10 предложений."
    )
    return await generate_response(
        prompt=f"Напиши пост на одну из следующих тем: {topics}",
        system_prompt=system_prompt,
    )
