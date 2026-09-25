"""Личный ключ админа группы — для того, чего ключ сообщества VK не умеет.

Ключ сообщества не пускает в photos.getWallUploadServer/saveWallPhoto,
wall.deleteComment, groups.ban/unban, wall.pin/unpin (err 27, проверено
23.09.2026). Владелец подключает свой ключ кнопкой в дашборде (VK OAuth,
web/oauth.py), здесь — хранение, проверка и выдача API.

Ключ хранится зашифрованным в настройках каждой группы, где VK подтвердил,
что этот человек — админ. Умершим считается только по ошибке VK 5.
"""

import logging

import httpx
from vkbottle import API

from core import vk_read
from core.crypto import decrypt_token, encrypt_token
from database.service import get_all_active_groups, get_setting, set_setting

logger = logging.getLogger(__name__)

TOKEN_KEY = "admin_user_token"
USER_ID_KEY = "admin_user_id"
NAME_KEY = "admin_user_name"
ERROR_KEY = "admin_key_error"
ALERTED_KEY = "admin_key_alerted"

ADMIN_KEY_HINT = "подключите личный ключ в панели: «Ключи и доступы» → «Подключить»"
_AUTH_FAILED = 5  # VK: User authorization failed — ключ отозван или истёк


class AdminKeyError(Exception):
    """Подключить ключ нельзя; текст — для владельца."""


def _client() -> httpx.AsyncClient:
    # Отдельная фабрика — точка подмены транспорта в тестах.
    return httpx.AsyncClient(timeout=15)


async def _vk(token: str, method: str, **params):
    async with _client() as client:
        resp = await client.post(
            f"{vk_read.VK_API}/{method}",
            data={**params, "access_token": token, "v": vk_read.VK_API_VERSION},
        )
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise vk_read.VKReadError(int(err.get("error_code", 0)), str(err.get("error_msg", "")))
    return data["response"]


async def _notify_admin(group_id: int, text: str) -> bool:
    from core.escalation import notify_group_admins
    return await notify_group_admins(group_id, text)


async def connect_admin_key(token: str, user_id: int) -> list[tuple[int, str]]:
    """Сохранить ключ для наших групп, где VK подтверждает админство владельца
    ключа. Возвращает [(group_id, name)]; ни одной — AdminKeyError."""
    try:
        admin_of = set((await _vk(token, "groups.get", filter="admin")).get("items", []))
        user = (await _vk(token, "users.get", user_ids=user_id))[0]
    except vk_read.VKReadError as e:
        raise AdminKeyError(f"VK не принял ключ: ошибка {e.code} ({e.message})") from e

    ours = [g for g in await get_all_active_groups() if g.group_id in admin_of]
    if not ours:
        raise AdminKeyError(
            "VK не подтвердил, что этот аккаунт — администратор подключённых групп. "
            "Войдите в VK аккаунтом админа группы и нажмите «Подключить» снова."
        )

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    encrypted = encrypt_token(token)
    for g in ours:
        await set_setting(g.group_id, TOKEN_KEY, encrypted)
        await set_setting(g.group_id, USER_ID_KEY, str(user_id))
        await set_setting(g.group_id, NAME_KEY, name)
        await set_setting(g.group_id, ERROR_KEY, "")
        await set_setting(g.group_id, ALERTED_KEY, "")
    logger.info(f"Admin key connected: user={user_id} groups={[g.group_id for g in ours]}")
    return [(g.group_id, g.group_name) for g in ours]


async def disconnect_admin_key(group_id: int) -> None:
    for key in (TOKEN_KEY, USER_ID_KEY, NAME_KEY, ERROR_KEY, ALERTED_KEY):
        await set_setting(group_id, key, "")
    logger.info(f"Admin key disconnected for group {group_id}")


async def admin_token(group_id: int) -> str:
    """Расшифрованный ключ или "" (нет ключа / сменился ENCRYPTION_KEY)."""
    encrypted = await get_setting(group_id, TOKEN_KEY, "")
    if not encrypted:
        return ""
    try:
        return decrypt_token(encrypted)
    except Exception:
        logger.warning(f"Admin key for group {group_id} does not decrypt")
        return ""


async def get_admin_api(group_id: int) -> API | None:
    """API с ключом админа или None — тогда вызывающий работает ключом сообщества."""
    if await get_setting(group_id, ERROR_KEY, ""):
        return None  # помечен умершим — ждём переподключения
    token = await admin_token(group_id)
    return API(token=token) if token else None


async def report_admin_key_failure(group_id: int, e: Exception) -> None:
    """Ошибка VK от ключа админа. Только 5 значит «ключ умер»: помечаем и
    один раз пишем менеджерам; прочие ошибки — про конкретное действие."""
    if getattr(e, "code", None) != _AUTH_FAILED:
        return
    await set_setting(group_id, ERROR_KEY, str(e)[:300])
    logger.warning(f"Admin key for group {group_id} is dead: {e}")
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
