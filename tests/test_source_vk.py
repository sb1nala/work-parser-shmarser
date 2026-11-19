import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import Employment, WorkFormat
from vacancy_bot.sources.vk import VKSource, _experience, _map_vacancy

FIXTURES = Path(__file__).parent / "fixtures"
LIST = json.loads((FIXTURES / "vk_list.json").read_text("utf-8"))
DETAILS = json.loads((FIXTURES / "vk_detail.json").read_text("utf-8"))
STATE = json.loads((FIXTURES / "vk_internship_state.json").read_text("utf-8"))
ITEMS = LIST["results"]
IDS = [i["id"] for i in ITEMS]
URL = "https://team.vk.company/career/api/v2/vacancies/"
SITE = "https://team.vk.company/vacancy"
PORTAL = "https://internship.vk.company"


def params(**kw):
    return {"specialties": [295], "page_size": 50, "max_pages": 10, **kw}


def page(items, next_offset=None):
    nxt = f"{URL}?limit=50&offset={next_offset}" if next_offset else None
    return {"count": 99, "next": nxt, "previous": None, "results": items}


def detail_routes(ids=IDS):
    for i in ids:
        respx.get(f"{SITE}/{i}/").mock(return_value=httpx.Response(200, text=DETAILS[str(i)]))


def portal_html(state):
    blob = json.dumps(state, ensure_ascii=False)
    return f'<html><script id="__NEXT_DATA__" type="application/json">{blob}</script></html>'


def state_with(vacancies):
    page_state = {**STATE["props"]["pageProps"]["page"], "vacancies": vacancies}
    return {"props": {"pageProps": {"page": page_state}}}


async def run(source_params):
    async with httpx.AsyncClient() as http:
        return await VKSource(http, source_params, delay=0).fetch()


@respx.mock
async def test_maps_fixture():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=LIST))
    detail_routes()
    postings = await run(params())
    assert [p.external_id for p in postings] == [str(i) for i in IDS]
    assert all(p.source == "vk" and p.company == "VK" for p in postings)

    first = postings[0]  # 46221, remote flag with a "flexible" work_format
    assert first.title == "Продуктовый аналитик"
    assert first.url == f"{SITE}/46221/"
    assert first.cities == ("Москва",)
    assert first.work_formats == frozenset({WorkFormat.REMOTE})
    assert first.employment is Employment.FULL
    assert first.experience_years_min == 1  # middle, senior -> lower bound
    assert first.published_at == datetime(2026, 11, 19, tzinfo=UTC)
    assert first.description.startswith("Ищем продуктового аналитика")
    assert "<p>" not in first.description and "<br" not in first.description

    assert postings[1].work_formats == frozenset({WorkFormat.HYBRID})  # "Комбинированный"
    assert postings[1].experience_years_min == 3  # senior
    assert postings[2].work_formats == frozenset({WorkFormat.REMOTE})  # "Дистанционный"
    assert postings[3].experience_years_min == 1  # middle

    q = route.calls[0].request.url.params
    assert q["specialty"] == "295"
    assert q["limit"] == "50"
    assert q["offset"] == "0"


@respx.mock
async def test_does_not_invent_values():
    bare = {"id": 7, "title": "Аналитик", "town": None, "work_format": "гибкий", "remote": False}
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([bare])))
    respx.get(f"{SITE}/7/").mock(
        return_value=httpx.Response(200, text='<div itemtype="http://schema.org/JobPosting"></div>')
    )
    (p,) = await run(params())
    assert p.cities == ()
    assert p.work_formats == frozenset()  # "гибкий" is not a stated office/hybrid/remote
    assert p.employment is None
    assert p.experience_years_min is None
    assert p.published_at is None
    assert p.description == ""


def test_junior_detail_maps_to_zero_experience():
    item = {"id": 52788, "title": "Младший аналитик по стратегии", "town": {"name": "Москва"}}
    item |= {"work_format": "Комбинированный", "remote": False}
    p = _map_vacancy(item, DETAILS["52788"])
    assert p.experience_years_min == 0  # junior, middle
    assert p.employment is Employment.FULL
    assert "Исследование рынков" in p.description


