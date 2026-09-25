"""Проверка ключей группы для карточки «Ключи и доступы» в дашборде.

У бота несколько разных ключей VK, и каждый умеет своё (почему ключ сообщества
не читает стену — см. core/vk_read.py). Когда что-то ломалось, владелец каждый
раз заново выяснял, какой ключ умер и где взять новый. Здесь каждый ключ
проверяется живым запросом без побочных эффектов, и к статусу приложено, где
ключ получить.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from core import vk_read
from core.config import settings
from core.crypto import decrypt_token
from core.widgets import widget_install_hint
from database.service import get_group, get_setting

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 8  # сек на ключ: страница дашборда не должна висеть из-за VK
APP_WIDGET_BIT = 64  # право app_widget в маске groups.getTokenPermissions


@dataclass
class KeyStatus:
    key: str      # group | service | widget | admin
    title: str
    purpose: str
    state: str    # ok | fail | missing | off
    detail: str
    where: str    # где взять или обновить


def _client() -> httpx.AsyncClient:
    # Отдельная фабрика — точка подмены транспорта в тестах.
    return httpx.AsyncClient(timeout=CHECK_TIMEOUT)


async def _token_permissions(token: str) -> tuple[list[str], int]:
    """Права ключа сообщества (имена, маска). Ничего не меняет. Бросает VKReadError."""
    async with _client() as client:
        resp = await client.post(
            f"{vk_read.VK_API}/groups.getTokenPermissions",
            data={"access_token": token, "v": vk_read.VK_API_VERSION},
        )
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise vk_read.VKReadError(int(err.get("error_code", 0)), str(err.get("error_msg", "")))
    result = data.get("response", {})
    names = [p.get("name", "") for p in result.get("permissions", [])]
    return names, int(result.get("mask", 0))


def _rejected(e: Exception) -> str:
    if isinstance(e, vk_read.VKReadError):
        return f"VK отклоняет: ошибка {e.code} ({e.message})"
    return f"VK не ответил: {e!r}"


async def _check_group_key(group) -> KeyStatus:
    status = KeyStatus(
        key="group", title="Ключ сообщества", purpose="сообщения, посты, комментарии",
        state="ok", detail="",
        where=("Панель → «Подключить группу» → эта группа (выдаст новый ключ). "
               "Если VK выдаёт урезанный ключ (ошибка 1051) — Управление сообществом → "
               "Работа с API → Ключи доступа; такой ключ вносится в базу вручную"),
    )
    try:
        token = decrypt_token(group.access_token)
    except Exception:
        status.state, status.detail = "fail", "не расшифровывается — сменился ENCRYPTION_KEY?"
        return status
    try:
        names, _ = await _token_permissions(token)
    except Exception as e:
        status.state, status.detail = "fail", _rejected(e)
        return status
    status.detail = "права: " + ", ".join(names)
    return status


async def _check_service_key(group_id: int) -> KeyStatus:
    status = KeyStatus(
        key="service", title="Сервисный ключ приложения", purpose="чтение стены и статистика",
        state="ok", detail="читает стену",
        where=("vk.com/apps?act=manage → приложение бота → Настройки → «Сервисный ключ "
               "доступа» → в .env VK_APP_SERVICE_KEY, затем перезапуск сервиса"),
    )
    if not settings.VK_APP_SERVICE_KEY:
        status.state, status.detail = "missing", "не задан — статистика стены не собирается"
        return status
    try:
        await asyncio.wait_for(vk_read.wall_get(-group_id, count=1), CHECK_TIMEOUT)
    except Exception as e:
        status.state, status.detail = "fail", _rejected(e)
    return status


async def _check_widget_token(group_id: int) -> KeyStatus:
    status = KeyStatus(
        key="widget", title="Токен виджета", purpose="рейтинг «Топ участников» в группе",
        state="ok", detail="право app_widget есть, обновляется раз в час",
        where=widget_install_hint(),
    )
    if (await get_setting(group_id, "widget_enabled", "false")).lower() != "true":
        status.state, status.detail = "off", "виджет выключен"
        return status
    token = await get_setting(group_id, "widget_token", "")
    if not token:
        status.state, status.detail = "missing", "нет токена — рейтинг в группе не обновляется"
        install_error = await get_setting(group_id, "widget_install_error", "")
        if install_error:
            status.detail += f"; последняя попытка установки не прошла — {install_error}"
        return status
    try:
        names, mask = await _token_permissions(token)
    except Exception as e:
        status.state, status.detail = "fail", _rejected(e)
        return status
    if "app_widget" not in names and not mask & APP_WIDGET_BIT:
        status.state, status.detail = "fail", "у токена нет права app_widget"
        return status
    last_error = await get_setting(group_id, "widget_last_error", "")
    if last_error:
        status.state = "fail"
        status.detail = f"право есть, но последнее обновление не прошло: {last_error}"
    return status


def _admin_key_status() -> KeyStatus:
    return KeyStatus(
        key="admin", title="Личный ключ админа",
        purpose="удаление комментариев, баны, закрепы, фото к постам",
        state="off",
        detail="не подключён — ключ сообщества VK эти действия не пускает, "
               "бот присылает админу ссылку",
        where="нужно решение владельца: подключить личный ключ администратора",
    )


async def check_group_keys(group_id: int) -> list[KeyStatus]:
    """Статусы всех ключей группы. Проверки идут параллельно."""
    group = await get_group(group_id)
    if not group:
        return []
    checked = await asyncio.gather(
        _check_group_key(group),
        _check_service_key(group_id),
        _check_widget_token(group_id),
    )
    return [*checked, _admin_key_status()]
