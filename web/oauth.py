"""OAuth flow for connecting VK groups to the bot."""

import logging
import secrets
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.auth import is_authenticated
from core.config import settings
from core.crypto import encrypt_token
from database.service import create_group, get_group, seed_default_settings

logger = logging.getLogger(__name__)
router = APIRouter()


async def _stable_secret(gid: int) -> str:
    """Секрет Callback API для (пере)подключения: существующий, если есть.

    Ротация при каждом переподключении разводила БД и VK: новый секрет попадал
    в БД раньше, чем в VK, и события отбрасывались как «Invalid secret».
    """
    group = await get_group(gid)
    if group and group.secret_key:
        return group.secret_key
    return secrets.token_hex(16)


def _vk_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}"
    err = data.get("error")
    return f"{err.get('error_code')}: {err.get('error_msg')}" if err else ""


async def _setup_callback_api(token: str, gid: int, secret_key: str) -> bool:
    """Idempotently register/refresh this group's Callback API server.

    VK has no upsert: ``addCallbackServer`` creates a NEW server on every call, so
    reconnecting a group used to pile up duplicate "VKAdmin Bot" servers (and a
    duplicate could end up the one VK delivers to, with the wrong/empty secret).
    Here we reuse the existing server that points at our events URL — and editing
    it also re-validates a server VK had marked ``failed`` — instead of adding one.
    Enables exactly the event types ``web/vk_callback.py`` dispatches.
    """
    callback_url = f"{settings.BASE_URL}/api/vk/events"
    common = {"access_token": token, "v": "5.199"}
    async with httpx.AsyncClient() as client:
        servers_resp = await client.get(
            "https://api.vk.com/method/groups.getCallbackServers",
            params={"group_id": gid, **common},
        )
        items = servers_resp.json().get("response", {}).get("items", [])
        server_id = next((s["id"] for s in items if s.get("url") == callback_url), None)

        if server_id:
            # Refresh url/title/secret on the existing server; this re-checks the
            # endpoint, flipping a previously-failed server back to "ok".
            edit_resp = await client.get(
                "https://api.vk.com/method/groups.editCallbackServer",
                params={
                    "group_id": gid, "server_id": server_id, "url": callback_url,
                    "title": "VKAdmin Bot", "secret_key": secret_key, **common,
                },
            )
            if err := _vk_error(edit_resp):
                logger.error(f"Callback API: editCallbackServer failed for group {gid}: {err}")
                return False
        else:
            add_resp = await client.get(
                "https://api.vk.com/method/groups.addCallbackServer",
                params={
                    "group_id": gid, "url": callback_url,
                    "title": "VKAdmin Bot", "secret_key": secret_key, **common,
                },
            )
            if err := _vk_error(add_resp):
                logger.error(f"Callback API: addCallbackServer failed for group {gid}: {err}")
                return False
            server_id = add_resp.json().get("response", {}).get("server_id")

        if not server_id:
            logger.error(f"Callback API: no server_id for group {gid}")
            return False

        settings_resp = await client.get(
            "https://api.vk.com/method/groups.setCallbackSettings",
            params={
                "group_id": gid, "server_id": server_id,
                "message_new": 1, "message_reply": 1, "wall_reply_new": 1,
                "group_join": 1, "group_leave": 1,
                "like_add": 1, "wall_repost": 1, **common,
            },
        )
        if err := _vk_error(settings_resp):
            logger.error(f"Callback API: setCallbackSettings failed for group {gid}: {err}")
            return False
        logger.info(f"Callback API configured for group {gid} (server_id={server_id})")
        return True


async def _resolve_group_id(raw: str) -> int | None:
    """Приводит пользовательский ввод к числовому ID группы.

    Принимает числовой ID, короткое имя (plgames_bot), ссылку
    (vk.com/plgames_bot) и формы club123/public123. Короткие имена
    резолвятся через groups.getById сервисным ключом приложения.
    """
    s = raw.strip().rstrip("/")
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    s = s.lstrip("@").split("?")[0]
    for prefix in ("club", "public", "event"):
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            return int(s[len(prefix):])
    if s.isdigit():
        return int(s)
    if not s or not settings.VK_APP_SERVICE_KEY:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.vk.com/method/groups.getById",
            params={
                "group_id": s,
                "access_token": settings.VK_APP_SERVICE_KEY,
                "v": "5.199",
            },
        )
    data = resp.json()
    if "error" in data:
        logger.warning(f"Failed to resolve group '{s}': {data['error'].get('error_msg')}")
        return None
    groups = data.get("response", {})
    if isinstance(groups, dict):
        groups = groups.get("groups", [])
    if isinstance(groups, list) and groups:
        return int(groups[0].get("id", 0)) or None
    return None


