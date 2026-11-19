from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from vacancy_bot.config import load_config
from vacancy_bot.domain import WorkFormat
from vacancy_bot.filtering import evaluate
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

    remote = postings["7165"]  # only the last " / " splits
    assert remote.title == "Junior AI Product Manager / AI-продакт"
    assert remote.company == "Changellenge"
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
    plain = {i for i in ids if not i.startswith("http")}
    assert plain == {"7154", "7156", "7158", "7161", "7165", "7162"}
    assert len(ids - plain) == 9  # 7169 is a digest: one posting per item, keyed by its link


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


@respx.mock
async def test_reply_uses_own_text_and_skips_media_only_reply():
    mock_then_end(PAGE)
    postings = {p.external_id: p for p in await run()}
    reply = postings["7162"]
    assert reply.title == "Ответ: Junior Dev"  # not the quoted "Стажёр аналитик"
    assert reply.company == "Своя Компания"
    assert "Цитата" not in reply.raw["text"]
    assert "7163" not in postings  # media-only reply: only the quote has text


async def fetch_one(text, when="2026-11-18T10:00:00+00:00"):
    with respx.mock:
        mock_then_end(html(post_html(1, when, text)))
        (p,) = await run()
    return p


@pytest.mark.parametrize("sep", [" / ", " — ", " | "])
async def test_title_company_separators(sep):
    p = await fetch_one(f"Junior Dev{sep}Acme")
    assert (p.title, p.company) == ("Junior Dev", "Acme")


@pytest.mark.parametrize("text", ["Junior Dev / ", "/ Acme", "Junior Dev |", "A/B tester"])
async def test_no_split_when_a_side_is_empty(text):
    p = await fetch_one(text)
    assert p.company == "Telegram @newdirections"


async def test_variation_selector_stripped_from_title_and_city():
    p = await fetch_one("⚡\ufe0f Junior Dev / Acme\ufe0f<br>🗺\ufe0f Москва.")
    assert (p.title, p.company) == ("Junior Dev", "Acme")
    assert p.cities == ("Москва",)


@pytest.mark.parametrize(
    ("line", "cities", "formats"),
    [
        ("📍 Казань, гибрид", ("Казань",), {WorkFormat.HYBRID}),
        ("🗺 Berlin. Hybrid", ("Berlin",), {WorkFormat.HYBRID}),
        ("🗺 Омск. Офис", ("Омск",), {WorkFormat.OFFICE}),
        ("📍 Tbilisi, Office", ("Tbilisi",), {WorkFormat.OFFICE}),
        ("🗺 Тверь. Удаленка", ("Тверь",), {WorkFormat.REMOTE}),
        ("📍 Remote", (), {WorkFormat.REMOTE}),
    ],
)
async def test_city_line_markers_and_formats(line, cities, formats):
    p = await fetch_one(f"Junior Dev<br>{line}<br>Текст")
    assert p.cities == cities
    assert p.work_formats == frozenset(formats)


async def test_br_becomes_newline():
    p = await fetch_one("Junior Dev / Acme<br>строка 1<br><br>строка 2")
    assert p.description == "строка 1\n\nстрока 2"
    assert p.raw["text"] == "Junior Dev / Acme\nстрока 1\n\nстрока 2"


@pytest.mark.parametrize(
    ("text", "title", "company"),
    [
        ("Стажер LLM / AI-агенты / ПАО Сбербанк", "Стажер LLM / AI-агенты", "ПАО Сбербанк"),
        ("Стажёр / менеджер проекта / Компания", "Стажёр / менеджер проекта", "Компания"),
        (
            "UX/UI дизайнер (Web / Mobile) / MULTIBROKER",
            "UX/UI дизайнер (Web / Mobile)",
            "MULTIBROKER",
        ),
        ("Role | A / B — C", "Role | A", "B — C"),  # " / " wins over the other separators
        ("Role — Acme | Beta", "Role", "Acme | Beta"),  # first occurrence of " — " / " | "
    ],
)
async def test_split_on_last_slash_separator(text, title, company):
    p = await fetch_one(text)
    assert (p.title, p.company) == (title, company)


async def test_labelled_post():
    p = await fetch_one(
        "Позиция: Стажер дата-аналитик<br>Компания: Х5 Group<br>"
        "Локация: Удаленная работа (…г. Москва)<br><br>Задачи: считать"
    )
    assert p.title == "Стажер дата-аналитик"
    assert p.company == "Х5 Group"
    assert p.work_formats == frozenset({WorkFormat.REMOTE})
    assert p.cities == ("Москва",)
    assert p.description == "Задачи: считать"


async def test_labels_override_heuristics_and_ignore_bullets_and_case():
    p = await fetch_one(
        "Что-то / Другое<br>🗺 Омск<br>• ДОЛЖНОСТЬ: Junior Dev<br>"
        "🏢 компания: Acme<br>Город: Казань. Гибрид"
    )
    assert (p.title, p.company) == ("Junior Dev", "Acme")
    assert p.cities == ("Казань",)
    assert p.work_formats == frozenset({WorkFormat.HYBRID})
    assert "Acme" not in p.description and "Junior Dev" not in p.description


