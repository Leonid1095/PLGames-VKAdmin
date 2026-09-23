"""Что можно публиковать от имени сообщества, а что — заглушка сбоя.

_call_llm при ошибке возвращает строку-заглушку вместо исключения, и раньше
каждый вызывающий проверял свой набор префиксов — часть путей публиковала
«Извините, произошла ошибка…» постом, комментарием или приветствием.
Все публичные пути проверяют текст здесь.
"""

# Ровно то, что возвращает core.ai_brain._call_llm при сбое.
LLM_FAILURE_PREFIXES = (
    "Извините, произошла ошибка",
    "ИИ вернул пустой ответ",
)

# Сгенерированный текст, начинающийся так, — ошибка или «нечего сказать»,
# а не пост. В прошлом «Нет коммитов за 7 дней» уходило на стену.
FAILURE_PREFIXES = LLM_FAILURE_PREFIXES + (
    "Ошибка", "Не удалось", "Извините", "Нет коммитов",
    "Произошла ошибка", "Не могу", "Неверная ссылка", "Unknown",
)


def is_llm_failure(text: str | None) -> bool:
    """Пусто или заглушка сбоя ИИ — такое нельзя отправлять людям."""
    if not text or not text.strip():
        return True
    return text.strip().startswith(LLM_FAILURE_PREFIXES)


def is_publishable(text: str | None) -> bool:
    """Сгенерированный пост пригоден для стены: не ошибка и не огрызок."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 50:
        return False
    return not stripped.startswith(FAILURE_PREFIXES)
