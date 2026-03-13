"""VK Mini App launch params verification and JWT tokens."""

import hashlib
import hmac
import base64
import time
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

from core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class VKLaunchParams:
    vk_user_id: int
    vk_app_id: int
    vk_group_id: int = 0  # present when opened from a group
    vk_platform: str = ""
    vk_ref: str = ""


def verify_vk_launch_params(query_params: dict) -> VKLaunchParams | None:
    """
    Verify VK Mini App launch params signature.
    Returns VKLaunchParams if valid, None if invalid.

    VK signs all vk_* params with HMAC-SHA256 using the app secret.
    """
    sign = query_params.get("sign", "")
    if not sign:
        return None

    # Collect and sort vk_* params
    vk_params = {k: v for k, v in sorted(query_params.items()) if k.startswith("vk_")}
    if not vk_params:
        return None

    # Build query string
    query_string = urlencode(vk_params)

    # Compute HMAC-SHA256 — try Mini App secret first, then main app secret
    secrets_to_try = []
    if settings.VK_MINIAPP_SECRET:
        secrets_to_try.append(settings.VK_MINIAPP_SECRET)
    if settings.VK_APP_SECRET:
        secrets_to_try.append(settings.VK_APP_SECRET)

    if not secrets_to_try:
        logger.error("No VK app secrets configured, cannot verify launch params")
        return None

    verified = False
    for secret in secrets_to_try:
        computed = hmac.new(
            secret.encode(), query_string.encode(), hashlib.sha256
        ).digest()
        computed_sign = base64.urlsafe_b64encode(computed).rstrip(b"=").decode()
        if hmac.compare_digest(computed_sign, sign):
            verified = True
            break

    if not verified:
        logger.warning("VK launch params signature mismatch")
        return None

    return VKLaunchParams(
        vk_user_id=int(vk_params.get("vk_user_id", 0)),
        vk_app_id=int(vk_params.get("vk_app_id", 0)),
        vk_group_id=int(vk_params.get("vk_group_id", 0)),
        vk_platform=vk_params.get("vk_platform", ""),
        vk_ref=vk_params.get("vk_ref", ""),
    )


def create_miniapp_token(vk_user_id: int, vk_group_id: int = 0) -> str:
    """Create a signed JWT-like token for Mini App session (1 hour)."""
    payload = {
        "uid": vk_user_id,
        "gid": vk_group_id,
        "exp": int(time.time()) + 3600,
    }
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(
        settings.JWT_SECRET.encode(), data.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{data}.{sig}"


def verify_miniapp_token(token: str) -> dict | None:
    """Verify and decode a Mini App token. Returns payload or None."""
    if not token or "." not in token:
        return None
    try:
        data, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            settings.JWT_SECRET.encode(), data.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(data + "=="))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None
