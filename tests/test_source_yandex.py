import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import Employment, WorkFormat
from vacancy_bot.sources.yandex import YandexSource, _map

FIXTURES = Path(__file__).parent / "fixtures"
LIST = json.loads((FIXTURES / "yandex_list.json").read_text("utf-8"))
DETAILS = json.loads((FIXTURES / "yandex_detail.json").read_text("utf-8"))
ITEMS = LIST["results"]
IDS = [i["id"] for i in ITEMS]
URL = "https://yandex.ru/jobs/api/publications"


def params(**kw):
    return {
        "professions": ["analyst", "ml-developer"],
        "pro_levels": ["intern", "junior"],
        "page_size": 40,
        "max_pages": 10,
        **kw,
    }


def page(items, cursor=None):
    nxt = f"/jobs/api/jobs/publications/?page_size=40&cursor={cursor}" if cursor else None
    body = {"count": 99, "results": items, "counters": {}}
    if nxt:
        body["next"] = nxt
    return body


def detail_routes():
    for key, body in DETAILS.items():
        respx.get(f"{URL}/{key}").mock(return_value=httpx.Response(200, json=body))


async def run(source_params):
    async with httpx.AsyncClient() as http:
        return await YandexSource(http, source_params, delay=0).fetch()


@respx.mock
async def test_maps_fixture():
    last_page = {k: v for k, v in LIST.items() if k != "next"}  # the real one had a next cursor
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=last_page))
    detail_routes()
    postings = await run(params())
    assert [p.external_id for p in postings] == [str(i) for i in IDS]
    assert all(p.source == "yandex" and p.company == "Яндекс" for p in postings)

    fin = postings[0]
    assert fin.title == "Финансовый аналитик в команду супераппа Яндекс Go"
    assert fin.url == f"https://yandex.ru/jobs/vacancies/{ITEMS[0]['publication_slug_url']}"
    assert fin.cities == ("Москва",)
    assert fin.work_formats == frozenset({WorkFormat.OFFICE})
    assert fin.employment is None
    assert fin.experience_years_min == 0  # junior..middle -> lower bound junior
    assert fin.published_at == datetime(2026, 11, 19, tzinfo=UTC)
    assert fin.description.startswith("В международные проекты Яндекс Такси")
    assert "<b>" not in fin.description and "<br" not in fin.description

    ds = postings[1]
    assert ds.work_formats == frozenset({WorkFormat.OFFICE, WorkFormat.HYBRID})
    assert ds.cities == ()  # none stated
    assert ds.experience_years_min == 1  # middle
    assert postings[2].experience_years_min == 3  # senior
    assert postings[4].experience_years_min == 0  # intern..middle
    ml = postings[4]  # real "Yandex Research ML Residency": minimum grade is intern
    assert ml.external_id == "1697" and ml.employment is Employment.INTERNSHIP
    assert all(p.employment is None for p in postings[:4])
    assert postings[4].work_formats == frozenset()  # no work_modes stated

    q = route.calls[0].request.url.params
    assert q.get_list("professions") == ["analyst", "ml-developer"]
    assert q.get_list("pro_levels") == ["intern", "junior"]
    assert q["page_size"] == "40"
    assert "cursor" not in q


@respx.mock
async def test_does_not_invent_values():
    bare = {
        "id": 7,
        "publication_slug_url": "x-7",
        "title": "Аналитик",
        "published_at": None,
        "vacancy": {"id": 1, "cities": [], "work_modes": []},
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([{"id": 7}])))
    respx.get(f"{URL}/7").mock(return_value=httpx.Response(200, json=bare))
    (p,) = await run(params())
    assert p.cities == ()
    assert p.work_formats == frozenset()
    assert p.employment is None
    assert p.experience_years_min is None
    assert p.published_at is None
    assert p.description == ""


def test_experience_mapping():
    def years(level):
        item = {
            "id": 1,
            "publication_slug_url": "x-1",
            "title": "x",
            "vacancy": {"pro_level_min_display": level},
        }
        return _map(item).experience_years_min

    assert years("publications.pro_level.intern") == 0
    assert years("publications.pro_level.junior") == 0
    assert years("publications.pro_level.middle") == 1
    assert years("publications.pro_level.senior") == 3
    assert years("publications.pro_level.chief") == 5
    assert years("publications.pro_level.unknown") is None
    assert years(None) is None


def test_internship_and_remote_flags():
    item = {
        "id": 1,
        "publication_slug_url": "x-1",
        "title": "x",
        "vacancy": {"employment_types": ["intern", "remote"], "work_modes": []},
    }
    p = _map(item)
    assert p.employment is Employment.INTERNSHIP
    assert p.work_formats == frozenset({WorkFormat.REMOTE})


def test_naive_date_is_utc():
    item = {"id": 1, "publication_slug_url": "x-1", "title": "x", "published_at": "2026-11-18"}
    item["vacancy"] = {}
    assert _map(item).published_at == datetime(2026, 11, 18, tzinfo=UTC)


