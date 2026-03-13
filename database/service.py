import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select, update
from database.engine import async_session
from database.models import (
    Group, UserContext, GroupSettings, UserStats,
    SuggestedPost, ContentSource, ScheduledPost, PostAnalytics,
    Newsletter, BanRecord,
)

logger = logging.getLogger(__name__)

# ─── Default settings seeded per group ───────────────────────────────────────

DEFAULT_SETTINGS = {
    "active_model": ("plgames-ai", "Активная AI-модель"),
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
    "welcome_message": ("", "Приветственное сообщение для новых участников (пусто = выкл)"),
    "welcome_ai": ("false", "Генерировать приветствие через ИИ: true / false"),
    "content_parse_interval_hours": ("4", "Интервал парсинга контента в часах"),
    "autoplan_enabled": ("false", "Авто-генерация контент-плана: true / false"),
    "autoplan_times": ("09:00,13:00,18:00", "Времена публикаций для контент-плана (через запятую)"),
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
        result = await session.execute(
            select(GroupSettings).where(
                GroupSettings.group_id == group_id, GroupSettings.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            session.add(GroupSettings(group_id=group_id, key=key, value=value))
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


async def get_user_stats(group_id: int, vk_id: int) -> UserStatsDTO:
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
            )
        )
        stats = result.scalar_one_or_none()
        if not stats:
            stats = UserStats(group_id=group_id, vk_id=vk_id)
            session.add(stats)
            await session.commit()
            await session.refresh(stats)
        return _stats_to_dto(stats)


async def check_and_increment_limit(group_id: int, vk_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
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
    from datetime import timedelta
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
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
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
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
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
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
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
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
    async with async_session() as session:
        result = await session.execute(
            select(UserStats).where(
                UserStats.group_id == group_id, UserStats.vk_id == vk_id,
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


async def review_suggestion(suggestion_id: int, status: str, reviewed_by: int, reject_reason: str = "") -> None:
    async with async_session() as session:
        result = await session.execute(
            select(SuggestedPost).where(SuggestedPost.id == suggestion_id)
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


async def delete_content_source(source_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(ContentSource).where(ContentSource.id == source_id)
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


async def mark_post_failed(post_id: int) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(ScheduledPost).where(ScheduledPost.id == post_id)
        )
        post = result.scalar_one_or_none()
        if post:
            post.status = "failed"
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
        result = await session.execute(
            select(PostAnalytics).where(
                PostAnalytics.group_id == group_id, PostAnalytics.vk_post_id == vk_post_id,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.likes = likes
            row.reposts = reposts
            row.comments = comments
            row.views = views
            row.last_checked_at = datetime.now(timezone.utc)
        else:
            session.add(PostAnalytics(
                group_id=group_id, vk_post_id=vk_post_id,
                likes=likes, reposts=reposts, comments=comments, views=views,
                published_at=published_at,
            ))
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
