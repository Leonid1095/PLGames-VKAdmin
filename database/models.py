from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, Float, UniqueConstraint, ForeignKey
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

_now = lambda: datetime.now(timezone.utc)


class Group(Base):
    """Connected VK group (tenant)."""
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, unique=True, nullable=False, index=True)
    group_name = Column(String, default="")
    access_token = Column(Text, nullable=False)  # encrypted
    admin_vk_id = Column(BigInteger, nullable=False)
    confirmation_code = Column(String, default="")
    secret_key = Column(String, default="")
    is_active = Column(Boolean, default=True, nullable=False)
    connected_at = Column(DateTime, default=_now)


class UserContext(Base):
    __tablename__ = "user_contexts"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    vk_id = Column(BigInteger, nullable=False, index=True)
    context_data = Column(Text, default="", nullable=False)
    last_interaction = Column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "vk_id", name="uq_context_group_user"),
    )


class GroupSettings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    key = Column(String, nullable=False, index=True)
    value = Column(String, nullable=False)
    description = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("group_id", "key", name="uq_settings_group_key"),
    )


class UserStats(Base):
    __tablename__ = "user_stats"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    vk_id = Column(BigInteger, nullable=False, index=True)
    xp = Column(Integer, default=0, nullable=False)
    level = Column(Integer, default=1, nullable=False)
    messages_count = Column(Integer, default=0, nullable=False)
    reputation = Column(Integer, default=0, nullable=False)
    warnings = Column(Integer, default=0, nullable=False)

    # Monetization & Personal Account
    is_vip = Column(Boolean, default=False, nullable=False)
    vip_expires = Column(DateTime, nullable=True)
    balance = Column(Float, default=0.0, nullable=False)
    daily_requests = Column(Integer, default=0, nullable=False)
    last_request_date = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("group_id", "vk_id", name="uq_stats_group_user"),
    )


# ─── Suggested Posts (Предложка) ─────────────────────────────────────────────

class SuggestedPost(Base):
    __tablename__ = "suggested_posts"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    from_vk_id = Column(BigInteger, nullable=False)
    text = Column(Text, nullable=False)
    attachments = Column(Text, default="")  # JSON list of attachment strings
    status = Column(String, default="pending", nullable=False, index=True)  # pending / approved / rejected / published
    created_at = Column(DateTime, default=_now, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(BigInteger, nullable=True)
    reject_reason = Column(String, nullable=True)


# ─── Content Sources (Парсинг) ───────────────────────────────────────────────

class ContentSource(Base):
    __tablename__ = "content_sources"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    source_type = Column(String, nullable=False)  # rss / vk_group
    source_url = Column(String, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_fetched_at = Column(DateTime, nullable=True)
    filter_keywords = Column(Text, default="")  # comma-separated


# ─── Scheduled Posts (Контент-план) ──────────────────────────────────────────

class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    attachments = Column(Text, default="")
    scheduled_at = Column(DateTime, nullable=False, index=True)
    status = Column(String, default="pending", nullable=False, index=True)  # pending / published / failed
    source = Column(String, default="manual")  # manual / ai / parsed / suggested
    published_at = Column(DateTime, nullable=True)
    vk_post_id = Column(BigInteger, nullable=True)


# ─── Post Analytics ──────────────────────────────────────────────────────────

class PostAnalytics(Base):
    __tablename__ = "post_analytics"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    vk_post_id = Column(BigInteger, nullable=False)
    published_at = Column(DateTime, nullable=True)
    likes = Column(Integer, default=0)
    reposts = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    views = Column(Integer, default=0)
    last_checked_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("group_id", "vk_post_id", name="uq_analytics_group_post"),
    )


# ─── Newsletter (Рассылка) ──────────────────────────────────────────────────

class Newsletter(Base):
    __tablename__ = "newsletters"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_now, nullable=False)
    status = Column(String, default="sending", nullable=False)  # sending / sent / failed
    total_recipients = Column(Integer, default=0)
    sent_count = Column(Integer, default=0)
    created_by = Column(BigInteger, nullable=False)


# ─── Ban Records (Аудит банов) ───────────────────────────────────────────────

# ─── Content Tasks (Авто-задачи копирайтера) ────────────────────────────────

class ContentTask(Base):
    """Recurring content task — e.g. 'every Friday, write patch notes from GitHub'."""
    __tablename__ = "content_tasks"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    name = Column(String, nullable=False)  # human-readable name
    task_type = Column(String, nullable=False)  # patch_notes, article, digest
    source_url = Column(String, default="")  # URL to fetch data from
    instruction = Column(Text, default="")  # custom instruction for AI
    schedule_cron = Column(String, nullable=False)  # cron expression: "0 18 * * 5" = Friday 18:00
    length = Column(String, default="auto")  # short / medium / long / auto
    is_active = Column(Boolean, default=True, nullable=False)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_content_task_group_name"),
    )


class BanRecord(Base):
    __tablename__ = "ban_records"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    vk_id = Column(BigInteger, nullable=False)
    banned_by = Column(BigInteger, nullable=False)
    reason = Column(String, default="")
    banned_at = Column(DateTime, default=_now, nullable=False)
    unbanned_at = Column(DateTime, nullable=True)
