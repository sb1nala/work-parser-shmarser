"""VTB careers source (https://rabota-vtb.ru/career).

The official careers site (rabota-vtb.ru, linked from vtb.ru) is a Next.js app whose
vacancy list is the bank's own hh.ru employer feed (employer 4181, "Банк ВТБ (ПАО)"),
served by its backend-for-frontend. Found in the page bundle (SWR key
"/vtb-federal-campaign/vacancies", base "https://backend.honeyleads.ru"); it is open,
needs no cookies or tokens (the site's JSON is also embedded in its __NEXT_DATA__) and
sits behind no anti-bot challenge. Its certificate verifies with certifi.

    GET https://backend.honeyleads.ru/vtb-federal-campaign/vacancies?page=0&text=...

Params: `page` (0-based) and the hh.ru vacancy-search filters the site itself uses:
`text` (hh query language: OR, "phrases"; matched against the title, "ё"/"е" are
interchangeable), `experience` (noExperience, ...). `per_page` is ignored: a page is
always 20 items. Unknown params (internship, professional_role, employment_form) are
silently ignored and return everything, so only text/experience narrow the result.
Without filters ~1100 vacancies (56 pages) are open, mostly retail/contact-centre.
Each query in config is sent as is; results of all queries are merged by id.

Response: {"items": [...], "found": N, "pages": N, "page": N, "per_page": 20, ...}.
The end is `page + 1 >= pages`; a page past the end is an empty list.
An item has the hh.ru shape: id, name, area{name} (city), alternate_url (hh.ru page),
published_at ("2026-11-18T17:51:33+0300"), work_format[{id: ON_SITE | HYBRID | REMOTE |
FIELD_WORK}], experience{id: noExperience | between1And3 | between3And6 | moreThan6},
employment_form{id: FULL | PART | PROJECT | ...}, internship (bool), snippet
{requirement, responsibility} (short texts, no full description), employer, salary.
Page of a vacancy on the site: https://rabota-vtb.ru/career/{id}.
"""

import asyncio
from datetime import UTC, datetime

import httpx

from vacancy_bot.domain import Employment, Posting, WorkFormat

API_URL = "https://backend.honeyleads.ru/vtb-federal-campaign/vacancies"
SITE_URL = "https://rabota-vtb.ru/career"
COMPANY = "ВТБ"

_WORK_FORMATS = {
    "ON_SITE": WorkFormat.OFFICE,
    "HYBRID": WorkFormat.HYBRID,
    "REMOTE": WorkFormat.REMOTE,
}  # FIELD_WORK (travelling) is none of the three: left unknown, like tbank "Разъездной"
_EMPLOYMENT = {"FULL": Employment.FULL, "PART": Employment.PART, "PROJECT": Employment.PROJECT}
_EXPERIENCE = {"noExperience": 0, "between1And3": 1, "between3And6": 3, "moreThan6": 6}


def _map(item: dict) -> Posting:
    snippet = item.get("snippet") or {}
    description = "\n".join(
        part for part in (snippet.get("requirement"), snippet.get("responsibility")) if part
    )
    published = item.get("published_at")
    published_at = datetime.fromisoformat(published) if published else None
    if published_at and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    if item.get("internship") is True:
        employment = Employment.INTERNSHIP
    else:
        employment = _EMPLOYMENT.get((item.get("employment_form") or {}).get("id"))
    area = item.get("area") or {}
    external_id = str(item["id"])
    return Posting(
        source="vtb",
        external_id=external_id,
        url=f"{SITE_URL}/{external_id}",
        company=COMPANY,
        title=item["name"],
        cities=((area["name"],) if area.get("name") else ()),
        work_formats=frozenset(
            _WORK_FORMATS[w["id"]]
            for w in item.get("work_format") or []
            if w.get("id") in _WORK_FORMATS
        ),
        employment=employment,
        experience_years_min=_EXPERIENCE.get((item.get("experience") or {}).get("id")),
        description=description,
        published_at=published_at,
        raw=item,
    )


class VTBSource:
    name = "vtb"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay
        self._requests = 0

    async def _get_page(
        self, query: dict, page: int
    ) -> tuple[list[Posting], int]:
        """One page of a query: (postings, total pages). Raises RuntimeError if malformed."""
        if self._requests:
            await asyncio.sleep(self._delay)
        self._requests += 1
        resp = await self._http.get(API_URL, params={**query, "page": page})
        resp.raise_for_status()
        try:
            data = resp.json()
            items = data["items"]
            pages = data["pages"]
            number = data["page"]
            if not (isinstance(items, list) and isinstance(pages, int)):
                raise ValueError("items/pages have unexpected types")
            if not isinstance(number, int):
                raise ValueError("page has an unexpected type")
            postings = [_map(item) for item in items]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"vtb: malformed vacancies response for query {query.get('text')!r} "
                f"page {page}: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc
        if number != page or (not postings and page + 1 < pages):
            raise RuntimeError(
                f"vtb: pagination did not advance for query {query.get('text')!r} at page "
                f"{page} (got page {number}, {len(postings)} items, {pages} pages)"
            )
        return postings, pages

    async def fetch(self) -> list[Posting]:
        max_pages = self._params.get("max_pages", 10)
        found: dict[str, Posting] = {}
        self._requests = 0
        for query in self._params["queries"]:
            for page in range(max_pages):
                postings, pages = await self._get_page(query, page)
                for posting in postings:
                    found.setdefault(posting.external_id, posting)
                if page + 1 >= pages:
                    break
            else:
                raise RuntimeError(
                    f"vtb: query {query.get('text')!r} not finished after "
                    f"max_pages={max_pages}; raise sources.vtb.max_pages or narrow the query"
                )
        return list(found.values())
