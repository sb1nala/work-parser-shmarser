import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from vacancy_bot.domain import Employment, WorkFormat
from vacancy_bot.sources.avito import AvitoSource

FIXTURES = Path(__file__).parent / "fixtures"
LIST = json.loads((FIXTURES / "avito_list.json").read_text("utf-8"))  # slug -> real item markup
DETAILS = json.loads((FIXTURES / "avito_detail.json").read_text("utf-8"))  # url id -> real page
SITE = "https://career.avito.com"
MAIN = f"{SITE}/vacancies/"
MSK = timezone(timedelta(hours=3))


def params(**kw):
    return {
        "directions": ["analitika-dannykh", "razrabotka"],
        "keep_directions": ["analitika-dannykh", "razrabotka"],
        **kw,
    }


def item(
    vid=1,
    url_id=100,
    slug="analitika-dannykh",
    title="Аналитик данных",
    section="Аналитика данных",
    geo="Москва",
    intern="Нет",
    fmt="Гибрид",
    team="Работа",
):
    """A list entry shaped like the real one (attribute order differs from the site on purpose)."""
    fmt_html = f'<span class="vacancies-section__item-format" data-hint="">{fmt}</span>' if fmt else ""
    return (
        f'<div class="vacancies-section__item" data-vacancy-team="{team}" '
        f'data-vacancy-intern="{intern}" data-vacancy-geo="{geo}" data-vacancy-id="{vid}" '
        f'data-vacancy-section="{section}" data-vacancy-remote="Да">'
        f'<a href="/vacancies/{slug}/{url_id}/" class="vacancies-section__item-link"></a>'
        f'<div class="vacancies-section__item-content">'
        f'<a href="/vacancies/{slug}/{url_id}/" class="vacancies-section__item-name">{title}</a>'
        f"</div>"
        f'<div class="vacancies-section__item-meta"><div class="vacancies-section__item-options">'
        f"{fmt_html}</div></div></div>"
    )


def list_page(*items):
    return f'<html><div class="vacancies-section__items">{"".join(items)}</div></html>'


def main_page(counts):
    """The main page: one titled section per entry, with its total count."""
    return "<html>" + "".join(
        f'<div class="vacancies-section__title"><h2>{name}</h2>'
        f'<span class="vacancies-section__items-count">{n}</span></div>'
        for name, n in counts.items()
    ) + "</html>"


def job_page(posted="2026-11-11T11:45:33.000+03:00", employment="FULL_TIME", description="Текст"):
    ld = {"@context": "https://schema.org/", "@type": "JobPosting", "title": "x"}
    if posted:
        ld["datePosted"] = posted
    if employment:
        ld["employmentType"] = employment
    if description:
        ld["description"] = description
    org = '{"@context": "https://schema.org", "@type": "Organization", "name": "Авито"}'
    return (
        f'<html><script type="application/ld+json">{org}</script>'
        f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></html>'
    )


def fixture_counts():
    counts = {}
    for html in LIST.values():
        for section in ("Аналитика данных", "Разработка"):
            counts[section] = counts.get(section, 0) + html.count(f'data-vacancy-section="{section}"')
    return counts


def mock_fixture(**routes):
    respx.get(MAIN).mock(return_value=httpx.Response(200, text=main_page(fixture_counts())))
    for slug, html in LIST.items():
        respx.get(f"{MAIN}{slug}/").mock(return_value=httpx.Response(200, text=html))
    for url_id, html in DETAILS.items():
        slug = "razrabotka" if url_id == "20855" else "analitika-dannykh"
        respx.get(f"{MAIN}{slug}/{url_id}/").mock(return_value=httpx.Response(200, text=html))


async def run(source_params=None, delay=0):
    async with httpx.AsyncClient() as http:
        return await AvitoSource(http, source_params or params(), delay=delay).fetch()


def mock_simple(pages, counts=None, details=None):
    """pages: {slug: [items]}; counts default to the number of listed items per section."""
    if counts is None:
        counts = {}
        for items in pages.values():
            for it in items:
                section = it.split('data-vacancy-section="')[1].split('"')[0]
                counts[section] = counts.get(section, 0) + 1
    respx.get(MAIN).mock(return_value=httpx.Response(200, text=main_page(counts)))
    for slug, items in pages.items():
        respx.get(f"{MAIN}{slug}/").mock(return_value=httpx.Response(200, text=list_page(*items)))
    for path, html in (details or {}).items():
        respx.get(f"{SITE}{path}").mock(return_value=httpx.Response(200, text=html))


