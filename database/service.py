import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select
from database.engine import async_session
from database.models import UserContext, Settings, UserStats

logger = logging.getLogger(__name__)

# ─── Default settings seeded into DB on first run ───────────────────────────

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

# ─── Settings helpers ────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    """Get a setting value from DB, or return default."""
    async with async_session() as session:
        result = await session.execute(select(Settings).where(Settings.key == key))
        row = result.scalar_one_or_none()
        return row.value if row else default

async def set_setting(key: str, value: str) -> None:
    """Create or update a setting in DB."""
    async with async_session() as session:
        result = await session.execute(select(Settings).where(Settings.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            session.add(Settings(key=key, value=value))
        await session.commit()

async def seed_default_settings() -> None:
    """Insert default settings if they don't exist yet."""
    async with async_session() as session:
        for key, (value, description) in DEFAULT_SETTINGS.items():
            result = await session.execute(select(Settings).where(Settings.key == key))
            if not result.scalar_one_or_none():
                session.add(Settings(key=key, value=value, description=description))
        await session.commit()
    logger.info("Default settings seeded.")

# ─── Memory helpers ──────────────────────────────────────────────────────────

MAX_MEMORY_MESSAGES = 10  # how many message-pairs to keep in memory

async def get_user_history(vk_id: int) -> list[dict]:
    """Return the conversation history for a given VK user as a list of dicts."""
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(UserContext.vk_id == vk_id)
        )
        row = result.scalar_one_or_none()
        if row and row.context_data:
            try:
                return json.loads(row.context_data)
            except json.JSONDecodeError:
                return []
        return []

async def save_user_history(vk_id: int, history: list[dict]) -> None:
    """Persist the conversation history for a VK user."""
    # Keep only the last N pairs so the context window doesn't blow up
    history = history[-(MAX_MEMORY_MESSAGES * 2):]
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(UserContext.vk_id == vk_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.context_data = json.dumps(history, ensure_ascii=False)
            row.last_interaction = datetime.now(timezone.utc)
        else:
            session.add(UserContext(
                vk_id=vk_id,
                context_data=json.dumps(history, ensure_ascii=False),
                last_interaction=datetime.now(timezone.utc),
            ))
        await session.commit()

async def clear_user_history(vk_id: int) -> None:
    """Clear conversation memory for a given user."""
    await save_user_history(vk_id, [])

# ─── Gamification & Stats helpers ────────────────────────────────────────────

@dataclass
class UserStatsDTO:
    """A detached data transfer object for user stats (safe to use outside session)."""
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

async def get_user_stats(vk_id: int) -> UserStatsDTO:
    """Get (or create) user stats. Returns a detached DTO safe to use anywhere."""
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)
            await session.commit()
            await session.refresh(stats)
        return UserStatsDTO(
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

async def check_and_increment_limit(vk_id: int) -> bool:
    """
    Check if user has available AI requests for today. If yes, increment the counter.
    Automatically resets the daily counter if it's a new day.
    Returns: True if request is allowed, False if limit reached.
    """
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)
            
        now = datetime.now(timezone.utc)
        
        # Reset counter if it's a new day
        if not stats.last_request_date or stats.last_request_date.date() < now.date():
            stats.daily_requests = 0
            
        # Check limits
        max_daily = 1000000 if stats.is_vip else 10  # 10 reqs for free, unlimited for VIP
        if stats.daily_requests >= max_daily:
            return False
            
        stats.daily_requests += 1
        stats.last_request_date = now
        await session.commit()
        return True

async def grant_vip(vk_id: int, days: int) -> None:
    """Grant VIP status for N days."""
    from datetime import timedelta
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)
            
        stats.is_vip = True
        now = datetime.now(timezone.utc)
        if stats.vip_expires and stats.vip_expires > now:
            stats.vip_expires += timedelta(days=days)
        else:
            stats.vip_expires = now + timedelta(days=days)
            
        await session.commit()

async def modify_balance(vk_id: int, amount: float) -> float:
    """Add or subtract balance (coins/rubles)."""
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)
            
        stats.balance += amount
        await session.commit()
        return stats.balance

async def add_xp(vk_id: int, xp_amount: int) -> tuple[int, bool]:
    """Adds XP to user. Returns (new_level, leveled_up)."""
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)

        stats.messages_count += 1
        stats.xp += xp_amount

        old_level = stats.level
        # Level formula: level = sqrt(xp / 10) + 1
        new_level = int((stats.xp / 10) ** 0.5) + 1
        leveled_up = new_level > old_level

        if leveled_up:
            stats.level = new_level

        await session.commit()
        return stats.level, leveled_up

async def modify_reputation(vk_id: int, amount: int) -> int:
    """Adds or subtracts reputation. Returns the new reputation value."""
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)

        stats.reputation += amount
        await session.commit()
        return stats.reputation

async def add_warning(vk_id: int) -> int:
    """Adds a warning (strike) and returns the new total."""
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(vk_id=vk_id)
            session.add(stats)

        stats.warnings += 1
        await session.commit()
        return stats.warnings

async def clear_warnings(vk_id: int) -> None:
    """Reset warning counter for a user."""
    async with async_session() as session:
        result = await session.execute(select(UserStats).where(UserStats.vk_id == vk_id))
        stats = result.scalar_one_or_none()
        if stats:
            stats.warnings = 0
            await session.commit()
