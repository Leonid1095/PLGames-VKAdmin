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
    BASE_URL: str = "http://localhost:8000"

    # AI Provider
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL: str = "openai/gpt-4o-mini"

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./vkbot.db"

    # Security
    ENCRYPTION_KEY: str = ""  # Fernet key for encrypting tokens
    JWT_SECRET: str = "change-me-to-random-secret"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
