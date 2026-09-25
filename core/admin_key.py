"""Личный ключ админа группы — для того, чего ключ сообщества VK не умеет.

Ключ сообщества не пускает в photos.getWallUploadServer/saveWallPhoto,
wall.deleteComment, groups.ban/unban, wall.pin/unpin (err 27, проверено
23.09.2026). Владелец подключает свой ключ кнопкой в дашборде через VK ID
(web/oauth.py; oauth.vk.com для нашего приложения отвечает «Security Error»),
здесь — хранение, проверка, продление и выдача API.

Ключ VK ID живёт час; пара «ключ + refresh» продлевается, и после продления
старая пара недействительна. Поэтому копии в группах одного админа
продлеваются вместе и под одним замком. Храним в настройках каждой группы,
где VK подтвердил, что этот человек — админ.

Умершим ключ считается по ошибке VK 5 или когда VK ID отказался продлить;
временные сбои — нет.
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

import httpx
from vkbottle import API

from core import vk_read
from core.config import settings
from core.crypto import decrypt_token, encrypt_token
from database.service import get_all_active_groups, get_setting, set_setting

logger = logging.getLogger(__name__)

TOKEN_KEY = "admin_user_token"
REFRESH_KEY = "admin_refresh_token"
DEVICE_KEY = "admin_device_id"
EXPIRES_KEY = "admin_token_expires_at"
USER_ID_KEY = "admin_user_id"
NAME_KEY = "admin_user_name"
ERROR_KEY = "admin_key_error"
ALERTED_KEY = "admin_key_alerted"
_ALL_KEYS = (TOKEN_KEY, REFRESH_KEY, DEVICE_KEY, EXPIRES_KEY, USER_ID_KEY,
             NAME_KEY, ERROR_KEY, ALERTED_KEY)

VKID_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
ADMIN_KEY_HINT = "подключите личный ключ в панели: «Ключи и доступы» → «Подключить»"
_AUTH_FAILED = 5  # VK: User authorization failed — ключ отозван или истёк
_REFRESH_MARGIN = 300  # продлеваем за 5 минут до конца часа
_DEAD_REFRESH = {"invalid_grant", "invalid_token", "access_denied", "invalid_client"}
_refresh_lock = asyncio.Lock()


class AdminKeyError(Exception):
    """Подключить ключ нельзя; текст — для владельца."""


class _TransientError(Exception):
    """VK ID сейчас не ответил — ключ не трогаем, попробуем позже."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    device_id: str
    expires_in: int
    user_id: int
    scope: str = ""


def _client() -> httpx.AsyncClient:
    # Отдельная фабрика — точка подмены транспорта в тестах.
    return httpx.AsyncClient(timeout=15)


