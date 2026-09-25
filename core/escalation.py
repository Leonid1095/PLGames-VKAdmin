"""Эскалация к живому админу: уведомить менеджеров, замолчать в диалоге."""

import logging
from datetime import datetime, timezone, timedelta

from core.group_context import GroupContext
from database.service import create_escalation, set_human_mode

logger = logging.getLogger(__name__)

HANDOFF_HOURS = 24  # бот молчит в диалоге после эскалации


async def _get_manager_ids(ctx: GroupContext) -> list[int]:
    """Все менеджеры группы; при недоступности API — хотя бы владелец.

    Сырой request вместо типизированного метода: vkbottle падает на валидации
    ответа groups.getMembers с filter=managers.
    """
    manager_ids: list[int] = []
    try:
        resp = await ctx.api.request(
            "groups.getMembers", {"group_id": ctx.group_id, "filter": "managers"}
        )
        for m in resp.get("response", {}).get("items", []):
            mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, int) else None)
            if mid:
                manager_ids.append(int(mid))
    except Exception as e:
        logger.warning(f"escalate: can't list managers for group {ctx.group_id}: {e}")
    if ctx.admin_vk_id and ctx.admin_vk_id not in manager_ids:
        manager_ids.append(ctx.admin_vk_id)
    return manager_ids


async def notify_group_admins(group_id: int, text: str) -> bool:
    """ЛС менеджерам группы от её имени, когда GroupContext на руках нет
    (фоновые задачи). True — хоть кому-то доставлено."""
    from web.vk_callback import _build_context

    ctx = await _build_context(group_id)
    if not ctx:
        return False
    return await notify_managers(ctx, text) > 0


async def notify_managers(ctx: GroupContext, note: str) -> int:
    """ЛС всем менеджерам группы + опционально Telegram. Возвращает, скольким
    каналам удалось доставить (0 — никто не узнал)."""
    notified = 0
    for mid in await _get_manager_ids(ctx):
        try:
            await ctx.api.messages.send(user_id=mid, message=note, random_id=0)
            notified += 1
        except Exception as e:
            # У менеджера может не быть диалога с группой — best-effort.
            logger.warning(f"notify: can't DM manager {mid} of group {ctx.group_id}: {e}")

    try:
        from core.telegram import notify_admin_telegram
        if await notify_admin_telegram(ctx.group_id, note):
            notified += 1
    except Exception as e:
        logger.warning(f"notify: telegram failed for group {ctx.group_id}: {e}")
    return notified


async def escalate_to_admin(
    ctx: GroupContext,
    user_id: int,
    reason: str,
    urgency: str = "normal",
) -> str:
    """Позвать человека в диалог: записать эскалацию, оповестить менеджеров
    (ЛС от имени группы + опционально Telegram), включить режим тишины бота.

    Возвращает текст ответа пользователю.
    """
    reason = (reason or "не указана").strip()[:300]

    user_name = f"id{user_id}"
    try:
        users = await ctx.api.users.get(user_ids=[user_id])
        if users:
            user_name = f"{users[0].first_name} {users[0].last_name}".strip()
    except Exception:
        pass

    await create_escalation(ctx.group_id, user_id, user_name, reason, urgency)

    mark = "🔴" if urgency == "high" else "🟡"
    note = (
        f"{mark} Нужен живой админ\n"
        f"Кто: {user_name} (vk.com/id{user_id})\n"
        f"Причина: {reason}\n"
        f"Диалог: https://vk.com/gim{ctx.group_id}?sel={user_id}\n\n"
        f"Бот молчит в этом диалоге {HANDOFF_HOURS} ч. "
        f"Вернуть его раньше: напишите мне «продолжай с {user_id}»."
    )

    notified = await notify_managers(ctx, note)

    until = datetime.now(timezone.utc) + timedelta(hours=HANDOFF_HOURS)
    await set_human_mode(ctx.group_id, user_id, until)

    logger.info(
        f"[ESCALATION] group={ctx.group_id} user={user_id} urgency={urgency} "
        f"notified={notified} reason={reason[:80]!r}"
    )

    if notified:
        return (
            "Позвал живого админа — он увидит диалог и ответит вам здесь. "
            "Я пока не вмешиваюсь, чтобы не мешать."
        )
    return "Оставил заявку живому админу — он ответит здесь, как только увидит."
