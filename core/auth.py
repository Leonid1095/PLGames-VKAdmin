"""Dashboard authentication — simple token-based auth via cookies."""

import hashlib
import hmac
import logging
import secrets
import time
from fastapi import Request, Response
from core.config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "vkadmin_session"
CSRF_COOKIE_NAME = "vkadmin_csrf"
_SESSION_TOKEN = None

# Mark cookies Secure when the public URL is HTTPS, so the session/CSRF cookies
# are never sent over plaintext. Left off for local http:// dev so login works.
_COOKIE_SECURE = settings.BASE_URL.lower().startswith("https")


def _get_session_token() -> str:
    """Server-side session cookie value.

    Derived from SESSION_SECRET (falls back to JWT_SECRET) via a domain-separated
    HMAC, so the cookie value is not equal to the signing secret or the password.
    """
    global _SESSION_TOKEN
    if _SESSION_TOKEN is None:
        seed = settings.SESSION_SECRET or settings.JWT_SECRET
        _SESSION_TOKEN = hmac.new(
            seed.encode(), b"vkadmin-dashboard-session-v1", hashlib.sha256
        ).hexdigest()[:32]
    return _SESSION_TOKEN


def get_dashboard_password() -> str:
    """Dashboard login password — DASHBOARD_PASSWORD, or JWT_SECRET if unset."""
    return settings.DASHBOARD_PASSWORD or settings.JWT_SECRET


def is_authenticated(request: Request) -> bool:
    """Check if the request has a valid session cookie."""
    cookie = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(cookie, _get_session_token())


def set_auth_cookie(response: Response) -> Response:
    """Set the session cookie on successful login."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=_get_session_token(),
        httponly=True,
        samesite="lax",
        secure=_COOKIE_SECURE,
        max_age=60 * 60 * 24 * 30,  # 30 days
    )
    return response


def clear_auth_cookie(response: Response) -> Response:
    """Remove the session cookie."""
    response.delete_cookie(key=COOKIE_NAME)
    return response


# ─── Login rate limiting ─────────────────────────────────────────────────────

_MAX_LOGIN_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 15 * 60
_failed_logins: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Client address for rate limiting.

    uvicorn слушает 127.0.0.1 за реверс-прокси, поэтому request.client — это
    всегда прокси. Берём ПОСЛЕДНИЙ адрес из X-Forwarded-For: его дописывает наш
    прокси, а первые элементы клиент может подделать.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def login_retry_after(request: Request) -> int:
    """Seconds until this client may attempt a login again (0 = not limited)."""
    now = time.monotonic()
    ip = _client_ip(request)
    attempts = [t for t in _failed_logins.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if attempts:
        _failed_logins[ip] = attempts
    else:
        _failed_logins.pop(ip, None)
    if len(attempts) < _MAX_LOGIN_FAILURES:
        return 0
    return int(_LOGIN_WINDOW_SECONDS - (now - attempts[0])) + 1


def record_login_failure(request: Request) -> None:
    now = time.monotonic()
    # Не даём словарю расти бесконечно при переборе с разных IP.
    if len(_failed_logins) > 1000:
        for ip, times in list(_failed_logins.items()):
            alive = [t for t in times if now - t < _LOGIN_WINDOW_SECONDS]
            if alive:
                _failed_logins[ip] = alive
            else:
                del _failed_logins[ip]
    _failed_logins.setdefault(_client_ip(request), []).append(now)
    logger.warning(f"Failed dashboard login attempt from {_client_ip(request)}")


def record_login_success(request: Request) -> None:
    _failed_logins.pop(_client_ip(request), None)


# ─── CSRF protection ─────────────────────────────────────────────────────────

def get_csrf_token(request: Request) -> str:
    """Get or generate a CSRF token, stored in a cookie."""
    token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not token:
        token = secrets.token_hex(32)
    return token


def set_csrf_cookie(response: Response, token: str) -> Response:
    """Set the CSRF token cookie."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=_COOKIE_SECURE,
        max_age=60 * 60 * 24,  # 24 hours
    )
    return response


async def verify_csrf_token(request: Request) -> bool:
    """Verify that the CSRF token in the form matches the cookie."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_token:
        return False
    form = await request.form()
    form_token = str(form.get("_csrf", ""))
    return hmac.compare_digest(cookie_token, form_token)
