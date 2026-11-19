from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from vacancy_bot.domain import WorkFormat
from vacancy_bot.sources.telegram import TelegramSource

PAGE = (Path(__file__).parent / "fixtures" / "tg_newdirections_page.html").read_text("utf-8")
URL = "https://t.me/s/newdirections"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def params(**kw):
    return {"window_days": 14, "max_pages": 10, "require_level_keyword": True, **kw}


def post_html(number, when, text="Должность / Компания"):
    body = f'<div class="tgme_widget_message_text">{text}</div>' if text is not None else ""
    return (
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" '
        f'data-post="newdirections/{number}">{body}<a class="tgme_widget_message_date">'
        f'<time datetime="{when}">x</time></a></div></div>'
    )


def html(*posts):
    return "<html><body>" + "".join(posts) + "</body></html>"


def query(call):
    return parse_qs(call.request.url.query.decode())


def mock_then_end(page):
    """The page, then an empty page (end of history) for any further request."""
    return respx.get(URL).mock(
        side_effect=[httpx.Response(200, text=page), httpx.Response(200, text=html())]
    )


async def run(source_params=None, channel="newdirections", now=NOW):
    async with httpx.AsyncClient() as http:
        source = TelegramSource(
            http, channel, source_params or params(), delay=0, now=lambda: now
        )
        return await source.fetch()


@respx.mock
async def test_maps_structured_post():
    mock_then_end(PAGE)
    postings = {p.external_id: p for p in await run()}
    p = postings["7154"]
    assert p.title == "Помощник менеджера проектов"
    assert p.company == "Бельведер"
    assert p.cities == ("Владивосток",)
    assert p.work_formats == frozenset()
    assert p.url == "https://t.me/newdirections/7154"
    assert p.source == "tg_newdirections"
    assert p.published_at == datetime(2026, 11, 14, 11, 0, 31, tzinfo=UTC)
    assert p.published_at.tzinfo is not None
    assert p.experience_years_min is None
    assert p.employment is None
    assert p.require_level_keyword is True
    assert p.description.startswith("🗺 Владивосток.")
    assert "Помощник менеджера проектов" not in p.description
    assert p.raw["channel"] == "newdirections" and p.raw["post"] == "7154"
    assert p.raw["text"].startswith("Помощник менеджера проектов / Бельведер")

    remote = postings["7165"]  # only the first " / " splits
    assert remote.title == "Junior AI Product Manager"
    assert remote.company == "AI-продакт / Changellenge"
    assert remote.cities == ("Москва",)
    assert remote.work_formats == frozenset({WorkFormat.REMOTE})


@respx.mock
async def test_company_defaults_to_channel():
    mock_then_end(PAGE)
    postings = {p.external_id: p for p in await run()}
    p = postings["7158"]
    assert p.company == "Telegram @newdirections"
    assert p.title.startswith("Ищем Серчера вакансий!")
    assert p.cities == ()


@respx.mock
async def test_skips_posts_without_text():
    mock_then_end(PAGE)
    ids = {p.external_id for p in await run()}
    assert "7157" not in ids  # media only
    assert ids == {"7154", "7156", "7158", "7161", "7165"}


@respx.mock
async def test_param_flag_is_passed_through():
    mock_then_end(PAGE)
    postings = await run(params(require_level_keyword=False))
    assert all(p.require_level_keyword is False for p in postings)


@respx.mock
async def test_paginates_with_before_until_window():
    first = html(
        post_html(110, "2026-11-18T10:00:00+00:00"),
        post_html(105, "2026-11-17T10:00:00+00:00"),
        post_html(108, "2026-11-17T12:00:00+00:00"),
    )
    second = html(
        post_html(100, "2026-11-01T10:00:00+00:00", "Старый / Пост"),
        post_html(104, "2026-11-16T10:00:00+00:00"),
    )
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, text=first), httpx.Response(200, text=second)]
    )
    postings = await run()
    assert route.call_count == 2
    assert "before" not in query(route.calls[0])
    assert query(route.calls[1])["before"] == ["105"]
    assert {p.external_id for p in postings} == {"110", "105", "108", "104"}


@respx.mock
async def test_dedups_posts_across_pages():
    first = html(post_html(10, "2026-11-18T10:00:00+00:00", "Первый / A"))
    second = html(
        post_html(10, "2026-11-18T10:00:00+00:00", "Второй / B"),
        post_html(9, "2026-01-01T10:00:00+00:00"),
    )
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, text=first), httpx.Response(200, text=second)]
    )
    postings = await run()
    assert [p.external_id for p in postings] == ["10"]
    assert postings[0].title == "Первый"


@respx.mock
async def test_all_posts_older_than_window_returns_empty():
    old = html(post_html(5, "2026-01-01T10:00:00+00:00"), post_html(4, "2026-01-01T09:00:00"))
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=old))
    assert await run() == []
    assert route.call_count == 1


@respx.mock
async def test_empty_page_after_first_is_end_of_history():
    first = html(post_html(10, "2026-11-18T10:00:00+00:00"))
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, text=first), httpx.Response(200, text=html())]
    )
    assert [p.external_id for p in await run()] == ["10"]


@respx.mock
async def test_naive_date_is_utc():
    mock_then_end(html(post_html(7, "2026-11-18T10:00:00")))
    (p,) = await run()
    assert p.published_at == datetime(2026, 11, 18, 10, tzinfo=UTC)


@respx.mock
async def test_page_without_posts_container_raises():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html><body>nope</body></html>"))
    with pytest.raises(RuntimeError, match="newdirections"):
        await run()


@respx.mock
async def test_raises_on_http_error():
    respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(RuntimeError, match="tg_newdirections") as err:
        await run()
    assert isinstance(err.value.__cause__, httpx.HTTPError)


@respx.mock
async def test_raises_on_bad_date():
    respx.get(URL).mock(
        return_value=httpx.Response(200, text=html(post_html(7, "not-a-date")))
    )
    with pytest.raises(RuntimeError, match="newdirections"):
        await run()


@respx.mock
async def test_raises_when_max_pages_exhausted():
    pages = [
        httpx.Response(200, text=html(post_html(n, "2026-11-18T10:00:00+00:00")))
        for n in (30, 20, 10)
    ]
    route = respx.get(URL).mock(side_effect=pages)
    with pytest.raises(RuntimeError, match="max_pages"):
        await run(params(max_pages=3))
    assert route.call_count == 3
