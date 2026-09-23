"""Чтение стены сообщества сервисным ключом приложения.

Ключ сообщества стену НЕ читает: у wall.get / wall.getById / wall.getComments
в схеме VK access_token_type = [user, service], и вызов ключом сообщества
возвращает error 27 «method is unavailable with group auth» (проверено вживую).
Поэтому всё чтение стены — статистика постов, анализ тона при настройке ИИ —
идёт через VK_APP_SERVICE_KEY. Ограничение сервисного ключа: только открытые
сообщества (закрытое вернёт error 15/30).
"""

import logging

import httpx

from core.config import settings
from core.http_retry import http_request_with_retry

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
VK_API_VERSION = "5.199"


class VKReadError(Exception):
    """VK отказал в чтении (или сервисный ключ не настроен)."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"VK error {code}: {message}")


def _client() -> httpx.AsyncClient:
    # Отдельная фабрика — точка подмены транспорта в тестах.
    return httpx.AsyncClient(timeout=15)


async def wall_get(owner_id: int, count: int = 20) -> list[dict]:
    """Последние посты стены (сырые dict'ы VK). Бросает VKReadError."""
    key = settings.VK_APP_SERVICE_KEY
    if not key:
        raise VKReadError(0, "VK_APP_SERVICE_KEY не задан — стену читать нечем")
    async with _client() as client:
        resp = await http_request_with_retry(
            client, "GET", f"{VK_API}/wall.get",
            params={
                "owner_id": owner_id,
                "count": count,
                "access_token": key,
                "v": VK_API_VERSION,
            },
        )
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise VKReadError(int(err.get("error_code", 0)), str(err.get("error_msg", "")))
    return data.get("response", {}).get("items", []) or []
