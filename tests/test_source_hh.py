import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import Employment, WorkFormat
from vacancy_bot.sources.hh import HHSource

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hh_page.json").read_text("utf-8"))
URL = "https://api.hh.ru/vacancies"


def params(queries=None):
    return {
        "per_page": 100,
        "max_pages": 20,
        "queries": queries
        or [{"text": "аналитик", "search_field": "name", "experience": "noExperience"}],
    }


async def run(source_params, **kw):
    async with httpx.AsyncClient() as http:
        return await HHSource(http, "vacancy-bot-dev/0.1", source_params, delay=0, **kw).fetch()


@respx.mock
async def test_maps_fixture_items():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    assert len(postings) == len(FIXTURE["items"])
    assert all(p.source == "hh" for p in postings)
    p = postings[0]
    assert p.external_id == "100000001"
    assert p.url == "https://hh.ru/vacancy/100000001"
    assert p.company == "Пример Банк"
    assert p.title == "Стажер-аналитик данных"
    assert p.cities == ("Москва",)
    assert p.work_formats == frozenset({WorkFormat.REMOTE})
    assert p.employment == Employment.INTERNSHIP
    assert p.experience_years_min == 0
    assert p.description == "Знание SQL и Python.\nПодготовка отчетов и дашбордов."
    assert p.published_at.utcoffset() == timedelta(hours=3)
    assert p.published_at.year == 2026
    assert p.raw == FIXTURE["items"][0]
    q = route.calls[0].request.url.params
    assert q["search_field"] == "name" and q["per_page"] == "100" and q["page"] == "0"
    assert q["experience"] == "noExperience"
    # other mapping branches
    assert postings[2].employment == Employment.PROJECT
    assert postings[2].experience_years_min == 3
    assert postings[2].work_formats == frozenset({WorkFormat.OFFICE})
    assert postings[2].description == "Анализ данных."


@respx.mock
async def test_work_format_preferred_over_schedule():
    respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    assert postings[1].work_formats == frozenset({WorkFormat.HYBRID})


@respx.mock
async def test_dedups_ids_across_queries():
    respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params([{"text": "a", "search_field": "name"},
                                 {"text": "b", "search_field": "name"}]))
    assert len(postings) == len(FIXTURE["items"])


@respx.mock
async def test_paginates_until_pages():
    page = {**FIXTURE, "pages": 2}
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=page))
    await run(params([{"text": "a", "search_field": "name"},
                      {"text": "b", "search_field": "name"}]))
    assert route.call_count == 4
    assert [c.request.url.params["page"] for c in route.calls] == ["0", "1", "0", "1"]


@respx.mock
async def test_raises_on_http_error():
    page = {**FIXTURE, "pages": 2}
    respx.get(URL).mock(side_effect=[httpx.Response(200, json=page), httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
async def test_sends_hh_user_agent():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    await run(params())
    assert route.calls[0].request.headers["HH-User-Agent"] == "vacancy-bot-dev/0.1"


@respx.mock
async def test_access_token_sends_bearer_header():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    await run(params(), access_token="secret")
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@respx.mock
async def test_no_authorization_header_without_token():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    await run(params())
    assert "Authorization" not in route.calls[0].request.headers


def item(id_="1", **kw):
    base = {
        "id": id_,
        "name": "Стажер-аналитик",
        "alternate_url": f"https://hh.ru/vacancy/{id_}",
    }
    return {**base, **kw}


def page_of(items, found=None, pages=1):
    return {"items": items, "found": len(items) if found is None else found, "pages": pages}


@respx.mock
async def test_raises_when_found_exceeds_hh_cap():
    # hh never returns more than 2000 results: 20 pages of 100, found 2500 -> truncated
    respx.get(URL).mock(return_value=httpx.Response(200, json=page_of([item()], 2500, 20)))
    with pytest.raises(RuntimeError, match="2500"):
        await run(params())


@respx.mock
async def test_raises_when_cap_hit_on_later_query():
    respx.get(URL).mock(side_effect=[
        httpx.Response(200, json=page_of([item("1")])),
        httpx.Response(200, json=page_of([item("2")], 2001, 20)),
    ])
    with pytest.raises(RuntimeError, match="'b'"):
        await run(params([{"text": "a"}, {"text": "b"}]))


@respx.mock
async def test_raises_when_max_pages_exhausted():
    route = respx.get(URL).mock(
        return_value=httpx.Response(200, json=page_of([item()], 500, 5))
    )
    with pytest.raises(RuntimeError, match="max_pages"):
        await run({**params(), "max_pages": 3})
    assert route.call_count == 1  # fails fast, before fetching pages it would drop


@respx.mock
async def test_all_pages_within_max_pages_is_fine():
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(200, json=page_of([item("1")], 150, 2)),
        httpx.Response(200, json=page_of([item("2")], 150, 2)),
    ])
    postings = await run({**params(), "per_page": 100, "max_pages": 2})
    assert {p.external_id for p in postings} == {"1", "2"}
    assert route.call_count == 2


