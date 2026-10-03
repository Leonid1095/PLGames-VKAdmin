"""Интервал автопостинга — тот, что в настройке, а не на час больше (03.10.2026).

Автопостинг проверяется раз в час, а `_last_autopost` пишется в конце сбора —
на секунды позже начала проверки. Через 6 часов следующая проверка видела
«прошло 5:59:48», пропускала группу, и пост уходил через 7 часов вместо 6:
новость PLGamesBot 03.10 ждала лишний час.
"""

from datetime import datetime, timedelta, timezone

from core.crypto import encrypt_token
from database.service import add_content_source, create_group, set_setting
from tasks import scheduler

GID = 240061584


async def _group(last_ago):
    await create_group(GID, "Bot", encrypt_token("t"), 309736634)
    await set_setting(GID, "autopost_enabled", "true")
    await set_setting(GID, "autopost_interval_hours", "6")
    await set_setting(GID, "_last_autopost",
                      (datetime.now(timezone.utc) - last_ago).isoformat())
    await add_content_source(GID, "api", "https://plgamesbot.ru/api/news")


async def _fetched(monkeypatch):
    asked = []

    async def _fetch(group_id):
        asked.append(group_id)
        return 0
    monkeypatch.setattr(scheduler, "fetch_and_schedule", _fetch)
    await scheduler._autopost_job()
    return asked


async def test_hourly_tick_seconds_short_of_interval_still_posts(db, monkeypatch):
    await _group(timedelta(hours=6) - timedelta(seconds=12))
    assert await _fetched(monkeypatch) == [GID]


async def test_interval_not_yet_passed_stays_silent(db, monkeypatch):
    await _group(timedelta(hours=5))
    assert await _fetched(monkeypatch) == []
