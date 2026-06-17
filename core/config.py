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

    # GitHub (for private repos)
    GITHUB_TOKEN: str = ""

    # Telegram (global fallback, per-group tokens override via settings)
    TELEGRAM_BOT_TOKEN: str = ""

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./vkbot.db"

    # Security
    ENCRYPTION_KEY: str = ""  # Fernet key for encrypting tokens
    # JWT_SECRET = cryptographic signing root for Mini App session tokens.
    # Keep it high-entropy and secret. The dashboard password and the session
    # cookie seed are now SEPARATE settings (below) so one leak ≠ all leaked.
    JWT_SECRET: str = "change-me-to-random-secret"
    DASHBOARD_PASSWORD: str = ""  # dashboard login password (falls back to JWT_SECRET if unset)
    SESSION_SECRET: str = ""  # seed for the dashboard session cookie (falls back to JWT_SECRET)
    API_KEY: str = ""  # API key for public API endpoints

    # Image search (free, 200 req/hr)
    PEXELS_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()

DEFAULT_JWT_SECRET = "change-me-to-random-secret"


def validate_critical_settings() -> None:
    """Validate that critical settings are present. Called during startup."""
    if not settings.ENCRYPTION_KEY:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set! Token encryption will fail.\n"
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # Fatal: a known/empty JWT_SECRET lets anyone forge Mini App admin sessions
    # (see core/vk_auth.py). Refuse to start rather than run wide open.
    if not settings.JWT_SECRET or settings.JWT_SECRET == DEFAULT_JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is not set or still the default placeholder.\n"
            "It signs Mini App session tokens — a known value lets anyone forge an admin session.\n"
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    if not settings.DASHBOARD_PASSWORD:
        import warnings
        warnings.warn(
            "DASHBOARD_PASSWORD is not set — the dashboard login falls back to JWT_SECRET. "
            "Set a separate DASHBOARD_PASSWORD so the typed password isn't your signing secret.",
            stacklevel=2,
        )