async def _vk(token: str, method: str, **params):
    """Вызов VK; любой сбой — сеть, не-JSON, чужой формат — это VKReadError,
    чтобы подключение ключа показало владельцу причину, а не 500."""
    try:
        async with _client() as client:
            resp = await client.post(
                f"{vk_read.VK_API}/{method}",
                data={**params, "access_token": token, "v": vk_read.VK_API_VERSION},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise vk_read.VKReadError(0, f"VK не ответил на {method}: {e!r}") from e
    if not isinstance(data, dict):
        raise vk_read.VKReadError(0, f"неожиданный ответ VK на {method}")
    if "error" in data:
        err = data["error"]
        raise vk_read.VKReadError(int(err.get("error_code", 0)), str(err.get("error_msg", "")))
    if "response" not in data:
        raise vk_read.VKReadError(0, f"неожиданный ответ VK на {method}")
    return data["response"]


async def _vkid(**form) -> dict:
    """POST на id.vk.ru/oauth2/auth. Сеть и не-JSON — _TransientError."""
    try:
        async with _client() as client:
            resp = await client.post(VKID_TOKEN_URL, data=form)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise _TransientError(repr(e)) from e
    if not isinstance(data, dict):
        raise _TransientError("неожиданный ответ VK ID")
    return data


def _token_set(data: dict, device_id: str) -> TokenSet:
    return TokenSet(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        device_id=device_id,
        expires_in=int(data.get("expires_in") or 3600),
        user_id=int(data.get("user_id") or 0),
        scope=str(data.get("scope", "")),
    )


async def exchange_code(code: str, code_verifier: str, device_id: str, state: str) -> TokenSet:
    """Код из возврата VK ID → пара ключей. Любой отказ — AdminKeyError."""
    try:
        data = await _vkid(
            grant_type="authorization_code", code=code, code_verifier=code_verifier,
            device_id=device_id, state=state, client_id=settings.VK_APP_ID,
            redirect_uri=f"{settings.BASE_URL}/api/vk/callback",
        )
    except _TransientError as e:
        raise AdminKeyError(f"VK ID не ответил при обмене кода ({e}). "
                            "Нажмите «Подключить» ещё раз.") from e
    if "error" in data or not data.get("access_token"):
        reason = data.get("error_description") or data.get("error") or "ключ не выдан"
        raise AdminKeyError(f"VK отказал: {reason}")
    return _token_set(data, device_id)


async def _notify_admin(group_id: int, text: str) -> bool:
    from core.escalation import notify_group_admins
    return await notify_group_admins(group_id, text)


async def _store(group_ids: list[int], tokens: TokenSet) -> None:
    access, refresh = encrypt_token(tokens.access_token), encrypt_token(tokens.refresh_token)
    expires_at = str(int(time.time()) + tokens.expires_in)
    for gid in group_ids:
        await set_setting(gid, TOKEN_KEY, access)
        await set_setting(gid, REFRESH_KEY, refresh)
        await set_setting(gid, DEVICE_KEY, tokens.device_id)
        await set_setting(gid, EXPIRES_KEY, expires_at)


# Безопасные методы на чтение: какие из них VK пускает с этим ключом.
_PROBES = (
    ("users.get", {}),
    ("groups.get", {"filter": "admin"}),
    ("groups.getById", {"group_id": "{gid}", "fields": "is_admin,admin_level"}),
    ("wall.get", {"owner_id": "-{gid}", "count": "1"}),
    ("photos.getWallUploadServer", {"group_id": "{gid}"}),
)


async def _probe(token: str) -> str:
    """Карта «метод=ok|код ошибки» для журнала — когда ключ не приняли."""
    groups = await get_all_active_groups()
    gid = groups[0].group_id if groups else 1
    results = []
    for method, params in _PROBES:
        try:
            await _vk(token, method, **{k: v.format(gid=gid) for k, v in params.items()})
            results.append(f"{method}=ok")
        except vk_read.VKReadError as e:
            results.append(f"{method}={e.code}")
    return " ".join(results)


async def connect_admin_key(tokens: TokenSet) -> list[tuple[int, str]]:
    """Сохранить ключ для наших групп, где VK подтверждает админство владельца
    ключа. Возвращает [(group_id, name)]; ни одной — AdminKeyError."""
    method = "users.get"
    try:
        users = await _vk(tokens.access_token, method)  # владелец ключа, а не чей-то id из URL
        method = "groups.get"
        admin_of = set((await _vk(tokens.access_token, method, filter="admin")).get("items", []))
    except vk_read.VKReadError as e:
        probe = await _probe(tokens.access_token)
        logger.warning(f"Admin key rejected on {method}: {e}; scope={tokens.scope!r}; probe: {probe}")
        raise AdminKeyError(f"VK не принял ключ: {method} — ошибка {e.code} ({e.message})") from e
    if not users:
        raise AdminKeyError("VK не вернул данные аккаунта. Нажмите «Подключить» ещё раз.")
    user = users[0]
    user_id = int(user.get("id") or tokens.user_id)

    ours = [g for g in await get_all_active_groups() if g.group_id in admin_of]
    if not ours:
        raise AdminKeyError(
            "VK не подтвердил, что этот аккаунт — администратор подключённых групп. "
            "Войдите в VK аккаунтом админа группы и нажмите «Подключить» снова."
        )

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    async with _refresh_lock:
        await _store([g.group_id for g in ours], tokens)
        for g in ours:
            await set_setting(g.group_id, USER_ID_KEY, str(user_id))
            await set_setting(g.group_id, NAME_KEY, name)
            await set_setting(g.group_id, ERROR_KEY, "")
            await set_setting(g.group_id, ALERTED_KEY, "")
    logger.info(f"Admin key connected: user={user_id} groups={[g.group_id for g in ours]} "
                f"scope={tokens.scope!r}")
    return [(g.group_id, g.group_name) for g in ours]


async def disconnect_admin_key(group_id: int) -> None:
    for key in _ALL_KEYS:
        await set_setting(group_id, key, "")
    logger.info(f"Admin key disconnected for group {group_id}")


async def _decrypted(group_id: int, key: str) -> str:
    encrypted = await get_setting(group_id, key, "")
    if not encrypted:
        return ""
    try:
        return decrypt_token(encrypted)
    except Exception:
        logger.warning(f"{key} for group {group_id} does not decrypt")
        return ""


async def admin_token(group_id: int) -> str:
    """Сохранённый ключ как есть (без продления) или ""."""
    return await _decrypted(group_id, TOKEN_KEY)


async def _groups_of(user_id: str) -> list[int]:
    return [g.group_id for g in await get_all_active_groups()
            if await get_setting(g.group_id, USER_ID_KEY, "") == user_id]


async def _expires_at(group_id: int) -> float:
    try:
        return float(await get_setting(group_id, EXPIRES_KEY, "0") or 0)
    except ValueError:
        return 0.0


async def fresh_token(group_id: int) -> str:
    """Живой ключ (при необходимости продлённый) или "" — нет, умер или VK ID
    не ответил, а старый уже истёк."""
    if await get_setting(group_id, ERROR_KEY, ""):
        return ""
    access = await admin_token(group_id)
    if not access:
        return ""
    if await _expires_at(group_id) - time.time() > _REFRESH_MARGIN:
        return access

    async with _refresh_lock:
        # Пока ждали замок, пару мог продлить другой вызов.
        access = await admin_token(group_id)
        if await get_setting(group_id, ERROR_KEY, "") or not access:
            return ""
        expires_at = await _expires_at(group_id)
        if expires_at - time.time() > _REFRESH_MARGIN:
            return access
        still_valid = access if expires_at > time.time() else ""

        refresh = await _decrypted(group_id, REFRESH_KEY)
        device_id = await get_setting(group_id, DEVICE_KEY, "")
        if not refresh:
            return still_valid
        try:
            data = await _vkid(
                grant_type="refresh_token", refresh_token=refresh, device_id=device_id,
                client_id=settings.VK_APP_ID, state=secrets.token_urlsafe(32),
            )
        except _TransientError as e:
            logger.warning(f"Admin key refresh for group {group_id} failed, retry later: {e}")
            return still_valid

        user_id = await get_setting(group_id, USER_ID_KEY, "")
        groups = await _groups_of(user_id) or [group_id]
        if data.get("access_token") and data.get("refresh_token"):
            tokens = _token_set(data, device_id)
            await _store(groups, tokens)
            logger.info(f"Admin key refreshed: user={user_id} groups={groups}")
            return tokens.access_token

        error = str(data.get("error", ""))
        if error in _DEAD_REFRESH:
            reason = f"VK ID не продлил ключ: {error} ({data.get('error_description', '')})"
            for gid in groups:
                await set_setting(gid, ERROR_KEY, reason[:300])
            dead_found_in = group_id
        else:
            logger.warning(f"Admin key refresh for group {group_id}: {error or data!r}, retry later")
            return still_valid

    logger.warning(f"Admin key for groups {groups} is dead: {reason}")
    await _alert_dead(dead_found_in)
    return ""


async def get_admin_api(group_id: int) -> API | None:
    """API с ключом админа или None — тогда вызывающий работает ключом сообщества."""
    token = await fresh_token(group_id)
    return API(token=token) if token else None


async def _alert_dead(group_id: int) -> None:
    if await get_setting(group_id, ALERTED_KEY, ""):
        return
    delivered = await _notify_admin(
        group_id,
        "🔑 Личный ключ админа больше не работает (VK: авторизация не прошла — "
        "сменили пароль или отозвали доступ). Фото к постам, удаление нарушений "
        "и баны снова только вручную.\n"
        "Чтобы вернуть: дашборд → группа → «Ключи и доступы» → «Подключить».",
    )
    if delivered:
        await set_setting(group_id, ALERTED_KEY, "1")


async def report_admin_key_failure(group_id: int, e: Exception) -> None:
    """Ошибка VK от ключа админа. Только 5 значит «ключ умер»: помечаем и
    один раз пишем менеджерам; прочие ошибки — про конкретное действие."""
    if getattr(e, "code", None) != _AUTH_FAILED:
        return
    await set_setting(group_id, ERROR_KEY, str(e)[:300])
    logger.warning(f"Admin key for group {group_id} is dead: {e}")
    await _alert_dead(group_id)