@router.get("/api/vk/oauth")
async def start_oauth(request: Request, group_ids: str = ""):
    """
    Step 1: Redirect admin to VK OAuth page to authorize the bot for their group.
    Usage: /api/vk/oauth?group_ids=123456
    If group_ids is empty, VK will let the user choose which group to authorize.
    """
    # Подключать группы может только владелец панели: иначе кто угодно мог бы
    # повесить свою группу на наш инстанс и жечь LLM-бюджет.
    if not is_authenticated(request):
        return RedirectResponse("/dashboard/login", status_code=303)

    if not settings.VK_APP_ID:
        return HTMLResponse(
            "<h2>VK App not configured</h2>"
            "<p>Set VK_APP_ID and VK_APP_SECRET in .env</p>",
            status_code=500,
        )

    if group_ids:
        resolved = []
        for part in group_ids.split(","):
            gid = await _resolve_group_id(part)
            if gid:
                resolved.append(str(gid))
        if not resolved:
            from html import escape
            return HTMLResponse(
                "<h2>Группа не найдена</h2>"
                f"<p>Не удалось определить группу по «{escape(group_ids)}». "
                "Укажите числовой ID, короткое имя или ссылку на группу.</p>"
                '<p><a href="/dashboard">&larr; Назад в панель</a></p>',
                status_code=400,
            )
        group_ids = ",".join(resolved)

    redirect_uri = f"{settings.BASE_URL}/api/vk/callback"
    scope = "messages,wall,manage,photos"

    # Generate state parameter to prevent CSRF
    state = secrets.token_hex(16)

    vk_auth_url = (
        f"https://oauth.vk.com/authorize?"
        f"client_id={settings.VK_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&response_type=code"
        f"&state={state}"
        f"&v=5.199"
        # revoke=1 — без него VK может отдать закэшированный (в т.ч. давно
        # отозванный) групповой токен: OAuth проходит «успешно», а каждый
        # вызов API падает с err 27. Принудительный перезапрос согласия
        # гарантирует свежий токен.
        f"&revoke=1"
    )
    if group_ids:
        vk_auth_url += f"&group_ids={group_ids}"

    response = RedirectResponse(vk_auth_url)
    response.set_cookie(
        key="vkadmin_oauth_state",
        value=state,
        httponly=True,
        samesite="lax",
        max_age=600,  # 10 minutes
    )
    return response


ADMIN_STATE_COOKIE = "vkadmin_admin_oauth_state"
ADMIN_SCOPE = "wall,photos,groups,offline"


@router.get("/api/vk/admin-oauth")
async def start_admin_oauth(request: Request):
    """Подключить личный ключ админа (зачем — core/admin_key.py).

    Тот же экран VK и тот же redirect_uri, что у групп (он уже разрешён в
    настройках приложения), но без group_ids, со своими правами и своей
    state-кукой — по ней колбэк отличает этот поток от группового."""
    if not is_authenticated(request):
        return RedirectResponse("/dashboard/login", status_code=303)
    if not settings.VK_APP_ID:
        return HTMLResponse(
            "<h2>VK App not configured</h2><p>Set VK_APP_ID and VK_APP_SECRET in .env</p>",
            status_code=500,
        )

    state = secrets.token_hex(16)
    vk_auth_url = (
        f"https://oauth.vk.com/authorize?"
        f"client_id={settings.VK_APP_ID}"
        f"&redirect_uri={settings.BASE_URL}/api/vk/callback"
        f"&scope={ADMIN_SCOPE}"
        f"&response_type=code"
        f"&state={state}"
        f"&v=5.199"
        f"&revoke=1"  # иначе VK может отдать закэшированный отозванный ключ
    )
    response = RedirectResponse(vk_auth_url)
    response.set_cookie(
        key=ADMIN_STATE_COOKIE, value=state, httponly=True, samesite="lax", max_age=600,
    )
    return response


def _is_admin_flow(request: Request, state: str) -> bool:
    cookie = request.cookies.get(ADMIN_STATE_COOKIE, "")
    return bool(cookie and state) and secrets.compare_digest(cookie, state)


