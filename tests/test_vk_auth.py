"""Подпись параметров запуска Mini App: верная — принимаем, протухшая/чужая — нет."""

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import core.vk_auth as vk_auth
from core.config import settings


def _signed(params: dict, secret: str) -> dict:
    vk = {k: v for k, v in sorted(params.items()) if k.startswith("vk_")}
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), urlencode(vk).encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return {**params, "sign": sig}


def _setup(monkeypatch):
    monkeypatch.setattr(settings, "VK_MINIAPP_SECRET", "mini-secret")
    monkeypatch.setattr(settings, "VK_MINIAPP_ID", "555")
    monkeypatch.setattr(settings, "VK_APP_ID", "444")
    monkeypatch.setattr(settings, "VK_APP_SECRET", "")


def test_fresh_valid_params_accepted(monkeypatch):
    _setup(monkeypatch)
    p = _signed({"vk_user_id": "1", "vk_app_id": "555", "vk_group_id": "7",
                 "vk_ts": str(int(time.time()))}, "mini-secret")
    got = vk_auth.verify_vk_launch_params(p)
    assert got and got.vk_user_id == 1 and got.vk_group_id == 7


def test_stale_params_rejected(monkeypatch):
    """Подписанный URL из журнала nginx не должен давать сессию вечно."""
    _setup(monkeypatch)
    old = int(time.time()) - 2 * 86400
    p = _signed({"vk_user_id": "1", "vk_app_id": "555", "vk_ts": str(old)}, "mini-secret")
    assert vk_auth.verify_vk_launch_params(p) is None


def test_foreign_app_rejected(monkeypatch):
    _setup(monkeypatch)
    p = _signed({"vk_user_id": "1", "vk_app_id": "999",
                 "vk_ts": str(int(time.time()))}, "mini-secret")
    assert vk_auth.verify_vk_launch_params(p) is None


def test_bad_signature_rejected(monkeypatch):
    _setup(monkeypatch)
    p = _signed({"vk_user_id": "1", "vk_app_id": "555",
                 "vk_ts": str(int(time.time()))}, "wrong")
    assert vk_auth.verify_vk_launch_params(p) is None
