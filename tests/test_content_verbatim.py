"""Свои новости — на стену как есть, без пересказа моделью (03.10.2026).

Первый пост из ленты PLGamesBot модель «пересказала своими словами» и
приписала новости то, чего в ней нет: «одно уведомление, даже если вы уже
запустили плеер», «вы получаете сообщение «добавлена в очередь»» (его получает
зритель), «снижает стресс». Новости продукта пишутся и сверяются с кодом в
самом продукте — группе с `content_verbatim=true` они уходят дословно.
"""

from datetime import datetime, timezone

import httpx
from sqlalchemy import select

import core.web_reader as web_reader
from database.engine import async_session
from database.models import ScheduledPost
from database.service import add_content_source, set_setting
from tasks import content_parser as cp

GID = 240061584
TEXT = ("Раньше, если плеер музыки не был открыт, бот отказывал зрителю. "
        "Теперь заказ принимается всегда.\n\n• Трек ждёт в очереди.\n" + "Подробности. " * 60)
ITEM = {"title": "Заказ музыки: без плеера заказы ждут в очереди", "text": TEXT,
        "link": "https://plgamesbot.ru/zakaz-muzyki-na-strime?ref=vk", "image_url": "",
        "date": datetime.now(timezone.utc)}


async def _source(monkeypatch, verbatim):
    await add_content_source(GID, "api", "https://plgamesbot.ru/api/news")
    if verbatim:
        await set_setting(GID, "content_verbatim", "true")

    async def _api(url):
        return [dict(ITEM)]
    monkeypatch.setattr(cp, "parse_api", _api)


async def _posts():
    async with async_session() as s:
        return (await s.execute(select(ScheduledPost))).scalars().all()


async def test_verbatim_source_posts_text_as_is(db, monkeypatch):
    await _source(monkeypatch, verbatim=True)

    async def _no_model(**kw):
        raise AssertionError("свою новость модель не пересказывает")

    async def _no_page(url):
        raise AssertionError("страницу по ссылке вместо текста не берём")
    monkeypatch.setattr(cp, "write_from_source", _no_model)
    monkeypatch.setattr(cp, "read_url", _no_page)

    assert await cp.fetch_and_schedule(GID) == 1
    [post] = await _posts()
    assert post.text == f"{ITEM['title']}\n\n{TEXT.strip()}\n\n{ITEM['link']}"


async def test_other_groups_still_rewritten(db, monkeypatch):
    await _source(monkeypatch, verbatim=False)
    asked = []

    async def _model(**kw):
        asked.append(kw["source_material"])
        return "Пересказ новости моделью — так, как было для всех групп и раньше. " * 2
    monkeypatch.setattr(cp, "write_from_source", _model)

    assert await cp.fetch_and_schedule(GID) == 1
    assert asked and ITEM["title"] in asked[0]


async def test_api_text_is_not_cut_to_500(monkeypatch):
    """parse_api резал текст до 500 знаков: дословный пост обрывался бы на
    полуслове. Хэш новости берёт первые 200 знаков — уже отмеченные новости
    других групп от этого не «оживут»."""
    long_text = "Слово " * 400                     # 2400 знаков
    monkeypatch.setattr(
        web_reader, "_http_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"items": [{"title": "T", "text": long_text}]})),
            timeout=timeout))
    [item] = await cp.parse_api("http://93.184.216.34/api/news")
    assert item["text"] == long_text.strip()
