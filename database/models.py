from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, Float, UniqueConstraint, ForeignKey
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()


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
    connected_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserContext(Base):
    __tablename__ = "user_contexts"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(BigInteger, ForeignKey("groups.group_id"), nullable=False, index=True)
    vk_id = Column(BigInteger, nullable=False, index=True)
    context_data = Column(Text, default="", nullable=False)
    last_interaction = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

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
