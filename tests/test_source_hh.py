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