@respx.mock
async def test_first_page_with_posts_but_no_text_raises():
    textless = html(post_html(2, "2026-11-18T10:00:00+00:00", None),
                    post_html(1, "2026-11-18T09:00:00+00:00", None))
    respx.get(URL).mock(return_value=httpx.Response(200, text=textless))
    with pytest.raises(RuntimeError, match="layout"):
        await run()


@respx.mock
async def test_later_textless_page_is_fine():
    first = html(post_html(10, "2026-11-18T10:00:00+00:00"))
    second = html(post_html(9, "2026-11-18T09:00:00+00:00", None),
                  post_html(8, "2026-01-01T09:00:00+00:00", None))
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, text=first), httpx.Response(200, text=second)]
    )
    assert [p.external_id for p in await run()] == ["10"]


async def test_empty_alert_threshold_comes_from_config():
    async with httpx.AsyncClient() as http:
        quiet = TelegramSource(http, "x", params(empty_alert_after_hours=720))
        default = TelegramSource(http, "x", params())
    assert quiet.empty_alert_after_hours == 720
    assert default.empty_alert_after_hours is None


# --- label scope ---


async def test_labels_only_count_in_leading_block():
    p = await fetch_one(
        "Junior Dev / Acme<br>🗺 Москва.<br><br>О нас<br>Компания: мы быстро растём<br>"
        "Город: любой"
    )
    assert (p.title, p.company) == ("Junior Dev", "Acme")
    assert p.cities == ("Москва",)
    assert "Компания: мы быстро растём" in p.description


async def test_labels_beyond_sixth_line_are_ignored():
    lines = "<br>".join(["Junior Dev / Acme"] + [f"строка {i}" for i in range(5)] + ["Компания: Evil"])
    p = await fetch_one(lines)
    assert p.company == "Acme"


# --- digests ---

CONFIG = load_config(Path("config/config.yaml"))
LINK = "https://youngjunior.ru/data/jobs/{}?utm_source=telegram&amp;amp;utm_medium=x&amp;amp;utm_campaign=digest"


def item(n, text, extra=""):
    return f'- <a href="{LINK.format(n)}">{text}</a>{extra}'


def job_url(n):
    return f"https://youngjunior.ru/data/jobs/{n}"


def by_item(postings):
    """Digest postings keyed "<post>:<item index>" (their external_id is the item URL)."""
    return {f"{p.raw['post']}:{p.raw['item']}": p for p in postings if "item" in p.raw}


def digest_html(*items, header="<b>Подборка вакансий: Data</b>"):
    footer = '<br/><br/><a href="https://t.me/+abc">Вакансии</a> │ <a href="https://t.me/+d">Ещё</a>'
    return header + "<br/><br/><b><i>Раздел:</i></b><br/>" + "<br/>".join(items) + footer


@respx.mock
async def test_digest_fixture_items_become_postings():
    mock_then_end(PAGE)
    postings = await run()
    assert "7169" not in {p.external_id for p in postings}  # the digest itself is not a posting
    assert not any("Подборка" in p.title for p in postings)
    by_id = by_item(postings)
    assert len(by_id) == 9
    first = by_id["7169:1"]
    assert first.external_id == "https://youngjunior.ru/data/jobs/113647"
    assert first.title == "Стажёр в Центр практического ИИ"
    assert first.company == "ПАО Сбербанк"
    assert first.url == "https://youngjunior.ru/data/jobs/113647"  # own link, utm stripped
    assert first.published_at == datetime(2026, 11, 18, 15, 0, 37, tzinfo=UTC)
    assert first.source == "tg_newdirections" and first.require_level_keyword is True
    assert first.raw["item"] == 1 and first.raw["post"] == "7169"
    assert first.raw["text"] == "- Стажёр в Центр практического ИИ в ПАО Сбербанк"
    abroad = by_id["7169:9"]  # "(Узбекистан)" after the link
    assert (abroad.title, abroad.company) == ("IT Project Manager", "ИП ООО Xalq Retail")
    assert abroad.cities == ("Узбекистан",)
    assert by_id["7169:6"].company == "Альфа-Банк: IT-направление и цифровой бизнес"


