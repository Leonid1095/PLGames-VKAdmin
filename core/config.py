from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # VK Bot (legacy single-group mode)
    VK_TOKEN: str = ""
    VK_GROUP_ID: str = "0"
    OWNER_VK_ID: str = "0"

    # VK App (multi-tenant mode)
    VK_APP_ID: str = ""
    VK_APP_SECRET: str = ""
    VK_APP_SERVICE_KEY: str = ""
    VK_MINIAPP_ID: str = ""
    VK_MINIAPP_SECRET: str = ""
    BASE_URL: str = "http://localhost:8000"

    # AI Provider
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL: str = "openai/gpt-4o-mini"

    # Telegram (global fallback, per-group tokens override via settings)
    TELEGRAM_BOT_TOKEN: str = ""

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./vkbot.db"

    # Security
    ENCRYPTION_KEY: str = ""  # Fernet key for encrypting tokens
    JWT_SECRET: str = "change-me-to-random-secret"
    API_KEY: str = ""  # API key for public API endpoints

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()

# Validate critical settings at import time
if not settings.ENCRYPTION_KEY:
    import warnings
    warnings.warn(
        "ENCRYPTION_KEY is not set! Token encryption will fail. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"",
        stacklevel=1,
    )