def test_experience_mapping():
    assert _experience(["junior"]) == 0
    assert _experience(["Junior", "middle"]) == 0
    assert _experience(["middle"]) == 1
    assert _experience(["middle", "senior"]) == 1
    assert _experience(["senior"]) == 3
    assert _experience(["lead", "head"]) == 5
    assert _experience(["lead", "senior"]) == 3
    assert _experience(["intern"]) == 0
    assert _experience(["стажёр"]) == 0
    assert _experience(["стажер", "junior"]) == 0
    assert _experience(["whatever"]) is None
    assert _experience([]) is None


def test_intern_level_means_internship():
    item = {"id": 1, "title": "x", "town": None, "work_format": None, "remote": False}
    html = (
        '<div itemtype="http://schema.org/JobPosting"></div>'
        '<h4 class="vacancy-title">Уровень</h4><div><div class="vacancy-tag">стажёр</div></div>'
    )
    p = _map_vacancy(item, html)
    assert p.employment is Employment.INTERNSHIP
    assert p.experience_years_min == 0


def test_part_time_schedule():
    item = {"id": 1, "title": "x", "town": None, "work_format": None, "remote": False}
    html = (
        '<div itemtype="http://schema.org/JobPosting"></div>'
        '<h4 class="vacancy-title">График работы</h4><div><div class="vacancy-tag">'
        "частичный</div></div>"
    )
    assert _map_vacancy(item, html).employment is Employment.PART
    assert _map_vacancy(item, html.replace("частичный", "сменный")).employment is None


def test_raw_is_an_allowlist_without_personal_data():
    item = {
        "id": 1,
        "title": "x",
        "town": {"name": "Москва"},
        "work_format": "Офисный",
        "remote": False,
        "tags": [{"name": "sql"}],
        "group": {"name": "Почта", "project_logo": "https://x/logo.png"},
        "specialty": {"id": 295, "name": "Продуктовая аналитика"},
        "recruiter": {"name": "Иванов Иван", "phone": "+7 000", "email": "a@b.c"},
    }
    html = '<div itemtype="http://schema.org/JobPosting"></div>'
    raw = _map_vacancy(item, html).raw
    blob = json.dumps(raw, ensure_ascii=False)
    for secret in ("Иванов", "+7 000", "a@b.c", "logo.png"):
        assert secret not in blob
    assert raw["id"] == 1 and raw["group"] == "Почта" and raw["tags"] == ["sql"]


@respx.mock
async def test_paginates_with_offset():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 2)),
            httpx.Response(200, json=page(ITEMS[2:])),
        ]
    )
    detail_routes()
    postings = await run(params())
    assert len(postings) == 5
    assert route.calls[0].request.url.params["offset"] == "0"
    assert route.calls[1].request.url.params["offset"] == "2"
    assert route.calls[1].request.url.params["specialty"] == "295"


@respx.mock
async def test_dedups_ids_across_pages_and_specialties():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 2)),
            httpx.Response(200, json=page(ITEMS[1:3])),
            httpx.Response(200, json=page(ITEMS[2:4])),
        ]
    )
    detail_routes()
    postings = await run(params(specialties=[295, 283]))
    assert [p.external_id for p in postings] == [str(i) for i in IDS[:4]]
    assert [c.request.url.params["specialty"] for c in route.calls] == ["295", "295", "283"]
    assert route.calls[2].request.url.params["offset"] == "0"  # offset restarts per specialty
    assert respx.calls.call_count == 3 + 4  # each detail page is fetched once


@respx.mock
async def test_multi_query_partial_failure_fails_whole_fetch():
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[:2])), httpx.Response(503)]
    )
    detail_routes()
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(specialties=[295, 283]))


