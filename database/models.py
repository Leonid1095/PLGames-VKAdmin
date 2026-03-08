from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

class UserContext(Base):
    __tablename__ = "user_contexts"

    id = Column(Integer, primary_key=True, index=True)
    vk_id = Column(Integer, unique=True, index=True, nullable=False)
    # Store JSON string or simple text describing the context of conversation
    context_data = Column(Text, default="", nullable=False)
    last_interaction = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=False)
    description = Column(String, nullable=True)

# Common default settings we might want:
# - active_model: "openai/gpt-4o-mini"
# - moderation_aggressiveness: "medium"
# - system_prompt: "You are a helpful VK admin..."

class UserStats(Base):
    __tablename__ = "user_stats"

    id = Column(Integer, primary_key=True, index=True)
    vk_id = Column(Integer, unique=True, index=True, nullable=False)
    xp = Column(Integer, default=0, nullable=False)
    level = Column(Integer, default=1, nullable=False)
    messages_count = Column(Integer, default=0, nullable=False)
    reputation = Column(Integer, default=0, nullable=False)
    warnings = Column(Integer, default=0, nullable=False)

    # Monetization & Personal Account
    is_vip = Column(Boolean, default=False, nullable=False)
    vip_expires = Column(DateTime, nullable=True)
    balance = Column(Float, default=0.0, nullable=False)  # Coins/Rubles
    daily_requests = Column(Integer, default=0, nullable=False)  # Requests used today
    last_request_date = Column(DateTime, nullable=True)  # To know when to reset the daily counter

