"""Текст ошибки ИИ не должен попадать на стену, в комментарии и приветствия.

_call_llm при сбое возвращает строку-заглушку («Извините, произошла ошибка…»,
«ИИ вернул пустой ответ.»), а вызывающие проверяли разные префиксы —
часть путей публиковала заглушку как пост/комментарий.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.crypto import encrypt_token
from core.text_guard import is_llm_failure, is_publishable
from database.service import create_group, create_scheduled_post
from database.engine import async_session
from database.models import ScheduledPost
from sqlalchemy import select

GID = 236517033

LLM_FAILURES = [
    "Извините, произошла ошибка при обращении к ИИ. Попробуйте позже.",
    "ИИ вернул пустой ответ.",
    "",
    None,
]


def test_llm_failure_sentinels_detected():
    for text in LLM_FAILURES:
        assert is_llm_failure(text), text
    assert not is_llm_failure("Привет! Новое обновление уже на сервере.")


def test_generated_post_must_be_real_content():
    for text in LLM_FAILURES:
        assert not is_publishable(text)
    assert not is_publishable("Нет коммитов за 7 дней")
    assert is_publishable("Обновление 12 июня: новые подземелья, исправления баланса и ивент выходного дня.")


async def test_scheduled_publisher_never_posts_llm_failure(db, monkeypatch):
    import tasks.scheduler as sched

    await create_group(GID, "WOW", encrypt_token("t"), 1)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    bad = await create_scheduled_post(GID, "ИИ вернул пустой ответ.", past, source="agent")
    good = await create_scheduled_post(GID, "Всем привет!", past, source="manual")

    posted = []

    class FakeWall:
        async def post(self, **kw):
            posted.append(kw["message"])
            return SimpleNamespace(post_id=100 + len(posted))

    monkeypatch.setattr(sched, "API", lambda token: SimpleNamespace(wall=FakeWall()))

    async def no_tg(*a, **kw):
        return False

    monkeypatch.setattr(sched, "send_to_telegram", no_tg)

    await sched._scheduled_posts_job()

    # Короткий ручной пост админа — легитимен; заглушка ИИ — нет.
    assert posted == ["Всем привет!"]
    async with async_session() as s:
        rows = {p.id: p for p in (await s.execute(select(ScheduledPost))).scalars()}
    assert rows[bad.id].status == "failed"
    assert rows[good.id].status == "published"


async def test_unknown_content_task_type_is_disabled_not_retried(db):
    """Задача patch_notes (путь удалён) гонялась каждые 30 мин: 567 warning'ов."""
    import tasks.scheduler as sched
    from database.service import create_content_task, get_all_active_content_tasks

    await create_group(GID, "WOW", encrypt_token("t"), 1)
    await create_content_task(GID, "patch_notes_WOW", "patch_notes", "0 18 * * 5")

    await sched._content_tasks_job()

    assert await get_all_active_content_tasks() == []


def test_daily_summary_runs_once_per_moscow_day_after_10():
    """Гейт «20 ч» при почасовом триггере сдвигал дайджест на 4 ч в сутки —
    он приходил и в 07:11, и в 00:11 по Москве."""
    from tasks.scheduler import _daily_summary_due

    def utc(d, h, m=0):
        return datetime(2026, 9, d, h, m, tzinfo=timezone.utc)

    # 09:00 МСК — рано
    assert not _daily_summary_due("", utc(23, 6))
    # 10:30 МСК, вчера уже было — пора
    assert _daily_summary_due(utc(22, 7, 10).isoformat(), utc(23, 7, 30))
    # 10:30 МСК, сегодня уже было — нет
    assert not _daily_summary_due(utc(23, 7, 5).isoformat(), utc(23, 7, 30))
    # Сервис лежал в 10:00 — догоняем в 15:00 МСК того же дня
    assert _daily_summary_due(utc(22, 7).isoformat(), utc(23, 12))
    # Мусор в настройке не блокирует дайджест навсегда
    assert _daily_summary_due("not-a-date", utc(23, 8))
