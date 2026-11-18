import json
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import Employment, WorkFormat
from vacancy_bot.sources.vtb import VTBSource, _map

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "vtb_list.json").read_text("utf-8"))
ITEMS = FIXTURE["items"]
# the saved page is one of 56 of the unfiltered list; serve it as the only page of a query
SINGLE = {**FIXTURE, "found": len(ITEMS), "pages": 1}
URL = "https://backend.honeyleads.ru/vtb-federal-campaign/vacancies"


def params(**kw):
    return {
        "queries": [{"text": "стажер OR стажёр"}],
        "max_pages": 60,
        **kw,
    }


def page(items, number, pages, found=None):
    return {
        "items": items,
        "found": found if found is not None else len(items),
        "pages": pages,
        "page": number,
        "per_page": 20,
    }


async def run(source_params):
    async with httpx.AsyncClient() as http:
        return await VTBSource(http, source_params, delay=0).fetch()


@respx.mock
async def test_maps_fixture():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=SINGLE))
    postings = await run(params())
    assert len(postings) == len(ITEMS)
    assert all(p.source == "vtb" and p.company == "ВТБ" for p in postings)

    p = postings[0]
    assert p.title == ITEMS[0]["name"]
    assert p.external_id == "136133923"
    assert p.url == "https://rabota-vtb.ru/career/136133923"
    assert p.cities == ("Москва",)
    assert p.work_formats == frozenset({WorkFormat.OFFICE})
    assert p.employment is Employment.FULL
    assert p.experience_years_min == 0
    assert "Аналитика и отчетность" in p.title
    assert p.published_at is not None and p.published_at.utcoffset() is not None
    assert p.raw == ITEMS[0]

    request = route.calls[0].request
    assert request.url.params["page"] == "0"
    assert request.url.params["text"] == "стажер OR стажёр"


@respx.mock
async def test_maps_flags_and_formats():
    respx.get(URL).mock(return_value=httpx.Response(200, json=SINGLE))
    postings = await run(params())
    intern, analyst, part_intern, remote = postings[1], postings[2], postings[3], postings[4]
    assert intern.employment is Employment.INTERNSHIP
    assert analyst.employment is Employment.FULL
    assert analyst.experience_years_min == 1
    # internship flag wins over the part-time employment form; both formats are kept
    assert part_intern.employment is Employment.INTERNSHIP
    assert part_intern.work_formats == frozenset({WorkFormat.OFFICE, WorkFormat.HYBRID})
    assert remote.work_formats == frozenset({WorkFormat.REMOTE})


def test_unknown_fields_stay_empty():
    p = _map({"id": 7, "name": "Аналитик", "alternate_url": "https://hh.ru/vacancy/7"})
    assert p.external_id == "7"
    assert p.cities == ()
    assert p.work_formats == frozenset()
    assert p.employment is None
    assert p.experience_years_min is None
    assert p.published_at is None
    assert p.description == ""


def test_no_invented_values_for_unmapped_ids():
    p = _map(
        {
            "id": 7,
            "name": "x",
            "area": None,
            "work_format": [{"id": "FIELD_WORK"}],
            "employment_form": {"id": "FLY_IN_FLY_OUT"},
            "experience": {"id": "something_new"},
            "internship": False,
            "snippet": {"requirement": None, "responsibility": None},
        }
    )
    assert p.cities == ()
    assert p.work_formats == frozenset()
    assert p.employment is None
    assert p.experience_years_min is None


@pytest.mark.parametrize(
    "exp,years",
    [("noExperience", 0), ("between1And3", 1), ("between3And6", 3), ("moreThan6", 6)],
)
def test_experience_ids(exp, years):
    assert _map({"id": 1, "name": "x", "experience": {"id": exp}}).experience_years_min == years


@pytest.mark.parametrize(
    "form,employment",
    [("FULL", Employment.FULL), ("PART", Employment.PART), ("PROJECT", Employment.PROJECT)],
)
def test_employment_forms(form, employment):
    assert _map({"id": 1, "name": "x", "employment_form": {"id": form}}).employment is employment