@respx.mock
async def test_maps_fixture():
    mock_fixture()
    postings = await run()
    assert [p.external_id for p in postings] == ["20883", "20879", "20954", "20857", "20855"]
    assert all(p.source == "avito" and p.company == "Авито" for p in postings)
    first = postings[0]
    assert first.url == f"{SITE}/vacancies/analitika-dannykh/20883/"
    assert first.title == "Аналитик данных в команду модерации"
    assert first.cities == ("Москва",)
    assert first.work_formats == {WorkFormat.REMOTE, WorkFormat.HYBRID}
    assert first.employment is Employment.FULL
    assert first.published_at == datetime(2026, 11, 11, 11, 45, 33, tzinfo=MSK)
    assert first.published_at.utcoffset() == timedelta(hours=3)
    assert first.description.startswith("О команде:")
    multi = postings[1]
    assert multi.title == "Аналитик данных в команду Авито Подработки"
    assert multi.cities == ("Самара", "Нижний Новгород", "Казань", "Москва", "Санкт-Петербург")
    assert postings[2].work_formats == {WorkFormat.OFFICE, WorkFormat.HYBRID}  # "Офис или гибрид"
    assert postings[3].work_formats == {WorkFormat.REMOTE}  # "Удалённая работа"


@respx.mock
async def test_does_not_invent_values():
    mock_fixture()
    postings = await run()
    bare = postings[-1]  # the real razrabotka item has no format label at all
    assert bare.external_id == "20855"
    assert bare.work_formats == frozenset()
    assert all(p.experience_years_min is None for p in postings)  # only free text, never parsed
    assert all(p.employment is Employment.FULL for p in postings)


@respx.mock
async def test_fetch_requests_only_listed_pages_once():
    mock_fixture()
    await run()
    paths = [c.request.url.path for c in respx.calls]
    assert paths[0] == "/vacancies/"
    assert paths.count("/vacancies/analitika-dannykh/") == 1
    assert paths.count("/vacancies/razrabotka/") == 1
    assert len(paths) == 1 + 2 + 5


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Гибрид", {WorkFormat.HYBRID}),
        ("Удалённая работа", {WorkFormat.REMOTE}),
        ("Удаленная работа", {WorkFormat.REMOTE}),  # ё/е
        ("Удалёнка", {WorkFormat.REMOTE}),
        ("Офис", {WorkFormat.OFFICE}),
        ("Офис или удалённая работа", {WorkFormat.OFFICE, WorkFormat.REMOTE}),
        ("Гибрид, удалённая работа или офис", set(WorkFormat)),
        ("Разъездная работа", set()),  # not one of ours: unknown, not invented
        ("", set()),
    ],
)
@respx.mock
async def test_work_format_labels(label, expected):
    mock_simple(
        {"analitika-dannykh": [item(url_id=7, fmt=label)]},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    (p,) = await run(params(directions=["analitika-dannykh"]))
    assert p.work_formats == frozenset(expected)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("FULL_TIME", Employment.FULL),
        ("PART_TIME", Employment.PART),
        ("CONTRACTOR", Employment.PROJECT),
        ("TEMPORARY", None),
        (None, None),
    ],
)
@respx.mock
async def test_employment_types(kind, expected):
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        details={"/vacancies/analitika-dannykh/7/": job_page(employment=kind)},
    )
    (p,) = await run(params(directions=["analitika-dannykh"]))
    assert p.employment == expected


@respx.mock
async def test_internship_flag_means_internship_in_any_direction():
    mock_simple(
        {
            "analitika-dannykh": [item(url_id=7)],
            "prodazhi": [
                item(vid=2, url_id=8, slug="prodazhi", section="Продажи", intern="Да"),
                item(vid=3, url_id=9, slug="prodazhi", section="Продажи", title="Менеджер"),
            ],
        },
        details={
            "/vacancies/analitika-dannykh/7/": job_page(),
            "/vacancies/prodazhi/8/": job_page(employment="FULL_TIME"),
        },  # 9 is not kept and has no detail route: it must not be requested
    )
    postings = await run(
        params(directions=["analitika-dannykh", "prodazhi"], keep_directions=["analitika-dannykh"])
    )
    assert {p.external_id: p.employment for p in postings} == {
        "7": Employment.FULL,
        "8": Employment.INTERNSHIP,
    }


@respx.mock
async def test_naive_date_is_utc():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        details={"/vacancies/analitika-dannykh/7/": job_page(posted="2026-11-11T11:45:33")},
    )
    (p,) = await run(params(directions=["analitika-dannykh"]))
    assert p.published_at == datetime(2026, 11, 11, 11, 45, 33, tzinfo=UTC)


