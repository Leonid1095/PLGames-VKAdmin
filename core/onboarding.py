"""Onboarding — dialog-based setup for new groups.

Instead of sending admin to a dashboard with 18 settings,
the bot asks 3 simple questions in chat and configures itself.
"""

import logging
from core.group_context import GroupContext
from core.ai_brain import generate_response

logger = logging.getLogger(__name__)


async def is_onboarding_needed(group_id: int) -> bool:
    """Check if this group hasn't been onboarded yet."""
    from database.service import get_setting
    return (await get_setting(group_id, "onboarding_complete", "false")) != "true"


async def run_onboarding(ctx: GroupContext, user_id: int, text: str) -> str | None:
    """Handle onboarding dialog. Returns response or None if onboarding is complete."""
    from database.service import get_setting, set_setting

    if not await is_onboarding_needed(ctx.group_id):
        return None

    step = await get_setting(ctx.group_id, "_onboarding_step", "0")

    if step == "0":
        # First contact — greet and ask about the group
        # Try to auto-detect group info first
        group_info = ""
        try:
            groups = await ctx.api.groups.get_by_id(group_id=ctx.group_id, fields=["description", "members_count"])
            if groups:
                g = groups[0]
                group_info = f" Я вижу, что группа называется «{g.name}»"
                if hasattr(g, 'description') and g.description:
                    group_info += f" и в описании написано: «{g.description}»"
                group_info += "."
        except Exception:
            pass

        await set_setting(ctx.group_id, "_onboarding_step", "1")
        return (
            f"Привет! Я буду AI-администратором вашей группы.{group_info}\n\n"
            "Расскажите в пару предложений, о чём ваша группа и кто ваша аудитория?\n\n"
            "(Например: «Игровое сообщество по Minecraft для русскоязычных игроков» "
            "или «Новости технологий для разработчиков»)"
        )

    if step == "1":
        # Got group description — save and ask about moderation
        await set_setting(ctx.group_id, "group_description", text.strip())
        await set_setting(ctx.group_id, "_onboarding_step", "2")
        return (
            "Отлично, запомнил!\n\n"
            "Насколько строго модерировать комментарии?\n\n"
            "1 — Минимально (только мат и угрозы)\n"
            "2 — Легко (мат и оскорбления)\n"
            "3 — Средне (+ спам и ссылки)\n"
            "4 — Строго (+ негатив)\n"
            "5 — Максимально (удалять всё подозрительное)\n\n"
            "Напишите число от 1 до 5:"
        )

    if step == "2":
        # Got moderation level — save and ask about autoposting
        level = text.strip()
        try:
            level_int = max(1, min(5, int(level)))
        except ValueError:
            return "Пожалуйста, напишите число от 1 до 5:"

        await set_setting(ctx.group_id, "moderation_level", str(level_int))
        await set_setting(ctx.group_id, "_onboarding_step", "3")
        return (
            f"Уровень модерации: {level_int}. Понял!\n\n"
            "Хотите, чтобы я сам публиковал контент?\n"
            "Я могу находить интересные материалы по теме группы и писать посты.\n\n"
            "Напишите «да» или «нет»:"
        )

    if step == "3":
        # Got autopost preference — finish onboarding
        answer = text.strip().lower()
        autopost = answer in ("да", "yes", "1", "true", "конечно", "давай", "ага", "хочу")
        await set_setting(ctx.group_id, "autopost_enabled", "true" if autopost else "false")

        # Now auto-configure AI based on the group description
        group_desc = await get_setting(ctx.group_id, "group_description", "")
        await _auto_configure_ai(ctx, group_desc)

        await set_setting(ctx.group_id, "onboarding_complete", "true")
        # Clean up temp step
        await set_setting(ctx.group_id, "_onboarding_step", "done")

        autopost_msg = (
            "Автопостинг включён! Я буду находить и публиковать интересный контент. "
            "Добавьте источники — просто пришлите мне ссылку на RSS, сайт или группу ВК."
        ) if autopost else (
            "Автопостинг выключен. Когда захотите — просто скажите."
        )

        return (
            f"Готово! Я настроился под вашу группу.\n\n"
            f"{autopost_msg}\n\n"
            "Что я умею:\n"
            "— Писать и публиковать посты (просто попросите)\n"
            "— Модерировать комментарии (автоматически)\n"
            "— Отвечать на вопросы участников\n"
            "— Показывать статистику группы\n"
            "— Банить нарушителей\n"
            "— Делать рассылки\n\n"
            "Просто пишите мне на обычном языке. Не нужно запоминать команды!"
        )

    # Onboarding already complete
    return None


async def _auto_configure_ai(ctx: GroupContext, group_description: str):
    """Use AI to generate personality and settings from group description."""
    from database.service import set_setting

    if not group_description:
        return

    # First try full group_setup if token is available
    try:
        from core.crypto import decrypt_token
        from database.service import get_group
        from core.group_setup import setup_group_ai

        group = await get_group(ctx.group_id)
        if group:
            token = decrypt_token(group.access_token)
            ok = await setup_group_ai(ctx.group_id, token)
            if ok:
                logger.info(f"[ONBOARDING] Full AI setup complete for group {ctx.group_id}")
                return
    except Exception as e:
        logger.warning(f"[ONBOARDING] Full setup failed, falling back to description-based: {e}")

    # Fallback: generate from description alone
    prompt = (
        f"Группа ВКонтакте: {group_description}\n\n"
        "Сгенерируй системный промпт для AI-администратора этой группы. "
        "Он должен:\n"
        "1. Знать тематику группы\n"
        "2. Общаться в подходящем стиле (формально/неформально/игровой сленг)\n"
        "3. Разбираться в теме на базовом уровне\n\n"
        "Ответь ТОЛЬКО текстом промпта, без пояснений."
    )

    system_prompt = await generate_response(prompt=prompt)
    if system_prompt and not system_prompt.startswith("Извините"):
        await set_setting(ctx.group_id, "ai_system_prompt", system_prompt)
        await set_setting(ctx.group_id, "ai_group_description", group_description)
        logger.info(f"[ONBOARDING] AI configured from description for group {ctx.group_id}")