@respx.mock
async def test_sends_area_remote_and_order_params():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=page_of([])))
    await run(params([
        {"text": "a", "area": [1, 1783], "order_by": "publication_time"},
        {"text": "b", "work_format": "REMOTE", "search_field": "description"},
    ]))
    first, second = (c.request.url.params for c in route.calls)
    assert first.get_list("area") == ["1", "1783"]
    assert first["order_by"] == "publication_time"
    assert first["search_field"] == "name"  # default
    assert "work_format" not in first
    assert second["work_format"] == "REMOTE"
    assert second["search_field"] == "description"
    assert "area" not in second


async def test_sleeps_between_pages_and_queries(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("vacancy_bot.sources.hh.asyncio.sleep", fake_sleep)
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, json=page_of([item()], 150, 2)))
        async with httpx.AsyncClient() as http:
            await HHSource(
                http, "ua", params([{"text": "a"}, {"text": "b"}]), delay=0.7
            ).fetch()
    # 4 requests (2 queries x 2 pages) -> a pause before each but the first
    assert sleeps == [0.7, 0.7, 0.7]


@respx.mock
async def test_raises_on_non_json_200():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>captcha</html>"))
    with pytest.raises(RuntimeError, match="hh: malformed") as exc:
        await run(params())
    assert exc.value.__cause__ is not None


@pytest.mark.parametrize("body", [
    page_of([{"name": "без id"}]),
    page_of([item(area={"id": "1"})]),
    page_of([item(published_at="вчера")]),
    {"items": [], "pages": 1},
    {"found": 0, "pages": 1},
    {"items": None, "found": 0, "pages": 1},
    [],
])
@respx.mock
async def test_raises_runtime_error_on_malformed_payload(body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="hh: malformed") as exc:
        await run(params())
    assert exc.value.__cause__ is not None


@respx.mock
async def test_naive_published_at_is_treated_as_utc():
    respx.get(URL).mock(return_value=httpx.Response(
        200, json=page_of([item(published_at="2026-11-18T10:00:00")])
    ))
    [p] = await run(params())
    assert p.published_at.utcoffset() == timedelta(0)
    assert p.published_at.hour == 10


@pytest.mark.parametrize(("raw", "expected"), [
    ([{"id": "REMOTE"}], {WorkFormat.REMOTE}),
    ([{"id": "ON_SITE"}], {WorkFormat.OFFICE}),
    ([{"id": "FIELD_WORK"}], {WorkFormat.OFFICE}),
    ([{"id": "REMOTE"}, {"id": "HYBRID"}], {WorkFormat.REMOTE, WorkFormat.HYBRID}),
    ([{"id": "UNKNOWN"}], set()),
])
@respx.mock
async def test_work_format_mapping(raw, expected):
    respx.get(URL).mock(return_value=httpx.Response(200, json=page_of([item(work_format=raw)])))
    [p] = await run(params())
    assert p.work_formats == frozenset(expected)


@pytest.mark.parametrize(("schedule", "expected"), [
    ({"id": "remote"}, {WorkFormat.REMOTE}),
    ({"id": "flexible"}, {WorkFormat.OFFICE}),
    ({}, set()),
    (None, set()),
])
@respx.mock
async def test_schedule_fallback_mapping(schedule, expected):
    respx.get(URL).mock(return_value=httpx.Response(200, json=page_of([item(schedule=schedule)])))
    [p] = await run(params())
    assert p.work_formats == frozenset(expected)


@pytest.mark.parametrize(("raw", "expected"), [
    ("full", Employment.FULL),
    ("part", Employment.PART),
    ("probation", Employment.INTERNSHIP),
    ("project", Employment.PROJECT),
    ("volunteer", Employment.OTHER),
    ("unknown", None),
])
@respx.mock
async def test_employment_mapping(raw, expected):
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=page_of([item(employment={"id": raw})]))
    )
    [p] = await run(params())
    assert p.employment == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("noExperience", 0),
    ("between1And3", 1),
    ("between3And6", 3),
    ("moreThan6", 6),
    ("unknown", None),
])
@respx.mock
async def test_experience_mapping(raw, expected):
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=page_of([item(experience={"id": raw})]))
    )
    [p] = await run(params())
    assert p.experience_years_min == expected


@respx.mock
async def test_missing_optional_fields_stay_unknown():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page_of([item()])))
    [p] = await run(params())
    assert p.company == "" and p.cities == () and p.work_formats == frozenset()
    assert p.employment is None and p.experience_years_min is None
    assert p.description == "" and p.published_at is None