@respx.mock
async def test_raw_is_an_allowlist_without_personal_data():
    mock_fixture()
    postings = await run()
    allowed = {
        "id", "url_id", "title", "direction", "team", "cities", "format", "intern",
        "employment_type", "date_posted",
    }  # fmt: skip
    for p in postings:
        assert set(p.raw) <= allowed
        assert "description" not in p.raw
        assert p.raw["url_id"] == p.external_id
    assert postings[0].raw["employment_type"] == "FULL_TIME"


@respx.mock
async def test_dedups_ids_across_directions():
    same = item(url_id=7)
    mock_simple(
        {"analitika-dannykh": [same, same], "razrabotka": [same]},
        counts={"Аналитика данных": 1},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    postings = await run()
    assert [p.external_id for p in postings] == ["7"]
    assert len([c for c in respx.calls if c.request.url.path.endswith("/7/")]) == 1


@respx.mock
async def test_attribute_order_does_not_matter():
    ld = json.dumps({"@type": "JobPosting", "datePosted": "2026-11-11T11:45:33+03:00"})
    swapped = f'<html><script data-x="1" type="application/ld+json">{ld}</script></html>'
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        details={"/vacancies/analitika-dannykh/7/": swapped},
    )
    (p,) = await run(params(directions=["analitika-dannykh"]))
    assert p.published_at is not None


# --- hardening ---------------------------------------------------------------------------


@respx.mock
async def test_raises_on_http_error_in_main_page():
    respx.get(MAIN).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await run()


@respx.mock
async def test_raises_on_http_error_in_direction_page():
    respx.get(MAIN).mock(return_value=httpx.Response(200, text=main_page({"Аналитика данных": 1})))
    respx.get(f"{MAIN}analitika-dannykh/").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await run()


@respx.mock
async def test_multi_page_partial_failure_fails_whole_fetch():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    respx.get(f"{MAIN}razrabotka/").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await run()


@respx.mock
async def test_raises_on_http_error_in_detail():
    mock_simple({"analitika-dannykh": [item(url_id=7)]})
    respx.get(f"{SITE}/vacancies/analitika-dannykh/7/").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_detail_failure_fails_whole_fetch_not_partial():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7), item(vid=2, url_id=8)]},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    respx.get(f"{SITE}/vacancies/analitika-dannykh/8/").mock(return_value=httpx.Response(502))
    with pytest.raises(httpx.HTTPStatusError):
        await run(params(directions=["analitika-dannykh"]))


@pytest.mark.parametrize(
    "body",
    [
        '<html><script type="application/ld+json">{not json</script></html>',
        '<html><script type="application/ld+json">[1, 2]</script></html>',
        '<html><script type="application/ld+json">{"@type": "JobPosting", "datePosted": 5}</script></html>',
        '<html><script type="application/ld+json">{"@type": "JobPosting", "datePosted": "вчера"}</script></html>',
        '<html><script type="application/ld+json">{"@type": "JobPosting", "description": 7}</script></html>',
        "",
        "<html>Service unavailable</html>",
        job_page(posted=None, description=None),  # neither a date nor a description
    ],
)
@respx.mock
async def test_raises_on_malformed_detail(body):
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        details={"/vacancies/analitika-dannykh/7/": body},
    )
    with pytest.raises(RuntimeError, match=r"avito: malformed vacancy 7"):
        await run(params(directions=["analitika-dannykh"]))


@pytest.mark.parametrize(
    "bad_item",
    [
        item(url_id=7).replace('data-vacancy-id="1"', 'data-vacancy-id=""'),
        item(url_id=7).replace('data-vacancy-id="1"', 'data-vacancy-id="abc"'),
        item(url_id=7, intern="Может быть"),
        item(url_id=7, title=""),
        item(url_id=7).replace("/vacancies/analitika-dannykh/7/", "/somewhere/else/"),
        item(url_id=7).replace(' data-vacancy-geo="Москва"', ""),
        item(url_id=7).replace(' data-vacancy-intern="Нет"', ""),
    ],
)
@respx.mock
async def test_raises_on_malformed_list_item(bad_item):
    mock_simple({"analitika-dannykh": [bad_item]}, counts={"Аналитика данных": 1})
    with pytest.raises(RuntimeError, match="avito: malformed"):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_raises_when_links_exist_but_no_items_parse():
    # the cards lost their data-vacancy-id attributes: a layout change, not an empty direction
    page = list_page(item(url_id=7).replace("data-vacancy-id", "data-id"))
    respx.get(MAIN).mock(return_value=httpx.Response(200, text=main_page({"Аналитика данных": 1})))
    respx.get(f"{MAIN}analitika-dannykh/").mock(return_value=httpx.Response(200, text=page))
    with pytest.raises(RuntimeError, match="layout"):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_raises_when_no_direction_has_any_vacancy():
    mock_simple({"analitika-dannykh": [], "razrabotka": []}, counts={"Аналитика данных": 3})
    with pytest.raises(RuntimeError, match="no vacancies"):
        await run()


