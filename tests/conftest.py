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

import httpx  # noqa: E402
import pytest  # noqa: E402

from database.engine import engine, init_db  # noqa: E402
from database.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def _no_vk_network(monkeypatch):
    """Ни один тест не ходит в настоящий VK: по умолчанию VK «отвечает» ошибкой.

    Ответ, а не обрыв соединения — иначе http_retry ждёт бэкофф на каждом вызове.
    Тесты, которым нужен VK, подменяют _client сами (их setattr — позже)."""
    from core import key_status, vk_read, widgets

    def offline(request):
        return httpx.Response(200, json={
            "error": {"error_code": 0, "error_msg": "VK недоступен в тестах"},
        })

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(offline))  # noqa: E731
    for module in (key_status, vk_read, widgets):
        monkeypatch.setattr(module, "_client", factory)


@pytest.fixture
async def db():
    """Чистая схема на каждый тест."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield
