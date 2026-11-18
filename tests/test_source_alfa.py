import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import WorkFormat
from vacancy_bot.sources.alfa import AlfaSource, _map

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "alfa_list.json").read_text("utf-8"))
ITEMS = FIXTURE["items"]
URL = "https://job.alfabank.ru/api/vacancies"
OPTIONS_URL = "https://job.alfabank.ru/api/vacancies/options"
CITIES = {
    "optionLists": {
        "cities": [
            {"id": "0100", "text": "Москва", "parentId": "", "count": 3},
            {"id": "0160", "text": "Пенза", "parentId": "", "count": 1},
            {"id": "0165", "text": "Ростов-на-Дону", "parentId": "", "count": 1},
        ]
    }
}


def params(**kw):
    return {"business_lines": ["1022", "1018"], "take": 100, "max_pages": 10, **kw}


def page(items, total):
    return {"total": total, "totalInternal": 0, "take": 100, "items": items}


@pytest.fixture
def cities():
    return respx.get(OPTIONS_URL).mock(return_value=httpx.Response(200, json=CITIES))


async def run(source_params):
    async with httpx.AsyncClient() as http:
        return await AlfaSource(http, source_params, delay=0).fetch()


@respx.mock
async def test_maps_fixture(cities):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    postings = await run(params())
    assert len(postings) == len(ITEMS)
    assert all(p.source == "alfa" and p.company == "Альфа-Банк" for p in postings)

    p = postings[0]
    assert p.external_id == "39906"
    assert p.title == "Старший инженер данных"
    assert p.url == (
        "https://job.alfabank.ru/vacancies/moskva/remote-job/starshii-inzhener-dannyikh_39906"
    )
    assert p.cities == ("Москва",)
    assert p.work_formats == frozenset({WorkFormat.HYBRID})  # "Гибридный - больше удалённо"
    assert p.employment is None
    assert p.experience_years_min == 3  # 3-5 лет
    assert p.description == ITEMS[0]["descriptionText"]
    assert p.published_at == datetime(2026, 11, 18, 12, 51, 23, 888697, tzinfo=UTC)

    assert postings[1].work_formats == frozenset({WorkFormat.REMOTE})
    assert postings[1].experience_years_min == 1  # 1-3 года
    assert postings[2].work_formats == frozenset({WorkFormat.HYBRID})
    assert postings[3].work_formats == frozenset({WorkFormat.OFFICE})
    assert postings[3].cities == ("Пенза",)

    q = route.calls[0].request.url.params
    assert q.get_list("businessLine") == ["1022", "1018"]
    assert q["take"] == "100" and q["skip"] == "0"
    assert "experience" not in q  # the project filter decides on experience
    cq = cities.calls[0].request.url.params
    assert cq.get_list("listId") == ["cities"]
    assert cq.get_list("businessLine") == ["1022", "1018"]


@respx.mock
async def test_does_not_invent_values(cities):
    bare = {"id": "1", "name": "Аналитик", "slug": "/x/y_1", "cityId": "9999", "createdAt": None}
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([bare], 1)))
    (p,) = await run(params())
    assert p.cities == ()  # unknown city id
    assert p.work_formats == frozenset()
    assert p.employment is None
    assert p.experience_years_min is None
    assert p.published_at is None
    assert p.description == ""


def test_experience_mapping():
    def years(exp):
        return _map({"id": "1", "name": "x", "slug": "/s_1", "experienceId": exp}, {}).experience_years_min

    assert years("custom_voc_5_entry_1") == 0  # Без опыта
    assert years("custom_voc_5_entry_2") == 0  # До 1 года
    assert years("custom_voc_5_entry_5") == 1  # 1-3 года
    assert years("custom_voc_5_entry_3") == 1  # 1-5 лет
    assert years("custom_voc_5_entry_6") == 3  # 3-5 лет
    assert years("custom_voc_5_entry_4") == 5  # Более 5 лет
    assert years("custom_voc_5_entry_99") is None


def test_raw_drops_contact_data():
    item = {
        "id": "1",
        "name": "x",
        "slug": "/s_1",
        "contactFullName": "Иванов Иван",
        "contactPhone": "+7 000",
        "contactEmail": "a@b.c",
    }
    raw = _map(item, {}).raw
    assert not [k for k in raw if k.startswith("contact")]
    assert raw["id"] == "1"


@respx.mock
async def test_paginates(cities):
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 5)),
            httpx.Response(200, json=page(ITEMS[2:], 5)),
        ]
    )
    postings = await run(params(take=2))
    assert len(postings) == 5
    assert [c.request.url.params["skip"] for c in route.calls] == ["0", "2"]
    assert route.calls[1].request.url.params["take"] == "2"


@respx.mock
async def test_dedups_ids_across_pages(cities):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 3)),
            httpx.Response(200, json=page(ITEMS[1:3], 3)),
        ]
    )
    postings = await run(params(take=2))
    assert [p.external_id for p in postings] == [i["id"] for i in ITEMS[:3]]


@respx.mock
async def test_raises_when_max_pages_exhausted(cities):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[i : i + 1], 99)) for i in range(3)]
    )
    with pytest.raises(RuntimeError, match="max_pages"):
        await run(params(max_pages=3))
    assert route.call_count == 3


@respx.mock
async def test_raises_on_empty_page_before_total(cities):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=page([], 99)))
    with pytest.raises(RuntimeError, match="empty page"):
        await run(params())
    assert route.call_count == 1


@respx.mock
async def test_raises_on_non_json_200(cities):
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>captcha</html>"))
    with pytest.raises(RuntimeError, match="alfa: malformed"):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"total": 1},
        {"items": []},
        {"total": "1", "items": []},
        {"total": 1, "items": [{"name": "x"}]},
        {"total": 1, "items": [{"id": "1", "name": "x", "slug": "/s_1", "createdAt": "nope"}]},
        [1, 2],
    ],
)
async def test_raises_on_malformed_payload(cities, body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="malformed"):
        await run(params())


@respx.mock
async def test_raises_on_http_error(cities):
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[:2], 5)), httpx.Response(503)]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(take=2))


@respx.mock
async def test_raises_on_cities_http_error():
    respx.get(OPTIONS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [{"optionLists": {}}, {"optionLists": {"cities": [{"id": "1"}]}}, {"x": 1}],
)
async def test_raises_on_malformed_cities(body):
    respx.get(OPTIONS_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="alfa: malformed cities"):
        await run(params())


@respx.mock
async def test_raises_on_non_json_cities():
    respx.get(OPTIONS_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(RuntimeError, match="alfa: malformed cities"):
        await run(params())
