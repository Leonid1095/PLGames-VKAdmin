"""Content writer — smart AI copywriter for VK groups.

Core principle: NEVER write from nothing. Always have source material first.
"""

import logging

from core.ai_brain import generate_response, _get_group_ai_context
from core.web_reader import read_url

logger = logging.getLogger(__name__)


def _build_system_prompt(ctx: dict, instruction: str = "") -> str:
    """Build a group-aware system prompt for content writing."""
    group_hint = ""
    if ctx["ai_group_description"]:
        group_hint = f" Группа: {ctx['ai_group_description']}."

    tone_map = {
        "formal": "Пиши в деловом стиле.",
        "casual": "Пиши неформально, как друг.",
        "gaming": "Пиши в геймерском стиле, используй соответствующий сленг.",
        "professional": "Пиши профессионально и экспертно.",
        "friendly": "Пиши дружелюбно и увлекательно.",
    }
    tone_hint = tone_map.get(ctx.get("ai_tone", "friendly"), "")

    return (
        f"Ты профессиональный копирайтер и администратор группы ВКонтакте.{group_hint} "
        f"{tone_hint} "
        "Правила:\n"
        "- Пиши грамотным, живым русским языком\n"
        "- Можно использовать эмодзи где уместно\n"
        "- Никогда не добавляй хэштеги\n"
        "- Не лей воду, каждое предложение должно нести смысл\n"
        "- Структурируй текст: используй абзацы, подзаголовки если текст длинный\n"
        "- Пиши так, чтобы пост хотелось дочитать до конца\n"
        "- Длину определяй по содержанию: если материала много — пиши развёрнуто (15-30 предложений), "
        "если тема простая — 5-10 предложений. Не тяни и не сокращай искусственно."
    )


async def write_from_source(
    group_id: int,
    source_material: str,
    instruction: str = "",
) -> str:
    """
    Write a post based on real source material.
    This is the primary content creation method — always use source data.
    """
    ctx = await _get_group_ai_context(group_id)
    system_prompt = _build_system_prompt(ctx, instruction)

    task = instruction or "Напиши пост для стены группы ВКонтакте на основе этого материала"

    user_prompt = (
        f"Задание: {task}\n\n"
        f"Исходный материал:\n{source_material}\n\n"
        "Выдели самое интересное и важное. Перескажи своими словами, "
        "добавь свой взгляд и полезные выводы для читателей группы."
    )

    return await generate_response(
        prompt=user_prompt,
        system_prompt=system_prompt,
        group_id=group_id,
    )


async def write_from_url(
    group_id: int,
    url: str,
    instruction: str = "",
) -> str:
    """Fetch URL content, then write a post based on it."""
    logger.info(f"[WRITER] Fetching: {url}")
    content = await read_url(url)
    if content.startswith("Ошибка"):
        return f"Не удалось загрузить: {content}"
    if len(content.strip()) < 50:
        return "Не удалось извлечь достаточно контента со страницы."

    return await write_from_source(group_id, content, instruction)


async def write_from_multiple_sources(
    group_id: int,
    sources: list[dict],
    instruction: str = "",
) -> str:
    """
    Write a post/digest from multiple source items.
    Each source: {"title": str, "text": str, "link": str}
    """
    if not sources:
        return ""

    # Combine sources into one material block
    parts = []
    for i, src in enumerate(sources, 1):
        title = src.get("title", "")
        text = src.get("text", "")
        link = src.get("link", "")
        entry = f"--- Источник {i} ---"
        if title:
            entry += f"\nЗаголовок: {title}"
        if text:
            entry += f"\n{text}"
        if link:
            entry += f"\nСсылка: {link}"
        parts.append(entry)

    combined = "\n\n".join(parts)

    # If sources have links and texts are short, try to fetch full content
    # from the first source for better material
    if sources[0].get("link") and len(sources[0].get("text", "")) < 200:
        try:
            full_content = await read_url(sources[0]["link"])
            if not full_content.startswith("Ошибка") and len(full_content) > 200:
                combined = f"--- Полный текст первого источника ---\n{full_content}\n\n{combined}"
        except Exception:
            pass

    return await write_from_source(group_id, combined, instruction)


async def write_article(
    group_id: int,
    source_url: str = "",
    instruction: str = "",
    length: str = "medium",
) -> str:
    """
    Write an article post — wrapper over write_from_url/write_from_source.
    length: 'short' (~5 sentences), 'medium' (~10-15), 'long' (~20-30).
    """
    length_hints = {
        "short": "Напиши кратко, 5-7 предложений.",
        "medium": "Напиши статью средней длины, 10-15 предложений.",
        "long": "Напиши развёрнутую статью, 20-30 предложений.",
    }
    length_hint = length_hints.get(length, length_hints["medium"])
    full_instruction = f"{instruction} {length_hint}".strip() if instruction else length_hint

    if source_url:
        return await write_from_url(group_id, source_url, full_instruction)

    # No URL — generate from instruction alone using write_from_source
    if not instruction:
        return "Не указан ни URL-источник, ни инструкция для статьи."
    return await write_from_source(group_id, instruction, full_instruction)


async def write_patch_notes(
    group_id: int,
    github_url: str,
    days: int = 7,
) -> str:
    """Generate patch notes from a GitHub repository."""
    from urllib.parse import urlparse
    from core.web_reader import read_github_commits

    parsed = urlparse(github_url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return "Неверная ссылка на GitHub. Формат: https://github.com/owner/repo"

    owner, repo = parts[0], parts[1].removesuffix(".git")

    commits_data = await read_github_commits(owner, repo, since_days=days)
    if commits_data.startswith("Ошибка") or commits_data.startswith("Нет коммитов"):
        return commits_data

    ctx = await _get_group_ai_context(group_id)
    group_hint = f" Группа: {ctx['ai_group_description']}." if ctx["ai_group_description"] else ""

    system_prompt = (
        f"Ты технический копирайтер и администратор группы ВКонтакте.{group_hint}\n\n"
        "Твоя задача — написать красивый, структурированный патч-нот для поста на стене ВК.\n\n"
        "Правила оформления:\n"
        "1. Начни с яркого заголовка с эмодзи, например: 🚀 Обновление v1.X — Что нового?\n"
        "2. Группируй изменения по категориям с эмодзи:\n"
        "   ✨ Новые возможности\n"
        "   🛠 Исправления\n"
        "   ⚡ Улучшения\n"
        "   🔒 Безопасность\n"
        "3. Каждый пункт — короткое, понятное предложение для обычных пользователей\n"
        "4. Технические коммиты (рефакторинг, CI, merge) — пропускай\n"
        "5. В конце добавь 1-2 предложения с призывом: обновляйтесь, пишите отзывы и т.д.\n"
        "6. Используй разделители (─── или пустые строки) между секциями\n"
        "7. Никогда не добавляй хэштеги\n"
        "8. Пиши живым языком, не сухо — будто рассказываешь другу что изменилось"
    )

    return await generate_response(
        prompt=(
            f"Вот коммиты за последние {days} дней из репозитория {owner}/{repo}:\n\n"
            f"{commits_data}\n\n"
            "Напиши красивый, развёрнутый патч-нот для поста ВКонтакте. "
            "Объясни каждое изменение понятно для обычного пользователя."
        ),
        system_prompt=system_prompt,
        group_id=group_id,
    )
