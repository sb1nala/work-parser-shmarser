import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from vacancy_bot.domain import WorkFormat
from vacancy_bot.sources.sber import SberSource

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sber_list.json").read_text("utf-8"))
ITEMS = FIXTURE["data"]["vacancies"]
URL = "https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1/publications"
AREAS = ["area-1", "area-2"]


def params(**kw):
    return {"prof_areas": AREAS, "take": 100, "max_pages": 10, **kw}


def page(items, total):
    return {"data": {"vacancies": items, "total": total}, "success": True}


def query(call):
    return parse_qs(call.request.url.query.decode())


async def run(source_params):
    async with httpx.AsyncClient() as http:
        return await SberSource(http, source_params, delay=0).fetch()


@respx.mock
async def test_maps_fixture():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    assert len(postings) == len(ITEMS)
    assert all(p.source == "sber" and p.company == "Сбер" for p in postings)

    p = postings[0]
    assert p.title == ITEMS[0]["title"] == "Data Analyst в управление комплаенс"
    assert p.external_id == str(ITEMS[0]["internalId"]) == "4587541"
    assert p.url == "https://rabota.sber.ru/search/4587541/"
    assert p.cities == ("Санкт-Петербург",)
    assert p.experience_years_min == 1  # "От 1 года до 3 лет"
    assert p.work_formats == frozenset()  # "Полный день" says nothing about format
    assert p.employment is None
    assert p.published_at.utcoffset() == timedelta(0)
    assert (p.published_at.year, p.published_at.month, p.published_at.day) == (2026, 11, 18)
    assert ITEMS[0]["introduction"] in p.description
    assert ITEMS[0]["requirements"] in p.description
    assert p.raw == ITEMS[0]

    q = query(route.calls[0])
    assert q["skip"] == ["0"] and q["take"] == ["100"]
    assert q["profAreas"] == AREAS


@respx.mock
async def test_no_invented_values():
    respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    intern, nizhny, flexible, remote = postings[1:]
    # the title says "Стажер" but no structured field does: employment stays unknown
    assert intern.employment is None
    assert intern.experience_years_min == 0
    assert nizhny.cities == ("Нижний Новгород",)
    # "Гибкий график" (flexible hours) is a schedule, not a work format
    assert flexible.work_formats == frozenset()
    assert remote.work_formats == frozenset({WorkFormat.REMOTE})
    assert remote.cities == ("Тула",)
    assert all(p.employment is None for p in postings)


@respx.mock
async def test_missing_optional_fields_stay_unknown():
    bare = {
        "internalId": 1,
        "title": "x",
        "city": "",
        "publicationDate": None,
        "introduction": None,
        "workScheduleId": 99,
        "experienceId": "unknown-id",
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([bare], 1)))
    (p,) = await run(params())
    assert p.cities == () and p.work_formats == frozenset()
    assert p.experience_years_min is None and p.employment is None
    assert p.published_at is None and p.description == ""


@respx.mock
async def test_paginates():
    first = page(ITEMS[:2], 5)
    second = page(ITEMS[2:4], 5)
    third = page(ITEMS[4:], 5)
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=r) for r in (first, second, third)]
    )
    postings = await run(params(take=2))
    assert len(postings) == 5
    assert [query(c)["skip"] for c in route.calls] == [["0"], ["2"], ["4"]]
    assert all(query(c)["take"] == ["2"] for c in route.calls)


@respx.mock
async def test_stops_when_total_reached_exactly():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 4)),
            httpx.Response(200, json=page(ITEMS[2:4], 4)),
        ]
    )
    assert len(await run(params(take=2))) == 4
    assert route.call_count == 2


@respx.mock
async def test_dedups_ids_across_pages():
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 4)),
            httpx.Response(200, json=page(ITEMS[1:3], 4)),
        ]
    )
    postings = await run(params(take=2))
    assert [p.external_id for p in postings] == [str(i["internalId"]) for i in ITEMS[:3]]


@respx.mock
async def test_raises_when_max_pages_exhausted():
    pages = [page(ITEMS[i : i + 1], 99) for i in range(3)]
    route = respx.get(URL).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with pytest.raises(RuntimeError, match="max_pages"):
        await run(params(take=1, max_pages=3))
    assert route.call_count == 3


@respx.mock
async def test_raises_on_empty_page_before_total():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 9)),
            httpx.Response(200, json=page([], 9)),
        ]
    )
    with pytest.raises(RuntimeError, match="empty page"):
        await run(params(take=2))
    assert route.call_count == 2


@respx.mock
async def test_raises_on_non_json_200():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>captcha</html>"))
    with pytest.raises(RuntimeError, match="malformed"):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"data": None, "success": True},
        {"data": {"vacancies": []}, "success": True},
        {"data": {"vacancies": [{"title": "x"}], "total": 1}, "success": True},
        {"data": {"vacancies": [], "total": "many"}, "success": True},
        {"data": {"vacancies": [], "total": 0}, "success": False},
        [1, 2],
    ],
)
async def test_raises_on_malformed_payload(body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="malformed"):
        await run(params())


@respx.mock
async def test_raises_on_http_error():
    first = page(ITEMS[:2], 5)
    respx.get(URL).mock(side_effect=[httpx.Response(200, json=first), httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(take=2))