@respx.mock
async def test_raises_on_stuck_offset():
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:1], 1)),
            httpx.Response(200, json=page(ITEMS[1:2], 1)),
        ]
    )
    with pytest.raises(RuntimeError, match="offset did not advance"):
        await run(params())
    assert route.call_count == 2


@respx.mock
async def test_raises_when_max_pages_exhausted():
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[i : i + 1], i + 1)) for i in range(3)]
    )
    with pytest.raises(RuntimeError, match="max_pages"):
        await run(params(max_pages=3))
    assert route.call_count == 3


@respx.mock
async def test_raises_on_empty_page_before_end():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([], 50)))
    with pytest.raises(RuntimeError, match="empty page"):
        await run(params())


@respx.mock
async def test_raises_on_non_json_200():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>captcha</html>"))
    with pytest.raises(RuntimeError, match="vk: malformed"):
        await run(params())


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"count": 1},
        {"results": "x"},
        {"results": [{"title": "no id"}]},
        {"results": [{"id": "1", "title": "x"}]},
        {"results": [{"id": 1}]},
        {"results": [], "next": 5},
        {"results": [{"id": 1, "title": "x"}], "next": f"{URL}?limit=50"},
        {"results": [{"id": 1, "title": "x"}], "next": f"{URL}?limit=50&offset=abc"},
        [1, 2],
    ],
)
async def test_raises_on_malformed_list(body):
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RuntimeError, match="vk: malformed"):
        await run(params())


@respx.mock
async def test_raises_on_http_error_in_list():
    respx.get(URL).mock(
        side_effect=[httpx.Response(200, json=page(ITEMS[:2], 2)), httpx.Response(503)]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
async def test_raises_on_http_error_in_detail():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:2])))
    detail_routes(IDS[:1])
    respx.get(f"{SITE}/{IDS[1]}/").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await run(params())


@respx.mock
async def test_detail_failure_fails_whole_fetch_not_partial():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:3])))
    detail_routes(IDS[:1])
    respx.get(f"{SITE}/{IDS[1]}/").mock(
        return_value=httpx.Response(200, text="<html>checking your browser</html>")
    )
    with pytest.raises(RuntimeError, match=f"vk: malformed vacancy {IDS[1]}"):
        await run(params())


@respx.mock
async def test_raises_on_bad_detail_date():
    html = (
        '<div itemtype="http://schema.org/JobPosting">'
        '<meta itemprop="datePosted" content="x"></div>'
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:1])))
    respx.get(f"{SITE}/{IDS[0]}/").mock(return_value=httpx.Response(200, text=html))
    with pytest.raises(RuntimeError, match=f"vk: malformed vacancy {IDS[0]}"):
        await run(params())


@respx.mock
async def test_raises_on_malformed_list_item_fields():
    bad = {"id": 7, "title": "x", "town": "Москва", "work_format": None, "remote": False}
    respx.get(URL).mock(return_value=httpx.Response(200, json=page([bad])))
    respx.get(f"{SITE}/7/").mock(
        return_value=httpx.Response(200, text='<div itemtype="http://schema.org/JobPosting">')
    )
    with pytest.raises(RuntimeError, match="vk: malformed vacancy 7"):
        await run(params())


@respx.mock
async def test_paces_requests(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("vacancy_bot.sources.vk.asyncio.sleep", fake_sleep)
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=page(ITEMS[:2], 2)),
            httpx.Response(200, json=page(ITEMS[2:3])),
        ]
    )
    detail_routes()
    async with httpx.AsyncClient() as http:
        await VKSource(http, params(), delay=0.5).fetch()
    # one pause before the second list page, one before each of the 3 detail pages
    assert sleeps == [0.5] * 4


# --- internship portal (internship.vk.company, embedded page state) ---


@respx.mock
async def test_maps_internship_portal():
    respx.get(f"{PORTAL}/internship").mock(
        return_value=httpx.Response(200, text=portal_html(STATE))
    )
    postings = await run(params(specialties=[], portal_pages=["internship"]))
    assert [p.external_id for p in postings] == [
        "intern-1480", "intern-1479", "intern-1477", "intern-1476", "intern-1471",
    ]  # fmt: skip
    assert all(p.source == "vk" and p.company == "VK" for p in postings)
    assert all(p.employment is Employment.INTERNSHIP for p in postings)
    assert all(p.experience_years_min == 0 for p in postings)

    py = postings[0]
    assert py.title == "Python developer"
    assert py.url == f"{PORTAL}/vacancy/1480"
    assert py.cities == ()  # "Любой" is not a city
    assert py.work_formats == frozenset({WorkFormat.REMOTE})
    assert py.published_at is None

    ml = postings[4]
    assert ml.title == "ML-инженер"
    assert ml.cities == ("Москва",)
    assert ml.work_formats == frozenset({WorkFormat.HYBRID})
    assert "Блок AI" in ml.description and "Машинное обучение" in ml.description
    assert "ноябрь 2026" in ml.description and "PyTorch" in ml.description
    assert ml.raw["id"] == 1471 and ml.raw["unit"] == "Блок AI"


@respx.mock
async def test_portal_skips_closed_and_maps_junior_type():
    base = STATE["props"]["pageProps"]["page"]["vacancies"][4]
    closed = {**base, "id": 1, "is_opened": False}
    junior = {**base, "id": 2, "internship_type": "junior", "employment": "part_time"}
    unknown = {**base, "id": 3, "format": "weird", "employment": "weird", "city": None}
    respx.get(f"{PORTAL}/vacancy").mock(
        return_value=httpx.Response(200, text=portal_html(state_with([closed, junior, unknown])))
    )
    postings = await run(params(specialties=[], portal_pages=["vacancy"]))
    assert [p.external_id for p in postings] == ["intern-2", "intern-3"]
    assert postings[0].employment is Employment.PART  # a junior vacancy, not an internship
    assert postings[0].experience_years_min == 0
    assert postings[1].work_formats == frozenset()
    assert postings[1].cities == ()
    assert postings[1].employment is Employment.INTERNSHIP


@respx.mock
async def test_portal_dedups_across_pages_and_combines_with_api():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:1])))
    detail_routes(IDS[:1])
    for path in ("internship", "vacancy"):
        respx.get(f"{PORTAL}/{path}").mock(
            return_value=httpx.Response(200, text=portal_html(STATE))
        )
    postings = await run(params(portal_pages=["internship", "vacancy"]))
    assert [p.external_id for p in postings] == [str(IDS[0])] + [
        f"intern-{i}" for i in (1480, 1479, 1477, 1476, 1471)
    ]


@respx.mock
async def test_portal_empty_vacancies_is_fine():
    respx.get(f"{PORTAL}/vacancy").mock(
        return_value=httpx.Response(200, text=portal_html(state_with([])))
    )
    assert await run(params(specialties=[], portal_pages=["vacancy"])) == []


@respx.mock
async def test_portal_http_error_fails_whole_fetch():
    respx.get(URL).mock(return_value=httpx.Response(200, json=page(ITEMS[:1])))
    detail_routes(IDS[:1])
    respx.get(f"{PORTAL}/internship").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(portal_pages=["internship"]))


@respx.mock
@pytest.mark.parametrize(
    "text",
    [
        "<html>no state here</html>",
        '<script id="__NEXT_DATA__" type="application/json">not json</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>',
        '<script id="__NEXT_DATA__" type="application/json">[1]</script>',
        portal_html(state_with("x")),
        portal_html(state_with([{"id": 1}])),
        portal_html(state_with([{"id": "1", "title": "x", "is_opened": True}])),
        portal_html(state_with([{"id": 1, "title": "x", "is_opened": True, "skills": "x"}])),
    ],
)
async def test_portal_raises_on_malformed_state(text):
    respx.get(f"{PORTAL}/internship").mock(return_value=httpx.Response(200, text=text))
    with pytest.raises(RuntimeError, match="vk: malformed internship portal page internship"):
        await run(params(specialties=[], portal_pages=["internship"]))