@respx.mock
async def test_empty_direction_among_others_is_fine():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)], "razrabotka": []},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    assert [p.external_id for p in await run()] == ["7"]


@respx.mock
async def test_raises_when_main_page_has_no_counts():
    mock_simple({"analitika-dannykh": [item(url_id=7)]}, counts={})
    with pytest.raises(RuntimeError, match="section counts"):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_raises_when_direction_page_lists_fewer_than_main_page_counts():
    # a truncated direction page would silently "close" the missing vacancies
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        counts={"Аналитика данных": 3},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    with pytest.raises(RuntimeError, match="Аналитика данных.*1.*3"):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_count_cross_check_tolerates_one_just_published_vacancy():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        counts={"Аналитика данных": 2},  # published between the main and the direction request
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    assert [p.external_id for p in await run(params(directions=["analitika-dannykh"]))] == ["7"]


@pytest.mark.parametrize("flag", [True, False])
@respx.mock
async def test_require_level_keyword_is_passed_to_every_posting(flag):
    mock_fixture()
    postings = await run(params(require_level_keyword=flag))
    assert postings and all(p.require_level_keyword is flag for p in postings)


@respx.mock
async def test_require_level_keyword_defaults_to_off():
    mock_fixture()
    assert not any(p.require_level_keyword for p in await run())


@respx.mock
async def test_raises_when_no_card_has_a_format_label():
    items = [item(vid=i, url_id=i, fmt="") for i in (1, 2, 3)]
    mock_simple(
        {"analitika-dannykh": items},
        details={f"/vacancies/analitika-dannykh/{i}/": job_page() for i in (1, 2, 3)},
    )
    with pytest.raises(RuntimeError, match="format"):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_format_label_missing_on_some_cards_or_few_cards_is_allowed():
    items = [item(vid=1, url_id=1, fmt=""), item(vid=2, url_id=2), item(vid=3, url_id=3)]
    mock_simple(
        {"analitika-dannykh": items},
        details={f"/vacancies/analitika-dannykh/{i}/": job_page() for i in (1, 2, 3)},
    )
    assert len(await run(params(directions=["analitika-dannykh"]))) == 3
    respx.reset()
    mock_simple(
        {"analitika-dannykh": [item(vid=1, url_id=1, fmt="")]},
        details={"/vacancies/analitika-dannykh/1/": job_page()},
    )
    assert len(await run(params(directions=["analitika-dannykh"]))) == 1


@respx.mock
async def test_paces_requests(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("vacancy_bot.sources.avito.asyncio.sleep", fake_sleep)
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    await run(params(directions=["analitika-dannykh"]), delay=0.5)
    assert sleeps == [0.5, 0.5]  # main -> direction -> detail: no sleep before the first request


@respx.mock
async def test_all_directions_scanned_when_none_kept():
    mock_simple({"analitika-dannykh": [item(url_id=7)]})
    assert await run(params(directions=["analitika-dannykh"], keep_directions=[])) == []


@respx.mock
async def test_raises_when_a_card_loses_its_id_attribute():
    cards = [item(vid=1, url_id=1), item(vid=2, url_id=2).replace(' data-vacancy-id="2"', "")]
    mock_simple({"analitika-dannykh": cards}, counts={"Аналитика данных": 2})
    with pytest.raises(RuntimeError, match="avito: malformed direction page.*layout"):
        await run(params(directions=["analitika-dannykh"]))


@respx.mock
async def test_raises_when_configured_section_has_count_but_no_cards():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)], "razrabotka": []},
        counts={"Аналитика данных": 1, "Разработка": 15},
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    cfg = params(sections={"analitika-dannykh": "Аналитика данных", "razrabotka": "Разработка"})
    with pytest.raises(RuntimeError, match="Разработка.*0.*15"):
        await run(cfg)


@respx.mock
async def test_section_of_unconfigured_direction_is_skipped():
    mock_simple(
        {"analitika-dannykh": [item(url_id=7)]},
        counts={"Аналитика данных": 1, "Продажи": 33},  # prodazhi is not a configured direction
        details={"/vacancies/analitika-dannykh/7/": job_page()},
    )
    cfg = params(
        directions=["analitika-dannykh"],
        sections={"analitika-dannykh": "Аналитика данных", "prodazhi": "Продажи"},
    )
    assert len(await run(cfg)) == 1
