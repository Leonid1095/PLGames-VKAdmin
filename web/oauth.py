"""OAuth flow for connecting VK groups to the bot."""

import logging
import secrets
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.config import settings
from core.crypto import encrypt_token
from database.service import create_group, seed_default_settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/vk/oauth")
async def start_oauth(request: Request, group_ids: str = ""):
    """
    Step 1: Redirect admin to VK OAuth page to authorize the bot for their group.
    Usage: /api/vk/oauth?group_ids=123456
    If group_ids is empty, VK will let the user choose which group to authorize.
    """
    if not settings.VK_APP_ID:
        return HTMLResponse(
            "<h2>VK App not configured</h2>"
            "<p>Set VK_APP_ID and VK_APP_SECRET in .env</p>",
            status_code=500,
        )

    redirect_uri = f"{settings.BASE_URL}/api/vk/callback"
    scope = "messages,wall,manage,photos"

    vk_auth_url = (
        f"https://oauth.vk.com/authorize?"
        f"client_id={settings.VK_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&response_type=code"
        f"&v=5.199"
    )
    if group_ids:
        vk_auth_url += f"&group_ids={group_ids}"

    return RedirectResponse(vk_auth_url)


@router.get("/api/vk/callback")
async def oauth_callback(request: Request, code: str = "", error: str = "", error_description: str = ""):
    """
    Step 2: VK redirects back with an authorization code.
    Exchange it for a group access token.
    """
    # Log all query params for debugging
    logger.info(f"OAuth callback params: {dict(request.query_params)}")

    if error:
        return HTMLResponse(
            f"<h2>Ошибка авторизации</h2><p>{error}: {error_description}</p>",
            status_code=400,
        )

    if not code:
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
                    window.location.href = '/api/vk/callback?code=' + code;
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

    redirect_uri = f"{settings.BASE_URL}/api/vk/callback"

    # Exchange code for token
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://oauth.vk.com/access_token",
            params={
                "client_id": settings.VK_APP_ID,
                "client_secret": settings.VK_APP_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )

    data = resp.json()

    if "error" in data:
        logger.error(f"OAuth error: {data}")
        return HTMLResponse(
            f"<h2>Ошибка авторизации</h2><p>{data.get('error_description', data.get('error'))}</p>",
            status_code=400,
        )

    # VK returns tokens as access_token_GROUPID for each authorized group
    groups_connected = []
    for key, value in data.items():
        if key.startswith("access_token_"):
            gid = int(key.replace("access_token_", ""))
            token = value
            secret_key = secrets.token_hex(16)

            # Get group info
            group_name = f"Group {gid}"
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
                groups_list = info_data.get("response", {}).get("groups", info_data.get("response", []))
                if isinstance(groups_list, list) and groups_list:
                    group_name = groups_list[0].get("name", group_name)
                elif isinstance(groups_list, dict):
                    group_name = groups_list.get("name", group_name)
            except Exception as e:
                logger.warning(f"Failed to get group name for {gid}: {e}")

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
            admin_id = data.get("expires_in", 0)  # VK also returns user info sometimes

            # Try to extract admin VK ID from the response
            admin_vk_id = 0
            for k, v in data.items():
                if k == "user_id" or k.startswith("user_id"):
                    admin_vk_id = int(v)
                    break

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

            # Set up Callback API server for this group
            try:
                callback_url = f"{settings.BASE_URL}/api/vk/events"
                async with httpx.AsyncClient() as client:
                    # Add callback server
                    add_resp = await client.get(
                        "https://api.vk.com/method/groups.addCallbackServer",
                        params={
                            "group_id": gid,
                            "url": callback_url,
                            "title": "VKAdmin Bot",
                            "secret_key": secret_key,
                            "access_token": token,
                            "v": "5.199",
                        },
                    )
                    add_data = add_resp.json()
                    server_id = add_data.get("response", {}).get("server_id")

                    if server_id:
                        # Enable message_new and wall_reply_new events
                        await client.get(
                            "https://api.vk.com/method/groups.setCallbackSettings",
                            params={
                                "group_id": gid,
                                "server_id": server_id,
                                "message_new": 1,
                                "wall_reply_new": 1,
                                "wall_post_new": 1,
                                "group_join": 1,
                                "group_leave": 1,
                                "access_token": token,
                                "v": "5.199",
                            },
                        )
                        logger.info(f"Callback API configured for group {gid}")
            except Exception as e:
                logger.error(f"Failed to setup Callback API for {gid}: {e}")

            groups_connected.append(f"{group_name} (ID: {gid})")

    if not groups_connected:
        return HTMLResponse(
            "<h2>Не удалось подключить группы</h2>"
            "<p>Попробуйте ещё раз и убедитесь, что вы выбрали группу.</p>",
            status_code=400,
        )

    groups_html = "".join(f"<li>{g}</li>" for g in groups_connected)
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
    params = dict(request.query_params)
    logger.info(f"Token callback params: {list(params.keys())}")

    groups_connected = []

    for key, value in params.items():
        if key.startswith("access_token_"):
            gid = int(key.replace("access_token_", ""))
            token = value
            secret_key = secrets.token_hex(16)

            group_name = f"Group {gid}"
            try:
                async with httpx.AsyncClient() as client:
                    info_resp = await client.get(
                        "https://api.vk.com/method/groups.getById",
                        params={"group_id": gid, "access_token": token, "v": "5.199"},
                    )
                info_data = info_resp.json()
                groups_list = info_data.get("response", {}).get("groups", info_data.get("response", []))
                if isinstance(groups_list, list) and groups_list:
                    group_name = groups_list[0].get("name", group_name)
            except Exception as e:
                logger.warning(f"Failed to get group name for {gid}: {e}")

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

            # Setup Callback API
            try:
                callback_url = f"{settings.BASE_URL}/api/vk/events"
                async with httpx.AsyncClient() as client:
                    add_resp = await client.get(
                        "https://api.vk.com/method/groups.addCallbackServer",
                        params={
                            "group_id": gid, "url": callback_url,
                            "title": "VKAdmin Bot", "secret_key": secret_key,
                            "access_token": token, "v": "5.199",
                        },
                    )
                    add_data = add_resp.json()
                    server_id = add_data.get("response", {}).get("server_id")
                    if server_id:
                        await client.get(
                            "https://api.vk.com/method/groups.setCallbackSettings",
                            params={
                                "group_id": gid, "server_id": server_id,
                                "message_new": 1, "wall_reply_new": 1,
                                "access_token": token, "v": "5.199",
                            },
                        )
                        logger.info(f"Callback API configured for group {gid}")
            except Exception as e:
                logger.error(f"Failed to setup Callback API for {gid}: {e}")

            groups_connected.append(f"{group_name} (ID: {gid})")

    if not groups_connected:
        return HTMLResponse(
            "<h2>Не удалось подключить группы</h2>"
            "<p>Токены не найдены в параметрах.</p>",
            status_code=400,
        )

    groups_html = "".join(f"<li>{g}</li>" for g in groups_connected)
    return _success_html(groups_html)
