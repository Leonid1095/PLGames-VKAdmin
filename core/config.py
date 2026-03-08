from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    VK_TOKEN: str = "placeholder_vk_token"
    VK_GROUP_ID: str = "0"
    OWNER_VK_ID: str = "0"
    OPENROUTER_API_KEY: str = "placeholder_openrouter_key"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL: str = "openai/gpt-4o-mini"
    DATABASE_URL: str = "sqlite+aiosqlite:///./vkbot.db"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