async def test_digest_item_forms():
    text = digest_html(
        item(1, "Data Scientist (Deep Learning) | Junior в Бюро кредитных историй"),
        item(2, "Графический дизайнер / Бренд дизайнер в ФотоЛайф"),
        item(3, "Аналитик в Acme (удалённо)"),
        item(4, "Junior Dev в Acme", " (удалённо)"),
        item(5, "Junior Dev / Beta Corp"),  # no " в ": title rules, last " / "
        item(6, "Стажёр"),  # no company: channel default
    )
    with respx.mock:
        mock_then_end(html(post_html(50, "2026-11-18T10:00:00+00:00", text)))
        ps = by_item(await run())
    assert len(ps) == 6
    a = ps["50:1"]
    assert (a.title, a.company) == ("Data Scientist (Deep Learning) | Junior", "Бюро кредитных историй")
    assert a.url == a.external_id == job_url(1) and a.work_formats == frozenset()
    assert (ps["50:2"].title, ps["50:2"].company) == (
        "Графический дизайнер / Бренд дизайнер", "ФотоЛайф")
    assert (ps["50:3"].title, ps["50:3"].company) == ("Аналитик", "Acme")
    assert ps["50:3"].work_formats == frozenset({WorkFormat.REMOTE})
    assert ps["50:4"].work_formats == frozenset({WorkFormat.REMOTE})
    assert ps["50:4"].company == "Acme" and ps["50:4"].cities == ()
    assert (ps["50:5"].title, ps["50:5"].company) == ("Junior Dev", "Beta Corp")
    assert ps["50:6"].company == "Telegram @newdirections"


async def test_digest_detected_by_item_count_without_header():
    body = "Свежие вакансии<br/>" + "<br/>".join(
        item(n, f"Стажёр {n} в Acme") for n in range(1, 4)
    )
    with respx.mock:
        mock_then_end(html(post_html(60, "2026-11-18T10:00:00+00:00", body)))
        ps = await run()
    assert [p.external_id for p in ps] == [job_url(1), job_url(2), job_url(3)]


async def test_digest_header_without_items_gives_nothing():
    with respx.mock:
        mock_then_end(html(post_html(61, "2026-11-18T10:00:00+00:00", "Подборка вакансий скоро")))
        assert await run() == []


async def test_bullet_lists_without_links_are_not_digests():
    body = "Junior Dev / Acme<br/>- делать раз<br/>- делать два<br/>- делать три"
    p = await fetch_one(body)
    assert p.external_id == "1" and p.title == "Junior Dev"


async def test_digest_items_pass_or_fail_the_filter():
    body = digest_html(item(1, "Стажёр продуктовый аналитик в Ozon"), item(2, "Product Designer в X"),
                       item(3, "Junior Data Analyst в Y"))
    with respx.mock:
        mock_then_end(html(post_html(70, "2026-11-18T10:00:00+00:00", body)))
        ps = by_item(await run())
    verdict = {k: evaluate(p, CONFIG.criteria) for k, p in ps.items()}
    assert verdict["70:1"].accepted and verdict["70:3"].accepted
    assert not verdict["70:2"].accepted


@respx.mock
async def test_digest_items_dedup_across_pages():
    digest = post_html(80, "2026-11-18T10:00:00+00:00", digest_html(
        item(1, "Стажёр A в X"), item(2, "Стажёр B в X"), item(3, "Стажёр C в X")))
    first = html(digest)
    second = html(digest, post_html(79, "2026-01-01T10:00:00+00:00"))
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, text=first), httpx.Response(200, text=second)]
    )
    ps = await run()
    assert [p.external_id for p in ps] == [job_url(1), job_url(2), job_url(3)]


@respx.mock
async def test_same_item_link_in_two_digests_is_one_posting():
    def digest(number, utm, link=None):
        href = link or f"https://YoungJunior.ru/data/jobs/7/?utm_source=a&amp;amp;utm_medium={utm}&amp;amp;fbclid=z#top"
        items = [f'- <a href="{href}">Стажёр аналитик в Ozon</a>'] + [
            item(n, f"Стажёр {n} в X") for n in (number, number + 1)
        ]
        return post_html(number, "2026-11-18T10:00:00+00:00", digest_html(*items))

    mock_then_end(html(digest(20, "one"), digest(10, "two")))
    ps = await run()
    ozon = [p for p in ps if p.company == "Ozon"]
    assert len(ozon) == 1
    assert ozon[0].external_id == ozon[0].url == "https://youngjunior.ru/data/jobs/7"
    assert ozon[0].raw["post"] == "20"  # first occurrence wins
    assert len(ps) == 5  # 1 shared + 2 + 2 distinct items


async def test_item_without_link_keeps_post_and_index_id():
    body = (
        "<b>Подборка вакансий</b><br/>"
        + item(1, "Стажёр A в Acme")
        + "<br/>- Стажёр Б в Beta<br/>"
        + item(2, "Стажёр C в Gamma")
    )
    with respx.mock:
        mock_then_end(html(post_html(90, "2026-11-18T10:00:00+00:00", body)))
        ps = await run()
    assert [p.external_id for p in ps] == [job_url(1), "90:2", job_url(2)]
    linkless = ps[1]
    assert (linkless.title, linkless.company) == ("Стажёр Б", "Beta")
    assert linkless.url == "https://t.me/newdirections/90"
