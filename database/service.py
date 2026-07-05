import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select, update
from database.engine import async_session, _is_sqlite
from database.models import (
    Group, UserContext, GroupSettings, UserStats,
    SuggestedPost, ContentSource, ScheduledPost, PostAnalytics,
    Newsletter, BanRecord, ContentTask, Escalation,
)

# Dialect-aware INSERT ... ON CONFLICT. Both the sqlite and postgresql dialects
# expose on_conflict_do_update / on_conflict_do_nothing with the same signature,
# which lets get-or-create paths upsert atomically instead of racing on a
# SELECT-then-INSERT (B3: two coroutines both miss the row, both insert, the
# second hits the UNIQUE constraint and crashes).
if _is_sqlite:
    from sqlalchemy.dialects.sqlite import insert as _upsert
else:
    from sqlalchemy.dialects.postgresql import insert as _upsert

logger = logging.getLogger(__name__)

# ─── Default settings seeded per group ───────────────────────────────────────
#
# 3 user-facing settings (simple for group owners):
#   - group_description: what the group is about (or auto-detected)
#   - moderation_level: 1-5 scale (1=minimal, 5=strict)
#   - autopost_enabled: true/false
#
# Everything else is internal (set by AI agent or kept as defaults).

DEFAULT_SETTINGS = {
    # === User-facing (simple) ===
    "group_description": ("", "О чём эта группа (заполняется при онбординге)"),
    "moderation_level": ("3", "Уровень модерации от 1 (мягкий) до 5 (строгий)"),
    "autopost_enabled": ("false", "Автопостинг: true / false"),

    # === Internal (managed by AI agent) ===
    "active_model": ("plgames-ai", "internal"),
    "system_prompt": (
        "Ты вежливый и отзывчивый помощник-администратор группы ВКонтакте. "
        "Отвечай по делу и дружелюбно. Помни контекст диалога.",
        "internal"
    ),
    "autopost_interval_hours": ("6", "internal"),
    "reply_to_comments": ("true", "internal"),
    "welcome_ai": ("true", "internal"),
    "content_parse_interval_hours": ("4", "internal"),
    "banned_words": ("", "internal"),
    "image_search_enabled": ("true", "internal"),
    "gamification_enabled": ("true", "internal"),
    "xp_per_like": ("2", "internal"),
    "xp_per_repost": ("5", "internal"),
    "xp_cooldown_sec": ("60", "internal"),
    "onboarding_complete": ("false", "internal"),
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
                group_id=group_id, group_name=group_name,
                access_token=access_token, admin_vk_id=admin_vk_id,
                confirmation_code=confirmation_code, secret_key=secret_key,
            )
            session.add(group)
        await session.commit()
        await session.refresh(group)
        return group


async def get_group(group_id: int) -> Group | None:
    async with async_session() as session:
        result = await session.execute(
            select(Group).where(Group.group_id == group_id, Group.is_active == True)
        )
        return result.scalar_one_or_none()


async def get_all_active_groups() -> list[Group]:
    async with async_session() as session:
        result = await session.execute(select(Group).where(Group.is_active == True))
        return list(result.scalars().all())


async def get_groups_by_admin(admin_vk_id: int) -> list[Group]:
    async with async_session() as session:
        result = await session.execute(
            select(Group).where(Group.admin_vk_id == admin_vk_id, Group.is_active == True)
        )
        return list(result.scalars().all())


