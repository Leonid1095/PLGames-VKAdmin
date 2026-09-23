"""Общая обвязка тестов: изолированная временная БД и тестовые секреты.

Переменные окружения ставятся ДО импорта модулей приложения: engine БД и
settings создаются на импорте, а переменные окружения перекрывают .env —
живая vkbot.db и боевые ключи тестами не затрагиваются.
"""

import os
import tempfile

from cryptography.fernet import Fernet

_tmpdir = tempfile.mkdtemp(prefix="vkadmin-tests-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmpdir}/test.db"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["JWT_SECRET"] = "test-jwt-secret-not-default"
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["VK_APP_SERVICE_KEY"] = "svc-test-key"
os.environ["OPENROUTER_API_KEY"] = "test"

import pytest  # noqa: E402

from database.engine import engine, init_db  # noqa: E402
from database.models import Base  # noqa: E402


@pytest.fixture
async def db():
    """Чистая схема на каждый тест."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield
