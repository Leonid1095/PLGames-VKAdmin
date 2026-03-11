import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select
from database.engine import async_session
from database.models import Group, UserContext, GroupSettings, UserStats

logger = logging.getLogger(__name__)

# ─── Default settings seeded per group ───────────────────────────────────────

DEFAULT_SETTINGS = {
    "active_model": ("openai/gpt-4o-mini", "Активная AI-модель через OpenRouter"),
    "system_prompt": (
        "Ты вежливый и отзывчивый помощник-администратор группы ВКонтакте. "
        "Отвечай по делу и дружелюбно. Помни контекст диалога.",
        "Системный промпт для ИИ при ответах на сообщения"
    ),
    "moderation_aggressiveness": ("medium", "Агрессивность модерации: low / medium / high"),
    "autopost_enabled": ("false", "Включить автопостинг: true / false"),
    "autopost_interval_hours": ("6", "Интервал автопостинга в часах"),
    "autopost_topics": ("новости технологий, интересные факты, советы дня", "Темы для генерации постов"),
    "reply_to_comments": ("true", "Отвечать ли ИИ на комментарии: true / false"),
}

# ─── Group CRUD ──────────────────────────────────────────────────────────────

async def create_group(
    group_id: int,
    group_name: str,
    access_token: str,
    admin_vk_id: int,
    confirmation_code: str = "",
    secret_key: str = "",
) -> Group:
    """Register a new group (or reactivate existing)."""
    async with async_session() as session:
        result = await session.execute(select(Group).where(Group.group_id == group_id))
        group = result.scalar_one_or_none()
        if group:
            group.access_token = access_token
            group.admin_vk_id = admin_vk_id
            group.group_name = group_name
            group.confirmation_code = confirmation_code
            group.secret_key = secret_key
            group.is_active = True
        else:
            group = Group(
                group_id=group_id,
                group_name=group_name,
                access_token=access_token,
                admin_vk_id=admin_vk_id,
                confirmation_code=confirmation_code,
                secret_key=secret_key,
            )
            session.add(group)
        await session.commit()
        await session.refresh(group)
        return group


async def get_group(group_id: int) -> Group | None:
    """Get a group record by VK group ID."""
    async with async_session() as session:
        result = await session.execute(
            select(Group).where(Group.group_id == group_id, Group.is_active == True)
        )
        return result.scalar_one_or_none()


async def get_all_active_groups() -> list[Group]:
    """Return all active groups."""
    async with async_session() as session:
        result = await session.execute(select(Group).where(Group.is_active == True))
        return list(result.scalars().all())


async def deactivate_group(group_id: int) -> None:
    """Soft-delete a group."""
    async with async_session() as session:
        result = await session.execute(select(Group).where(Group.group_id == group_id))
        group = result.scalar_one_or_none()
        if group:
            group.is_active = False
            await session.commit()


# ─── Settings helpers ────────────────────────────────────────────────────────

async def get_setting(group_id: int, key: str, default: str = "") -> str:
    """Get a setting value for a specific group."""
    async with async_session() as session:
        result = await session.execute(
            select(GroupSettings).where(
                GroupSettings.group_id == group_id,
                GroupSettings.key == key,
            )
        )
        row = result.scalar_one_or_none()
        return row.value if row else default