async def deactivate_group(group_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(select(Group).where(Group.group_id == group_id))
        group = result.scalar_one_or_none()
        if group:
            group.is_active = False
            await session.commit()


# ─── Settings helpers ────────────────────────────────────────────────────────

async def get_setting(group_id: int, key: str, default: str = "") -> str:
    async with async_session() as session:
        result = await session.execute(
            select(GroupSettings).where(
                GroupSettings.group_id == group_id, GroupSettings.key == key,
            )
        )
        row = result.scalar_one_or_none()
        return row.value if row else default


async def set_setting(group_id: int, key: str, value: str) -> None:
    async with async_session() as session:
        stmt = (
            _upsert(GroupSettings)
            .values(group_id=group_id, key=key, value=value)
            .on_conflict_do_update(
                index_elements=["group_id", "key"],
                set_={"value": value},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def seed_default_settings(group_id: int) -> None:
    async with async_session() as session:
        for key, (value, description) in DEFAULT_SETTINGS.items():
            result = await session.execute(
                select(GroupSettings).where(
                    GroupSettings.group_id == group_id, GroupSettings.key == key,
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
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(
                UserContext.group_id == group_id, UserContext.vk_id == vk_id,
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
    history = history[-(MAX_MEMORY_MESSAGES * 2):]
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(
                UserContext.group_id == group_id, UserContext.vk_id == vk_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.context_data = json.dumps(history, ensure_ascii=False)
            row.last_interaction = datetime.now(timezone.utc)
        else:
            session.add(UserContext(
                group_id=group_id, vk_id=vk_id,
                context_data=json.dumps(history, ensure_ascii=False),
                last_interaction=datetime.now(timezone.utc),
            ))
        await session.commit()


# ─── Human handoff («живой админ в диалоге») ─────────────────────────────────

async def set_human_mode(group_id: int, vk_id: int, until: datetime | None) -> None:
    """Пока until в будущем — бот молчит в этом диалоге. None = вернуть бота."""
    async with async_session() as session:
        result = await session.execute(
            select(UserContext).where(
                UserContext.group_id == group_id, UserContext.vk_id == vk_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.human_mode_until = until
        else:
            session.add(UserContext(
                group_id=group_id, vk_id=vk_id, context_data="",
                human_mode_until=until,
            ))
        await session.commit()


async def is_human_mode_active(group_id: int, vk_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(UserContext.human_mode_until).where(
                UserContext.group_id == group_id, UserContext.vk_id == vk_id,
            )
        )
        until = result.scalar_one_or_none()
    if not until:
        return False
    # SQLite отдаёт naive datetime, храним UTC — сравниваем в naive UTC.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if until.tzinfo is not None:
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    return until > now


async def create_escalation(
    group_id: int, vk_id: int, user_name: str, reason: str, urgency: str = "normal",
) -> None:
    async with async_session() as session:
        session.add(Escalation(
            group_id=group_id, vk_id=vk_id, user_name=user_name[:100],
            reason=reason[:300], urgency=urgency,
        ))
        await session.commit()


async def resolve_escalations(group_id: int, vk_id: int) -> None:
    async with async_session() as session:
        await session.execute(
            update(Escalation)
            .where(
                Escalation.group_id == group_id,
                Escalation.vk_id == vk_id,
                Escalation.resolved == False,  # noqa: E712
            )
            .values(resolved=True)
        )
        await session.commit()


async def get_escalations_since(group_id: int, since: datetime) -> list[Escalation]:
    if since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        result = await session.execute(
            select(Escalation).where(
                Escalation.group_id == group_id,
                Escalation.created_at >= since,
            ).order_by(Escalation.created_at)
        )
        return list(result.scalars().all())


async def count_active_dialogs(group_id: int, since: datetime) -> int:
    from sqlalchemy import func
    if since.tzinfo is not None:
        since = since.astimezone(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(UserContext).where(
                UserContext.group_id == group_id,
                UserContext.last_interaction >= since,
            )
        )
        return int(result.scalar_one() or 0)


async def clear_user_history(group_id: int, vk_id: int) -> None:
    await save_user_history(group_id, vk_id, [])


# ─── Gamification & Stats helpers ────────────────────────────────────────────

@dataclass
class UserStatsDTO:
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


def _stats_to_dto(stats: UserStats) -> UserStatsDTO:
    return UserStatsDTO(
        group_id=stats.group_id, vk_id=stats.vk_id,
        xp=stats.xp, level=stats.level,
        messages_count=stats.messages_count, reputation=stats.reputation,
        warnings=stats.warnings, is_vip=stats.is_vip,
        vip_expires=stats.vip_expires, balance=stats.balance,
        daily_requests=stats.daily_requests, last_request_date=stats.last_request_date,
    )


async def _ensure_stats_row(session, group_id: int, vk_id: int) -> None:
    """Insert a blank stats row if absent, atomically (B3). Concurrent callers
    race here — ON CONFLICT DO NOTHING makes the loser a no-op instead of a
    UNIQUE-constraint crash. After this the row is guaranteed to exist."""
    await session.execute(
        _upsert(UserStats)
        .values(group_id=group_id, vk_id=vk_id)
        .on_conflict_do_nothing(index_elements=["group_id", "vk_id"])
    )


async def get_user_stats(group_id: int, vk_id: int) -> UserStatsDTO:
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        return _stats_to_dto(result.scalar_one())


async def check_and_increment_limit(group_id: int, vk_id: int, max_daily: int = 10) -> bool:
    from sqlalchemy import case

    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        await session.commit()
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        max_daily = 1000000 if stats.is_vip else max_daily

        # Effective counter: reset to 0 if last request was before today
        effective_count = case(
            (UserStats.last_request_date == None, 0),
            (UserStats.last_request_date < today_start, 0),
            else_=UserStats.daily_requests,
        )

        # Atomic UPDATE: increment only if under limit
        result = await session.execute(
            update(UserStats)
            .where(
                UserStats.group_id == group_id,
                UserStats.vk_id == vk_id,
                effective_count < max_daily,
            )
            .values(
                daily_requests=effective_count + 1,
                last_request_date=now,
            )
        )
        await session.commit()
        return result.rowcount > 0


async def grant_vip(group_id: int, vk_id: int, days: int) -> None:
    from datetime import timedelta
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        stats.is_vip = True
        now = datetime.now(timezone.utc)
        if stats.vip_expires and stats.vip_expires > now:
            stats.vip_expires += timedelta(days=days)
        else:
            stats.vip_expires = now + timedelta(days=days)
        await session.commit()


async def modify_balance(group_id: int, vk_id: int, amount: float) -> float:
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        stats.balance += amount
        await session.commit()
        return stats.balance


async def add_xp(group_id: int, vk_id: int, xp_amount: int) -> tuple[int, bool]:
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        stats.messages_count += 1
        stats.xp += xp_amount

        old_level = stats.level
        new_level = int((stats.xp / 10) ** 0.5) + 1
        leveled_up = new_level > old_level

        if leveled_up:
            stats.level = new_level

        await session.commit()
        return stats.level, leveled_up


async def add_xp_activity(group_id: int, vk_id: int, xp_amount: int) -> tuple[int, bool]:
    """Add XP for activity (likes, reposts) without incrementing messages_count."""
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        stats.xp += xp_amount

        old_level = stats.level
        new_level = int((stats.xp / 10) ** 0.5) + 1
        leveled_up = new_level > old_level

        if leveled_up:
            stats.level = new_level

        await session.commit()
        return stats.level, leveled_up


async def modify_reputation(group_id: int, vk_id: int, amount: int) -> int:
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        stats.reputation += amount
        await session.commit()
        return stats.reputation


async def add_warning(group_id: int, vk_id: int) -> int:
    async with async_session() as session:
        await _ensure_stats_row(session, group_id, vk_id)
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one()

        stats.warnings += 1
        await session.commit()
        return stats.warnings


async def clear_warnings(group_id: int, vk_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if stats:
            stats.warnings = 0
            await session.commit()


async def get_top_users(group_id: int, order_by: str = "xp", limit: int = 10) -> list[UserStatsDTO]:
    col_map = {
        "xp": UserStats.xp, "rep": UserStats.reputation,
        "reputation": UserStats.reputation,
        "messages": UserStats.messages_count, "level": UserStats.level,
    }
    col = col_map.get(order_by, UserStats.xp)
    async with async_session() as session:
        result = await session.execute(
            select(UserStats)
            .where(UserStats.group_id == group_id)
            .order_by(col.desc())
            .limit(limit)
        )
        return [_stats_to_dto(s) for s in result.scalars().all()]


# ─── Suggested Posts (Предложка) ─────────────────────────────────────────────

async def create_suggested_post(group_id: int, from_vk_id: int, text: str, attachments: str = "") -> SuggestedPost:
    async with async_session() as session:
        post = SuggestedPost(
            group_id=group_id, from_vk_id=from_vk_id,
            text=text, attachments=attachments,
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        return post


async def get_pending_suggestions(group_id: int, limit: int = 10) -> list[SuggestedPost]:
    async with async_session() as session:
        result = await session.execute(
            select(SuggestedPost)
            .where(SuggestedPost.group_id == group_id, SuggestedPost.status == "pending")
            .order_by(SuggestedPost.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_suggestion(suggestion_id: int) -> SuggestedPost | None:
    async with async_session() as session:
        result = await session.execute(
            select(SuggestedPost).where(SuggestedPost.id == suggestion_id)
        )
        return result.scalar_one_or_none()


async def review_suggestion(suggestion_id: int, group_id: int, status: str, reviewed_by: int, reject_reason: str = "") -> None:
    async with async_session() as session:
        result = await session.execute(
            select(SuggestedPost).where(
                SuggestedPost.id == suggestion_id,
                SuggestedPost.group_id == group_id,
            )
        )
        post = result.scalar_one_or_none()
        if post:
            post.status = status
            post.reviewed_by = reviewed_by
            post.reviewed_at = datetime.now(timezone.utc)
            if reject_reason:
                post.reject_reason = reject_reason
            await session.commit()


# ─── Content Sources (Парсинг) ───────────────────────────────────────────────

async def add_content_source(group_id: int, source_type: str, source_url: str, filter_keywords: str = "") -> ContentSource:
    async with async_session() as session:
        src = ContentSource(
            group_id=group_id, source_type=source_type,
            source_url=source_url, filter_keywords=filter_keywords,
        )
        session.add(src)
        await session.commit()
        await session.refresh(src)
        return src


async def get_content_sources(group_id: int) -> list[ContentSource]:
    async with async_session() as session:
        result = await session.execute(
            select(ContentSource).where(
                ContentSource.group_id == group_id, ContentSource.is_active == True,
            )
        )
        return list(result.scalars().all())


async def delete_content_source(source_id: int, group_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(ContentSource).where(
                ContentSource.id == source_id,
                ContentSource.group_id == group_id,
            )
        )
        src = result.scalar_one_or_none()
        if src:
            src.is_active = False
            await session.commit()
            return True
        return False


async def update_source_fetched(source_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(ContentSource).where(ContentSource.id == source_id)
        )
        src = result.scalar_one_or_none()
        if src:
            src.last_fetched_at = datetime.now(timezone.utc)
            await session.commit()


# ─── Scheduled Posts (Контент-план) ──────────────────────────────────────────

async def create_scheduled_post(
    group_id: int, text: str, scheduled_at: datetime,
    source: str = "manual", attachments: str = "",
) -> ScheduledPost:
    async with async_session() as session:
        post = ScheduledPost(
            group_id=group_id, text=text, scheduled_at=scheduled_at,
            source=source, attachments=attachments,
        )
        session.add(post)
        await session.commit()
        await session.refresh(post)
        return post


async def get_due_posts() -> list[ScheduledPost]:
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost)
            .where(ScheduledPost.scheduled_at <= now, ScheduledPost.status == "pending")
            .order_by(ScheduledPost.scheduled_at.asc())
        )
        return list(result.scalars().all())


async def reset_stale_publishing(older_than_minutes: int = 15) -> int:
    """Recover posts stuck in 'publishing' — claimed but never finished (e.g. a
    crash between the VK call and mark_post_*). Returns them to 'pending' so a
    later tick retries them. Returns how many were recovered."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    async with async_session() as session:
        res = await session.execute(
            update(ScheduledPost)
            .where(
                ScheduledPost.status == "publishing",
                ScheduledPost.scheduled_at <= cutoff,
            )
            .values(status="pending")
        )
        await session.commit()
        return res.rowcount or 0


async def claim_due_posts(limit: int = 25) -> list[ScheduledPost]:
    """Atomically claim due posts (pending → publishing) before publishing them.
    A post is only ever handed to ONE caller, so an overlapping tick or a
    re-delivery can't publish the same post twice (B6 idempotency)."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost.id)
            .where(ScheduledPost.scheduled_at <= now, ScheduledPost.status == "pending")
            .order_by(ScheduledPost.scheduled_at.asc())
            .limit(limit)
        )
        ids = [r[0] for r in result.all()]
        claimed: list[int] = []
        for pid in ids:
            res = await session.execute(
                update(ScheduledPost)
                .where(ScheduledPost.id == pid, ScheduledPost.status == "pending")
                .values(status="publishing")
            )
            if res.rowcount == 1:
                claimed.append(pid)
        await session.commit()
        if not claimed:
            return []
        result = await session.execute(
            select(ScheduledPost)
            .where(ScheduledPost.id.in_(claimed))
            .order_by(ScheduledPost.scheduled_at.asc())
        )
        return list(result.scalars().all())


async def mark_post_published(post_id: int, vk_post_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost).where(ScheduledPost.id == post_id)
        )
        post = result.scalar_one_or_none()
        if post:
            post.status = "published"
            post.published_at = datetime.now(timezone.utc)
            post.vk_post_id = vk_post_id
            await session.commit()


async def mark_post_failed(post_id: int, max_attempts: int = 3) -> None:
    """Record a failed publish attempt. Below max_attempts the post returns to
    'pending' for a later retry; once exhausted it is marked 'failed' for good —
    so a transient VK error no longer kills a post, and a permanent one no
    longer leaves it stuck publishing forever."""
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost).where(ScheduledPost.id == post_id)
        )
        post = result.scalar_one_or_none()
        if post:
            post.attempts = (post.attempts or 0) + 1
            post.status = "failed" if post.attempts >= max_attempts else "pending"
            await session.commit()


async def get_content_plan(group_id: int, date: datetime) -> list[ScheduledPost]:
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59)
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.group_id == group_id,
                ScheduledPost.scheduled_at >= start,
                ScheduledPost.scheduled_at <= end,
            )
            .order_by(ScheduledPost.scheduled_at.asc())
        )
        return list(result.scalars().all())


# ─── Post Analytics ──────────────────────────────────────────────────────────

async def upsert_post_analytics(
    group_id: int, vk_post_id: int,
    likes: int = 0, reposts: int = 0, comments: int = 0, views: int = 0,
    published_at: datetime | None = None,
) -> None:
    async with async_session() as session:
        now = datetime.now(timezone.utc)
        stmt = (
            _upsert(PostAnalytics)
            .values(
                group_id=group_id, vk_post_id=vk_post_id,
                likes=likes, reposts=reposts, comments=comments, views=views,
                published_at=published_at, last_checked_at=now,
            )
            .on_conflict_do_update(
                index_elements=["group_id", "vk_post_id"],
                # published_at is intentionally NOT overwritten — keep the first
                # value we recorded for the post.
                set_={
                    "likes": likes, "reposts": reposts, "comments": comments,
                    "views": views, "last_checked_at": now,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()


async def get_post_analytics(group_id: int, limit: int = 20) -> list[PostAnalytics]:
    async with async_session() as session:
        result = await session.execute(
            select(PostAnalytics)
            .where(PostAnalytics.group_id == group_id)
            .order_by(PostAnalytics.published_at.desc().nullslast())
            .limit(limit)
        )
        return list(result.scalars().all())


# ─── Newsletter (Рассылка) ──────────────────────────────────────────────────

async def create_newsletter(group_id: int, text: str, created_by: int, total: int) -> Newsletter:
    async with async_session() as session:
        nl = Newsletter(
            group_id=group_id, text=text,
            created_by=created_by, total_recipients=total,
        )
        session.add(nl)
        await session.commit()
        await session.refresh(nl)
        return nl


async def update_newsletter_progress(newsletter_id: int, sent_count: int, status: str = "sending") -> None:
    async with async_session() as session:
        result = await session.execute(
            select(Newsletter).where(Newsletter.id == newsletter_id)
        )
        nl = result.scalar_one_or_none()
        if nl:
            nl.sent_count = sent_count
            nl.status = status
            await session.commit()


# ─── Ban Records ─────────────────────────────────────────────────────────────

async def create_ban_record(group_id: int, vk_id: int, banned_by: int, reason: str = "") -> BanRecord:
    async with async_session() as session:
        rec = BanRecord(
            group_id=group_id, vk_id=vk_id,
            banned_by=banned_by, reason=reason,
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec


async def remove_ban_record(group_id: int, vk_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(BanRecord).where(
                BanRecord.group_id == group_id, BanRecord.vk_id == vk_id,
                BanRecord.unbanned_at == None,
            )
        )
        rec = result.scalar_one_or_none()
        if rec:
            rec.unbanned_at = datetime.now(timezone.utc)
            await session.commit()


async def get_ban_history(group_id: int, limit: int = 50) -> list[BanRecord]:
    async with async_session() as session:
        result = await session.execute(
            select(BanRecord)
            .where(BanRecord.group_id == group_id)
            .order_by(BanRecord.banned_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


# ─── Content Tasks (Авто-задачи копирайтера) ────────────────────────────────

async def create_content_task(
    group_id: int, name: str, task_type: str, schedule_cron: str,
    source_url: str = "", instruction: str = "", length: str = "auto",
) -> ContentTask:
    async with async_session() as session:
        task = ContentTask(
            group_id=group_id, name=name, task_type=task_type,
            source_url=source_url, instruction=instruction,
            schedule_cron=schedule_cron, length=length,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task


async def get_content_tasks(group_id: int) -> list[ContentTask]:
    async with async_session() as session:
        result = await session.execute(
            select(ContentTask).where(
                ContentTask.group_id == group_id, ContentTask.is_active == True,
            )
        )
        return list(result.scalars().all())


async def get_all_active_content_tasks() -> list[ContentTask]:
    async with async_session() as session:
        result = await session.execute(
            select(ContentTask).where(ContentTask.is_active == True)
        )
        return list(result.scalars().all())


async def update_content_task_run(task_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(ContentTask).where(ContentTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            task.last_run_at = datetime.now(timezone.utc)
            await session.commit()


async def delete_content_task(task_id: int, group_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(ContentTask).where(
                ContentTask.id == task_id,
                ContentTask.group_id == group_id,
            )
        )
        task = result.scalar_one_or_none()
        if task:
            task.is_active = False
            await session.commit()
            return True
        return False


# ─── Daily membership state (for proactive digest & public welcome) ──────────
#
# Membership events are bursty but low-volume, so we accumulate them in settings
# and let the once-a-day proactive job drain them. Stored as plain settings:
#   _pending_welcome : JSON list of {"id", "name"} for newcomers not yet greeted
#   _joins_since_digest / _leaves_since_digest : counters for the admin digest

_MAX_PENDING_WELCOME = 50


async def record_member_join(group_id: int, vk_id: int, name: str) -> None:
    raw = await get_setting(group_id, "_pending_welcome", "[]")
    try:
        pending = json.loads(raw)
        if not isinstance(pending, list):
            pending = []
    except (json.JSONDecodeError, TypeError):
        pending = []
    if not any(p.get("id") == vk_id for p in pending):
        pending.append({"id": vk_id, "name": name})
        pending = pending[-_MAX_PENDING_WELCOME:]
        await set_setting(group_id, "_pending_welcome", json.dumps(pending, ensure_ascii=False))

    joins = await get_setting(group_id, "_joins_since_digest", "0")
    try:
        joins_n = int(joins) + 1
    except ValueError:
        joins_n = 1
    await set_setting(group_id, "_joins_since_digest", str(joins_n))


async def record_member_leave(group_id: int) -> None:
    leaves = await get_setting(group_id, "_leaves_since_digest", "0")
    try:
        leaves_n = int(leaves) + 1
    except ValueError:
        leaves_n = 1
    await set_setting(group_id, "_leaves_since_digest", str(leaves_n))


async def take_daily_membership_state(group_id: int) -> dict:
    """Read the accumulated join/leave state and reset it. Returns
    {"welcomes": [{"id","name"}], "joins": int, "leaves": int}."""
    raw = await get_setting(group_id, "_pending_welcome", "[]")
    try:
        welcomes = json.loads(raw)
        if not isinstance(welcomes, list):
            welcomes = []
    except (json.JSONDecodeError, TypeError):
        welcomes = []
    try:
        joins = int(await get_setting(group_id, "_joins_since_digest", "0"))
    except ValueError:
        joins = 0
    try:
        leaves = int(await get_setting(group_id, "_leaves_since_digest", "0"))
    except ValueError:
        leaves = 0

    await set_setting(group_id, "_pending_welcome", "[]")
    await set_setting(group_id, "_joins_since_digest", "0")
    await set_setting(group_id, "_leaves_since_digest", "0")
    return {"welcomes": welcomes, "joins": joins, "leaves": leaves}