def _admin_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    response = HTMLResponse(f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>VKAdmin — {title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
        ul {{ line-height: 2; }}
        a {{ color: #1976d2; }}
    </style></head>
    <body>
        <h2>{title}</h2>
        {body}
        <p><a href="{settings.BASE_URL}/dashboard">Вернуться в панель управления</a></p>
    </body></html>
    """, status_code=status_code)
    response.delete_cookie(ADMIN_STATE_COOKIE)
    return response


async def _finish_admin_oauth(token: str, user_id: int) -> HTMLResponse:
    from html import escape
    from core.admin_key import AdminKeyError, connect_admin_key

    try:
        if not token or not user_id:
            raise AdminKeyError("VK не вернул ключ. Нажмите «Подключить» ещё раз.")
        connected = await connect_admin_key(token, user_id)
    except AdminKeyError as e:
        logger.warning(f"Admin key not connected: {e}")
        return _admin_page("Личный ключ не подключён", f"<p>{escape(str(e))}</p>", 400)

    items = "".join(f"<li>{escape(name)} (ID: {gid})</li>" for gid, name in connected)
    return _admin_page(
        "🔑 Личный ключ админа подключён",
        "<p>Теперь бот сам загружает фото к постам, удаляет нарушения, банит и "
        f"закрепляет посты в группах:</p><ul>{items}</ul>",
    )


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _exchange_code(code: str) -> dict:
    """Код авторизации → ответ oauth.vk.com/access_token (общий для групп и админа)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://oauth.vk.com/access_token",
            params={
                "client_id": settings.VK_APP_ID,
                "client_secret": settings.VK_APP_SECRET,
                "redirect_uri": f"{settings.BASE_URL}/api/vk/callback",
                "code": code,
            },
        )
    return resp.json()


@router.get("/api/vk/callback")
async def oauth_callback(request: Request, code: str = "", error: str = "", error_description: str = "", state: str = ""):
    """
    Step 2: VK redirects back with an authorization code.
    Exchange it for a group access token.
    """
    # Log param keys for debugging, but never the values — they can carry the
    # authorization code or access token (secret-in-logs leak).
    _sensitive = {"code", "access_token", "token", "secret"}
    _safe = {
        k: ("<redacted>" if k in _sensitive else v)
        for k, v in request.query_params.items()
    }
    logger.info(f"OAuth callback params: {_safe}")

    if error:
        from html import escape
        return HTMLResponse(
            f"<h2>Ошибка авторизации</h2><p>{escape(error)}: {escape(error_description)}</p>",
            status_code=400,
        )

    if not code:
        # VK Mini App launch params — redirect to Mini App
        if request.query_params.get("vk_app_id"):
            return RedirectResponse(f"/miniapp?{request.query_params}")

        # VK might return token in fragment (Standalone app) — show JS extractor
        return HTMLResponse("""
        <!DOCTYPE html>
        <html><head><meta charset="utf-8"><title>VKAdmin — Авторизация</title>
        <script>
            // VK Standalone apps return token in URL fragment (#access_token=...)
            if (window.location.hash) {
                var params = new URLSearchParams(window.location.hash.substring(1));
                var code = params.get('code');
                var accessToken = params.get('access_token');
                if (code) {
                    // Весь fragment целиком: вместе с code должен уйти state.
                    window.location.href = '/api/vk/callback?' + window.location.hash.substring(1);
                } else if (accessToken) {
                    // Redirect with token directly
                    window.location.href = '/api/vk/callback/token?' + window.location.hash.substring(1);
                } else {
                    document.getElementById('msg').textContent = 'Параметры: ' + window.location.hash;
                }
            } else {
                document.getElementById('msg').textContent = 'Код авторизации не получен. Параметры: ' + window.location.search;
            }
        </script></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 50px auto; padding: 20px;">
            <h2>Обработка авторизации...</h2>
            <p id="msg">Подождите...</p>
        </body></html>
        """, status_code=200)

    # Личный ключ админа: тот же redirect_uri, отличаем по своей state-куке.
    if _is_admin_flow(request, state):
        if not is_authenticated(request):
            return RedirectResponse("/dashboard/login", status_code=303)
        data = await _exchange_code(code)
        if "error" in data:
            from html import escape
            logger.error(f"Admin key OAuth error: {data.get('error')}")
            return _admin_page(
                "Личный ключ не подключён",
                f"<p>VK отказал: {escape(str(data.get('error_description') or data.get('error')))}</p>",
                400,
            )
        return await _finish_admin_oauth(data.get("access_token", ""), _int(data.get("user_id")))

    # Verify OAuth state parameter
    cookie_state = request.cookies.get("vkadmin_oauth_state", "")
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        logger.warning("OAuth state mismatch — possible CSRF")
        return HTMLResponse(
            "<h2>Ошибка безопасности</h2><p>Несоответствие state-параметра. Попробуйте ещё раз.</p>",
            status_code=400,
        )

    # Exchange code for token
    data = await _exchange_code(code)

    if "error" in data:
        logger.error(f"OAuth error: {data}")
        return HTMLResponse(
            f"<h2>Ошибка авторизации</h2><p>{data.get('error_description', data.get('error'))}</p>",
            status_code=400,
        )

    # VK returns tokens as access_token_GROUPID for each authorized group
    groups_connected = []
    groups_failed = []
    for key, value in data.items():
        if key.startswith("access_token_"):
            gid = int(key.replace("access_token_", ""))
            token = value
            secret_key = await _stable_secret(gid)

            # Get group info — заодно проверка живости токена: VK может выдать
            # уже отозванный токен (err 27); без проверки группа выглядела бы
            # «подключённой», но с мёртвым ботом (пустой confirmation_code,
            # Callback API не настроен).
            group_name = f"Group {gid}"
            token_error = ""
            try:
                async with httpx.AsyncClient() as client:
                    info_resp = await client.get(
                        "https://api.vk.com/method/groups.getById",
                        params={
                            "group_id": gid,
                            "access_token": token,
                            "v": "5.199",
                        },
                    )
                info_data = info_resp.json()
                if "error" in info_data:
                    token_error = info_data["error"].get("error_msg", "unknown error")
                groups_list = info_data.get("response", {}).get("groups", info_data.get("response", []))
                if isinstance(groups_list, list) and groups_list:
                    group_name = groups_list[0].get("name", group_name)
                elif isinstance(groups_list, dict):
                    group_name = groups_list.get("name", group_name)
            except Exception as e:
                logger.warning(f"Failed to get group name for {gid}: {e}")

            if token_error:
                logger.error(f"Token for group {gid} is unusable: {token_error}")
                groups_failed.append(f"{gid}: {token_error}")
                continue

            # Get confirmation code for Callback API
            confirmation_code = ""
            try:
                async with httpx.AsyncClient() as client:
                    conf_resp = await client.get(
                        "https://api.vk.com/method/groups.getCallbackConfirmationCode",
                        params={
                            "group_id": gid,
                            "access_token": token,
                            "v": "5.199",
                        },
                    )
                conf_data = conf_resp.json()
                confirmation_code = conf_data.get("response", {}).get("code", "")
            except Exception as e:
                logger.warning(f"Failed to get confirmation code for {gid}: {e}")

            # Save group to DB
            encrypted_token = encrypt_token(token)

            # Extract admin VK ID from OAuth response
            admin_vk_id = 0
            if "user_id" in data:
                admin_vk_id = int(data["user_id"])
            else:
                # VK sometimes returns user_id per group: user_id_GROUPID
                uid_key = f"user_id_{gid}"
                if uid_key in data:
                    admin_vk_id = int(data[uid_key])

            await create_group(
                group_id=gid,
                group_name=group_name,
                access_token=encrypted_token,
                admin_vk_id=admin_vk_id,
                confirmation_code=confirmation_code,
                secret_key=secret_key,
            )

            # Seed default settings
            await seed_default_settings(gid)

            # Callback API — ДО медленной настройки ИИ: события начинают
            # доходить сразу, а не через минуту LLM-анализа (idempotent).
            try:
                await _setup_callback_api(token, gid, secret_key)
            except Exception as e:
                logger.error(f"Failed to setup Callback API for {gid}: {e!r}")

            # Auto-setup AI personality for this group
            try:
                from core.group_setup import setup_group_ai
                await setup_group_ai(gid, token)
            except Exception as e:
                logger.warning(f"AI setup failed for group {gid}, will use defaults: {e}")

            groups_connected.append(f"{group_name} (ID: {gid})")

    if not groups_connected:
        from html import escape as html_escape
        if groups_failed:
            details = "".join(f"<li>{html_escape(f)}</li>" for f in groups_failed)
            body = (
                f"<p>VK выдал неработающий токен:</p><ul>{details}</ul>"
                "<p>Нажмите «Подключить» ещё раз — согласие будет запрошено "
                "заново и VK выпустит свежий токен.</p>"
            )
        else:
            body = "<p>Попробуйте ещё раз и убедитесь, что вы выбрали группу.</p>"
        return HTMLResponse(
            "<h2>Не удалось подключить группы</h2>" + body
            + '<p><a href="/dashboard">&larr; Назад в панель</a></p>',
            status_code=400,
        )

    from html import escape as html_escape
    groups_html = "".join(f"<li>{html_escape(g)}</li>" for g in groups_connected)
    return _success_html(groups_html)


def _success_html(groups_html: str) -> HTMLResponse:
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>VKAdmin — Подключено!</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
        .success {{ color: #2e7d32; }}
        ul {{ line-height: 2; }}
        a {{ color: #1976d2; }}
    </style></head>
    <body>
        <h2 class="success">Бот успешно подключен!</h2>
        <p>Подключённые группы:</p>
        <ul>{groups_html}</ul>
        <p>Бот уже работает. Напишите в сообщения группы, чтобы проверить.</p>
        <p><a href="{settings.BASE_URL}/dashboard">Перейти в панель управления</a></p>
    </body></html>
    """)


@router.get("/api/vk/callback/token")
async def oauth_token_callback(request: Request):
    """
    Handle Standalone-app flow where VK returns tokens in URL fragment.
    JS on the client redirects here with token params as query string.
    """
    # Только для залогиненного владельца панели И только в рамках OAuth, который
    # он сам начал (state из куки). Без state это CSRF: GET + сессионная кука
    # SameSite=Lax уходят при переходе по чужой ссылке — атакующий подменял бы
    # токен и админа любой группы своими.
    if not is_authenticated(request):
        return RedirectResponse("/dashboard/login", status_code=303)
    state = request.query_params.get("state", "")
    if _is_admin_flow(request, state):
        return await _finish_admin_oauth(
            request.query_params.get("access_token", ""),
            _int(request.query_params.get("user_id")),
        )
    cookie_state = request.cookies.get("vkadmin_oauth_state", "")
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        logger.warning("Token callback without matching OAuth state — possible CSRF")
        return HTMLResponse(
            "<h2>Ошибка безопасности</h2><p>Несоответствие state-параметра. "
            "Начните подключение заново из панели.</p>",
            status_code=403,
        )

    params = dict(request.query_params)
    logger.info(f"Token callback params: {list(params.keys())}")

    groups_connected = []

    for key, value in params.items():
        if key.startswith("access_token_"):
            gid = int(key.replace("access_token_", ""))
            token = value
            secret_key = await _stable_secret(gid)

            group_name = f"Group {gid}"
            token_error = ""
            try:
                async with httpx.AsyncClient() as client:
                    info_resp = await client.get(
                        "https://api.vk.com/method/groups.getById",
                        params={"group_id": gid, "access_token": token, "v": "5.199"},
                    )
                info_data = info_resp.json()
                if "error" in info_data:
                    token_error = info_data["error"].get("error_msg", "unknown error")
                groups_list = info_data.get("response", {}).get("groups", info_data.get("response", []))
                if isinstance(groups_list, list) and groups_list:
                    group_name = groups_list[0].get("name", group_name)
            except Exception as e:
                logger.warning(f"Failed to get group name for {gid}: {e}")

            if token_error:
                logger.error(f"Token for group {gid} is unusable: {token_error}")
                continue

            confirmation_code = ""
            try:
                async with httpx.AsyncClient() as client:
                    conf_resp = await client.get(
                        "https://api.vk.com/method/groups.getCallbackConfirmationCode",
                        params={"group_id": gid, "access_token": token, "v": "5.199"},
                    )
                conf_data = conf_resp.json()
                confirmation_code = conf_data.get("response", {}).get("code", "")
            except Exception as e:
                logger.warning(f"Failed to get confirmation code for {gid}: {e}")

            encrypted_token = encrypt_token(token)
            admin_vk_id = int(params.get("user_id", 0))

            await create_group(
                group_id=gid,
                group_name=group_name,
                access_token=encrypted_token,
                admin_vk_id=admin_vk_id,
                confirmation_code=confirmation_code,
                secret_key=secret_key,
            )
            await seed_default_settings(gid)

            # Setup Callback API first (idempotent), then the slow AI setup
            try:
                await _setup_callback_api(token, gid, secret_key)
            except Exception as e:
                logger.error(f"Failed to setup Callback API for {gid}: {e!r}")

            # Auto-setup AI personality
            try:
                from core.group_setup import setup_group_ai
                await setup_group_ai(gid, token)
            except Exception as e:
                logger.warning(f"AI setup failed for group {gid}: {e}")

            groups_connected.append(f"{group_name} (ID: {gid})")

    if not groups_connected:
        return HTMLResponse(
            "<h2>Не удалось подключить группы</h2>"
            "<p>Токены не найдены в параметрах.</p>",
            status_code=400,
        )

    from html import escape as html_escape
    groups_html = "".join(f"<li>{html_escape(g)}</li>" for g in groups_connected)
    return _success_html(groups_html)
