import json
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import Employment, WorkFormat
from vacancy_bot.sources.tbank import TBankSource

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tbank_list.json").read_text("utf-8"))
ITEMS = FIXTURE["payload"]["vacancies"]
URL = "https://www.tbank.ru/pfpjobs/papi/getVacancies"


def params(**kw):
    return {
        "categories": ["tcareer_it", "tcareer_back_office"],
        "include_internships": True,
        "limit": 100,
        "max_pages": 20,
        **kw,
    }


def page(items, offset, finished, total=None):
    return {
        "resultCode": "OK",
        "payload": {
            "vacancies": items,
            "nextPagination": {
                "offset": offset,
                "isFinished": finished,
                "totalCount": total or offset,
            },
        },
    }


async def run(source_params):
    async with httpx.AsyncClient() as http:
        return await TBankSource(http, source_params, delay=0).fetch()


@respx.mock
async def test_maps_fixture():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    assert len(postings) == len(ITEMS)
    assert all(p.source == "tbank" and p.company == "Т-Банк" for p in postings)

    p = postings[0]
    assert p.title == ITEMS[0]["title"]
    assert p.external_id == ITEMS[0]["urlSlug"]
    assert p.url == (
        "https://www.tbank.ru/career/back-office/vacancy/all/"
        f"{ITEMS[0]['seoSlug']}/{ITEMS[0]['urlSlug']}/"
    )
    assert p.cities == ("Мурманск",)
    assert p.work_formats == frozenset()  # "Разъездной" is not office/remote/hybrid
    assert p.employment is None
    assert p.experience_years_min == 1
    assert p.description == ITEMS[0]["shortDescription"]
    assert p.published_at is None
    assert p.raw == ITEMS[0]

    body = route.calls[0].request.content
    sent = json.loads(body)
    assert sent["pagination"] == {"offset": 0}
    assert sent["limit"] == 100
    gql = sent["filters"]["generatedGraphQL"]
    assert gql["type"] == "T_CAREER" and gql["status"] == "ACTIVE"
    assert gql["includeInternshipPublications"] is True
    assert gql["or"] == [{"category": "tcareer_it"}, {"category": "tcareer_back_office"}]


@respx.mock
async def test_maps_tags_and_fallbacks():
    respx.post(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    intern, hybrid_intern, middle, remote = postings[1], postings[2], postings[3], postings[4]
    # redirectUrl-only vacancy keeps its redirect, grade "Стажер" is an internship
    assert intern.url == "https://l.tbank.ru/vac_analyst"
    assert intern.employment == Employment.INTERNSHIP
    assert intern.experience_years_min == 0
    assert intern.cities == ()
    assert intern.work_formats == frozenset()
    assert hybrid_intern.work_formats == frozenset({WorkFormat.HYBRID})
    # no city, no stated work format, no experience tag
    assert middle.cities == () and middle.work_formats == frozenset()
    assert middle.experience_years_min is None and middle.employment is None
    assert middle.url.startswith("https://www.tbank.ru/career/it/vacancy/all/produktovyi-analitik-ds/")
    assert remote.work_formats == frozenset({WorkFormat.REMOTE})
    assert remote.experience_years_min == 0


@respx.mock
async def test_paginates():
    first = page(ITEMS[:2], 2, False, total=5)
    second = page(ITEMS[2:], 5, True, total=5)
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    postings = await run(params(limit=2))
    assert len(postings) == 5
    offsets = [json.loads(c.request.content)["pagination"]["offset"] for c in route.calls]
    assert offsets == [0, 2]
    assert json.loads(route.calls[1].request.content)["limit"] == 2


@respx.mock
async def test_dedups_ids_across_pages():
    first = page(ITEMS[:2], 2, False, total=3)
    second = page(ITEMS[1:3], 4, True, total=3)
    respx.post(URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    postings = await run(params(limit=2))
    assert [p.external_id for p in postings] == [i["urlSlug"] for i in ITEMS[:3]]


@respx.mock
async def test_stops_at_max_pages():
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json=page(ITEMS[:1], 1, False, total=99))
    )
    await run(params(max_pages=3))
    assert route.call_count == 3


@respx.mock
async def test_raises_on_http_error():
    first = page(ITEMS[:2], 2, False, total=5)
    respx.post(URL).mock(side_effect=[httpx.Response(200, json=first), httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(limit=2))


@respx.mock
async def test_raises_on_bad_result_code():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"resultCode": "ERROR"}))
    with pytest.raises(RuntimeError):
        await run(params())
