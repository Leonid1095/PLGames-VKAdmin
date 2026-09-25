"""Виджет «Топ участников»: токен не теряется, о поломке админ узнаёт сам.

Регрессия: при первой же ошибке VK 5/15/27 бот стирал widget_token, и
ежечасное обновление дальше молча писало в лог «Сначала нажмите «Установить
виджет»» — у 236517033 так было минимум с июля, админ каждый раз заново искал,
где взять токен.
"""

import json

import httpx
import pytest

import core.widgets as widgets
from core.crypto import encrypt_token
from database.service import add_xp, create_group, get_setting, set_setting

GID = 236517033
ADMIN = 309736634


def _mock_vk(monkeypatch, widget_update, seen=None):
    """users.get отвечает именами, appWidgets.update — ответом из widget_update().
    В seen (если передан) складываются запросы к appWidgets.update."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users.get"):
            return httpx.Response(200, json={"response": [
                {"id": 11, "first_name": "Иван", "last_name": "Петров"},
            ]})
        if request.url.path.endswith("/appWidgets.update"):
            if seen is not None:
                seen.append(request)
            return httpx.Response(200, json=widget_update())
        return httpx.Response(404)

    monkeypatch.setattr(
        widgets, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def notes(monkeypatch):
    """Сообщения админу (ЛС от группы) — перехватываем, в VK не шлём."""
    sent = []

    async def fake_notify(group_id, text):
        sent.append((group_id, text))
        return True

    monkeypatch.setattr(widgets, "_notify_admin", fake_notify)
    return sent


async def _group_with_widget(token="widget-token"):
    await create_group(GID, "WOW", encrypt_token("group-token"), ADMIN)
    await set_setting(GID, "widget_enabled", "true")
    await set_setting(GID, "widget_token", token)
    await add_xp(GID, 11, 10)


_REJECTED = {"error": {"error_code": 15, "error_msg": "Access denied"}}


async def test_vk_rejecting_widget_token_does_not_delete_it(db, monkeypatch, notes):
    await _group_with_widget()
    _mock_vk(monkeypatch, lambda: _REJECTED)

    ok, _ = await widgets.update_widget_for_group(GID)

    assert not ok
    assert await get_setting(GID, "widget_token") == "widget-token"
    assert "15" in await get_setting(GID, "widget_last_error")


async def test_rejected_token_alerts_admin_once_with_where_to_click(db, monkeypatch, notes):
    await _group_with_widget()
    _mock_vk(monkeypatch, lambda: _REJECTED)

    await widgets.update_widget_for_group(GID)
    await widgets.update_widget_for_group(GID)  # следующий час — не спамим

    assert len(notes) == 1
    assert "Установить виджет" in notes[0][1]
    assert "на компьютере" in notes[0][1]  # с телефона установка не проходит


async def test_missing_token_alerts_admin_once(db, monkeypatch, notes):
    await _group_with_widget(token="")

    await widgets.update_widget_for_group(GID)
    await widgets.update_widget_for_group(GID)

    assert len(notes) == 1
    assert "Установить виджет" in notes[0][1]


async def test_success_resets_alert_so_next_breakage_is_reported(db, monkeypatch, notes):
    await _group_with_widget()
    replies = [_REJECTED, {"response": 1}, _REJECTED]
    _mock_vk(monkeypatch, lambda: replies.pop(0))

    await widgets.update_widget_for_group(GID)
    ok, _ = await widgets.update_widget_for_group(GID)
    assert ok
    assert await get_setting(GID, "widget_last_error") == ""

    await widgets.update_widget_for_group(GID)
    assert len(notes) == 2


# ─── Вид виджета: понятная таблица (type=table) ──────────────────────────────

def _widget(code: str) -> dict:
    assert code.startswith("return ") and code.endswith(";")
    return json.loads(code[len("return "):-1])


def _row(vk_id, name, *, level=1, xp=10, messages=1):
    return {"vk_id": vk_id, "name": name, "level": level, "xp": xp,
            "messages": messages, "reputation": 0}


def test_table_first_cell_is_avatar_medal_and_name(monkeypatch):
    monkeypatch.setattr(widgets.settings, "VK_MINIAPP_ID", "54475361")
    w = _widget(widgets._build_table_widget_code([
        _row(11, "Иван Петров", level=2, xp=32, messages=15),
        _row(12, "Мария Кузнецова"), _row(13, "Олег Смирнов"), _row(14, "Анна Рыбакова"),
    ], GID))

    assert w["title"] == "🏆 Топ участников"
    assert w["title_url"] == f"https://vk.com/app54475361_-{GID}"
    assert w["head"] == [
        {"text": "Участник"},
        {"text": "Уровень", "align": "center"},
        {"text": "Опыт", "align": "center"},
        {"text": "Сообщения", "align": "center"},
    ]
    assert w["body"][0] == [
        {"text": "🥇 Иван Петров", "url": "https://vk.com/id11", "icon_id": "id11"},
        {"text": "2"}, {"text": "32"}, {"text": "15"},
    ]
    assert [r[0]["text"] for r in w["body"][1:]] == [
        "🥈 Мария Кузнецова", "🥉 Олег Смирнов", "4. Анна Рыбакова",
    ]


def test_table_shows_at_most_ten_people():
    """VK отклоняет table длиннее 10 строк (плюс строка заголовков)."""
    rows = [_row(i, f"U{i}") for i in range(1, 13)]
    assert len(_widget(widgets._build_table_widget_code(rows, GID))["body"]) == 10


async def test_update_sends_table_widget_to_vk(db, monkeypatch, notes):
    await _group_with_widget()
    await set_setting(GID, "widget_top_count", "20")
    seen = []
    _mock_vk(monkeypatch, lambda: {"response": 1}, seen)

    ok, msg = await widgets.update_widget_for_group(GID)

    assert ok
    assert msg == "Обновлено (1 участник)"
    params = seen[0].url.params
    assert params["type"] == "table"
    assert _widget(params["code"])["body"][0][0]["text"] == "🥇 Иван Петров"
