"""Dashboard authentication — simple token-based auth via cookies."""

import hashlib
import hmac
import logging
import secrets
from fastapi import Request, Response
from core.config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "vkadmin_session"
CSRF_COOKIE_NAME = "vkadmin_csrf"
_SESSION_TOKEN = None


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
        max_age=60 * 60 * 24 * 30,  # 30 days
    )
    return response


def clear_auth_cookie(response: Response) -> Response:
    """Remove the session cookie."""
    response.delete_cookie(key=COOKIE_NAME)
    return response


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