def test_raw_is_an_allowlist_without_personal_data():
    item = {
        "id": 1,
        "publication_slug_url": "x-1",
        "title": "x",
        "vacancy": {"cities": [], "work_modes": []},
        "contacts": [{"name": "Иванов Иван", "phone": "+7 000", "email": "a@b.c"}],
        "form_url": "https://forms.yandex.ru/surveys/secret",
        "recruiter": "Пётр",
    }
    raw = _map(item).raw
    blob = json.dumps(raw, ensure_ascii=False)
    for secret in ("Иванов", "+7 000", "a@b.c", "secret", "Пётр"):
        assert secret not in blob
    assert raw["id"] == 1


@respx.mock
async def test_paginates_with_cursor():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], "AAA")),
            httpx.Response(200, json=page(ITEMS[2:])),
        ]
    )
    detail_routes()
    postings = await run(params())
    assert len(postings) == 5
    assert "cursor" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["cursor"] == "AAA"
    assert route.calls[1].request.url.params.get_list("pro_levels") == ["intern", "junior"]


@respx.mock
async def test_dedups_ids_across_pages():
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], "AAA")),
            httpx.Response(200, json=page(ITEMS[1:3])),
        ]
    )
    detail_routes()
    postings = await run(params())
    assert [p.external_id for p in postings] == [str(i) for i in IDS[:3]]
    # each publication's detail is fetched once
    assert respx.calls.call_count == 2 + 3


@respx.mock
async def test_raises_on_stuck_cursor():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:1], "AAA")),
            httpx.Response(200, json=page(ITEMS[1:2], "AAA")),
        ]
    )
    with pytest.raises(RuntimeError, match="cursor did not advance"):
        await run(params())
    assert route.call_count == 2


@respx.mock
async def test_raises_when_max_pages_exhausted():
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[i : i + 1], f"C{i}")) for i in range(3)]
    )
    with pytest.raises(RuntimeError, match="max_pages"):
        await run(params(max_pages=3))
    assert route.call_count == 3


@respx.mock
async def test_raises_on_empty_page_before_end():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([], "AAA")))
    with pytest.raises(RuntimeError, match="empty page"):
        await run(params())


@respx.mock
async def test_raises_on_non_json_200():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>captcha</html>"))
    with pytest.raises(RuntimeError, match="yandex: malformed"):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"count": 1},
        {"results": "x"},
        {"results": [{"title": "no id"}]},
        {"results": [{"id": "1"}]},
        {"results": [], "next": 5},
        {"results": [{"id": 1}], "next": "/jobs/api/jobs/publications/?page_size=40"},
        [1, 2],
    ],
)
async def test_raises_on_malformed_list(body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="yandex: malformed"):
        await run(params())


@respx.mock
async def test_raises_on_http_error_in_list():
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[:2], "AAA")), httpx.Response(503)]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
async def test_raises_on_http_error_in_detail():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:2])))
    respx.get(f"{URL}/{IDS[0]}").mock(return_value=httpx.Response(200, json=DETAILS[str(IDS[0])]))
    respx.get(f"{URL}/{IDS[1]}").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
async def test_detail_failure_fails_whole_fetch_not_partial():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:3])))
    respx.get(f"{URL}/{IDS[0]}").mock(return_value=httpx.Response(200, json=DETAILS[str(IDS[0])]))
    respx.get(f"{URL}/{IDS[1]}").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(RuntimeError, match=f"yandex: malformed publication {IDS[1]}"):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"id": 7},  # required keys missing
        {"id": 8, "publication_slug_url": "x", "title": "x", "vacancy": {}},  # id mismatch
        {
            "id": 7,
            "publication_slug_url": "x",
            "title": "x",
            "vacancy": {"cities": [{"slug": "a"}]},
        },
        {"id": 7, "publication_slug_url": "x", "title": "x", "vacancy": {}, "published_at": "nope"},
        {"id": 7, "publication_slug_url": "x", "title": "x", "vacancy": {"work_modes": "x"}},
        [1, 2],
    ],
)
async def test_raises_on_malformed_detail(body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([{"id": 7}])))
    respx.get(f"{URL}/7").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="yandex: malformed publication 7"):
        await run(params())


def test_employment_types_string_is_not_split():
    item = {
        "id": 1,
        "publication_slug_url": "x-1",
        "title": "x",
        "vacancy": {"employment_types": "intern remote"},
    }
    with pytest.raises(ValueError, match="employment_types"):
        _map(item)


@respx.mock
async def test_paces_requests(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("vacancy_bot.sources.yandex.asyncio.sleep", fake_sleep)
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], "AAA")),
            httpx.Response(200, json=page(ITEMS[2:3])),
        ]
    )
    detail_routes()
    async with httpx.AsyncClient() as http:
        await YandexSource(http, params(), delay=0.5).fetch()
    # one pause before the second list page, one before each of the 3 detail calls
    assert sleeps == [0.5] * 4


def test_empty_alert_after_hours_is_read_from_params():
    src = YandexSource(None, params(empty_alert_after_hours=720))  # type: ignore[arg-type]
    assert src.empty_alert_after_hours == 720
    assert YandexSource(None, params()).empty_alert_after_hours is None  # type: ignore[arg-type]