@respx.mock
async def test_paginates():
    first = page(ITEMS[:2], 0, 2, found=5)
    second = page(ITEMS[2:], 1, 2, found=5)
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    postings = await run(params())
    assert len(postings) == 5
    assert [c.request.url.params["page"] for c in route.calls] == ["0", "1"]


@respx.mock
async def test_runs_every_query_and_dedups_across_them():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:3], 0, 1)),
            httpx.Response(200, json=page(ITEMS[2:], 0, 1)),
        ]
    )
    postings = await run(params(queries=[{"text": "стажер"}, {"text": "аналитик"}]))
    assert [c.request.url.params["text"] for c in route.calls] == ["стажер", "аналитик"]
    assert [p.external_id for p in postings] == [i["id"] for i in ITEMS]


@respx.mock
async def test_dedups_ids_across_pages():
    first = page(ITEMS[:2], 0, 2, found=3)
    second = page(ITEMS[1:3], 1, 2, found=3)
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    postings = await run(params())
    assert [p.external_id for p in postings] == [i["id"] for i in ITEMS[:3]]


@respx.mock
async def test_raises_when_max_pages_exhausted():
    pages = [page(ITEMS[i : i + 1], i, 99, found=99) for i in range(3)]
    route = respx.get(URL).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    with pytest.raises(RuntimeError, match="max_pages"):
        await run(params(max_pages=3))
    assert route.call_count == 3


@respx.mock
async def test_raises_on_stuck_page_number():
    # the server keeps answering page 0, whatever was requested
    route = respx.get(URL).mock(
        return_value=httpx.Response(200, json=page(ITEMS[:1], 0, 5, found=99))
    )
    with pytest.raises(RuntimeError, match="did not advance"):
        await run(params())
    assert route.call_count == 2


@respx.mock
async def test_raises_on_empty_page_before_the_end():
    first = page(ITEMS[:2], 0, 3, found=50)
    empty = page([], 1, 3, found=50)
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=empty)]
    )
    with pytest.raises(RuntimeError, match="did not advance"):
        await run(params())


@respx.mock
async def test_raises_on_non_json_200():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>captcha</html>"))
    with pytest.raises(RuntimeError, match=r"vtb: malformed .*page 0"):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"items": None, "pages": 1},
        {"items": [], "found": 0},
        {"items": [{"name": "x"}], "pages": 1, "page": 0},
        {"items": [], "pages": "1", "page": 0},
        {"items": [], "pages": 1, "page": "0"},
        [1, 2],
    ],
)
async def test_raises_on_malformed_payload(body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="malformed"):
        await run(params())


@respx.mock
async def test_raises_on_http_error():
    first = page(ITEMS[:2], 0, 2, found=5)
    respx.get(URL).mock(side_effect=[httpx.Response(200, json=first), httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "second,error",
    [
        (httpx.Response(503), httpx.HTTPStatusError),
        (httpx.Response(200, text="<html>captcha</html>"), RuntimeError),
        (httpx.Response(200, json={"items": None, "pages": 1, "page": 0}), RuntimeError),
    ],
)
async def test_later_query_failure_returns_nothing_partial(second, error):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[:2], 0, 1)), second]
    )
    source_params = params(queries=[{"text": "стажер"}, {"text": "аналитик"}])
    async with httpx.AsyncClient() as http:
        source = VTBSource(http, source_params, delay=0)
        with pytest.raises(error):
            await source.fetch()
    assert route.call_count == 2


@respx.mock
async def test_max_pages_default_matches_config():
    import yaml

    cfg = yaml.safe_load((Path(__file__).parents[1] / "config" / "config.yaml").read_text("utf-8"))
    assert cfg["sources"]["vtb"]["max_pages"] == 10
    route = respx.get(URL).mock(
        side_effect=lambda request: httpx.Response(
            200, json=page(ITEMS[:1], int(request.url.params["page"]), 99)
        )
    )
    with pytest.raises(RuntimeError, match="max_pages=10"):
        await run({"queries": [{"text": "x"}]})
    assert route.call_count == 10