async def set_setting(group_id: int, key: str, value: str) -> None:
    """Create or update a setting for a specific group."""
    async with async_session() as session:
        result = await session.execute(
            select(GroupSettings).where(
                GroupSettings.group_id == group_id,
                GroupSettings.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            session.add(GroupSettings(group_id=group_id, key=key, value=value))
        await session.commit()


async def seed_default_settings(group_id: int) -> None:
    """Insert default settings for a group if they don't exist yet."""
    async with async_session() as session:
        for key, (value, description) in DEFAULT_SETTINGS.items():
            result = await session.execute(
                select(GroupSettings).where(
                    GroupSettings.group_id == group_id,
                    GroupSettings.key == key,
                )
            )
            if not result.scalar_one_or_none():
                session.add(GroupSettings(
                    group_id=group_id, key=key, value=value, description=description
                ))
        await session.commit()
    logger.info(f"Default settings seeded for group {group_id}.")


# ─── Memory helpers ──────────────────────────────────────────────────────────

MAX_MEMORY_MESSAGES = 10

async def get_user_history(group_id: int, vk_id: int) -> list[dict]:
    """Return conversation history for a user in a specific group."""
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(
                UserContext.group_id == group_id,
                UserContext.vk_id == vk_id,
            )
        )
        row = result.scalar_one_or_none()
        if row and row.context_data:
            try:
                return json.loads(row.context_data)
            except json.JSONDecodeError:
                return []
        return []


async def save_user_history(group_id: int, vk_id: int, history: list[dict]) -> None:
    """Persist conversation history for a user in a specific group."""
    history = history[-(MAX_MEMORY_MESSAGES * 2):]
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(
                UserContext.group_id == group_id,
                UserContext.vk_id == vk_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.context_data = json.dumps(history, ensure_ascii=False)
            row.last_interaction = datetime.now(timezone.utc)
        else:
            session.add(UserContext(
                group_id=group_id,
                vk_id=vk_id,
                context_data=json.dumps(history, ensure_ascii=False),
                last_interaction=datetime.now(timezone.utc),
            ))
        await session.commit()


async def clear_user_history(group_id: int, vk_id: int) -> None:
    """Clear conversation memory for a user in a specific group."""
    await save_user_history(group_id, vk_id, [])


# ─── Gamification & Stats helpers ────────────────────────────────────────────

@dataclass
class UserStatsDTO:
    """Detached data transfer object for user stats."""
    group_id: int
    vk_id: int
    xp: int
    level: int
    messages_count: int
    reputation: int
    warnings: int
    is_vip: bool
    vip_expires: datetime | None
    balance: float
    daily_requests: int
    last_request_date: datetime | None


async def get_user_stats(group_id: int, vk_id: int) -> UserStatsDTO:
    """Get (or create) user stats for a specific group."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)
            await session.commit()
            await session.refresh(stats)
        return UserStatsDTO(
            group_id=stats.group_id,
            vk_id=stats.vk_id,
            xp=stats.xp,
            level=stats.level,
            messages_count=stats.messages_count,
            reputation=stats.reputation,
            warnings=stats.warnings,
            is_vip=stats.is_vip,
            vip_expires=stats.vip_expires,
            balance=stats.balance,
            daily_requests=stats.daily_requests,
            last_request_date=stats.last_request_date,
        )


async def check_and_increment_limit(group_id: int, vk_id: int) -> bool:
    """Check daily AI request limit. Returns True if allowed."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)

        now = datetime.now(timezone.utc)
        if not stats.last_request_date or stats.last_request_date.date() < now.date():
            stats.daily_requests = 0

        max_daily = 1000000 if stats.is_vip else 10
        if stats.daily_requests >= max_daily:
            return False

        stats.daily_requests += 1
        stats.last_request_date = now
        await session.commit()
        return True


async def grant_vip(group_id: int, vk_id: int, days: int) -> None:
    """Grant VIP status for N days."""
    from datetime import timedelta
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)

        stats.is_vip = True
        now = datetime.now(timezone.utc)
        if stats.vip_expires and stats.vip_expires > now:
            stats.vip_expires += timedelta(days=days)
        else:
            stats.vip_expires = now + timedelta(days=days)

        await session.commit()


async def modify_balance(group_id: int, vk_id: int, amount: float) -> float:
    """Add or subtract balance."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)

        stats.balance += amount
        await session.commit()
        return stats.balance


async def add_xp(group_id: int, vk_id: int, xp_amount: int) -> tuple[int, bool]:
    """Add XP to user. Returns (new_level, leveled_up)."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)

        stats.messages_count += 1
        stats.xp += xp_amount

        old_level = stats.level
        new_level = int((stats.xp / 10) ** 0.5) + 1
        leveled_up = new_level > old_level

        if leveled_up:
            stats.level = new_level

        await session.commit()
        return stats.level, leveled_up


async def modify_reputation(group_id: int, vk_id: int, amount: int) -> int:
    """Add or subtract reputation. Returns new value."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)

        stats.reputation += amount
        await session.commit()
        return stats.reputation


async def add_warning(group_id: int, vk_id: int) -> int:
    """Add a warning (strike). Returns new total."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)

        stats.warnings += 1
        await session.commit()
        return stats.warnings


async def clear_warnings(group_id: int, vk_id: int) -> None:
    """Reset warning counter for a user in a group."""
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if stats:
            stats.warnings = 0
            await session.commit()
