"""hh.ru source: GET https://api.hh.ru/vacancies (needs an app token since April 2026).

hh caps any search at 2000 results: `pages` never exceeds 2000 / per_page and items
past the cap cannot be fetched at all, while `found` still reports the full count.
The pipeline closes postings missing from a successful fetch, so a truncated list
would read as closures (and later as false reopenings). fetch() therefore raises
instead of returning a partial list when a query is truncated by the cap or by
`max_pages`; the fix is a narrower query (area, work_format, ...) in config.

Each query in config is sent as is (lists become repeated params, e.g. area: [1, 1783]);
`search_field` defaults to "name".
"""

import asyncio
from datetime import UTC, datetime

import httpx

from vacancy_bot.domain import Employment, Posting, WorkFormat

API_URL = "https://api.hh.ru/vacancies"

_WORK_FORMATS = {
    "REMOTE": WorkFormat.REMOTE,
    "HYBRID": WorkFormat.HYBRID,
    "ON_SITE": WorkFormat.OFFICE,
    "FIELD_WORK": WorkFormat.OFFICE,
}
_EMPLOYMENT = {
    "full": Employment.FULL,
    "part": Employment.PART,
    "probation": Employment.INTERNSHIP,
    "project": Employment.PROJECT,
    "volunteer": Employment.OTHER,
}
_EXPERIENCE = {"noExperience": 0, "between1And3": 1, "between3And6": 3, "moreThan6": 6}


def _work_formats(item: dict) -> frozenset[WorkFormat]:
    if item.get("work_format"):
        return frozenset(
            _WORK_FORMATS[w["id"]] for w in item["work_format"] if w.get("id") in _WORK_FORMATS
        )
    schedule = item.get("schedule")
    if not schedule or not schedule.get("id"):
        return frozenset()
    return frozenset({WorkFormat.REMOTE if schedule["id"] == "remote" else WorkFormat.OFFICE})


def _map(item: dict) -> Posting:
    snippet = item.get("snippet") or {}
    description = "\n".join(
        part for part in (snippet.get("requirement"), snippet.get("responsibility")) if part
    )
    published = item.get("published_at")
    published_at = datetime.fromisoformat(published) if published else None
    if published_at and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return Posting(
        source="hh",
        external_id=str(item["id"]),
        url=item["alternate_url"],
        company=(item.get("employer") or {}).get("name") or "",
        title=item["name"],
        cities=((item["area"]["name"],) if item.get("area") else ()),
        work_formats=_work_formats(item),
        employment=_EMPLOYMENT.get((item.get("employment") or {}).get("id")),
        experience_years_min=_EXPERIENCE.get((item.get("experience") or {}).get("id")),
        description=description,
        published_at=published_at,
        raw=item,
    )


class HHSource:
    name = "hh"

    def __init__(
        self,
        http: httpx.AsyncClient,
        user_agent: str,
        params: dict,
        delay: float = 0.3,
        access_token: str | None = None,
    ) -> None:
        self._http = http
        self._params = params
        self._delay = delay
        self._headers = {"HH-User-Agent": user_agent}
        if access_token:
            self._headers["Authorization"] = f"Bearer {access_token}"
        self._requests = 0

    async def _get_page(
        self, query: dict, per_page: int, page: int
    ) -> tuple[list[Posting], int, int]:
        """One page of a query: (postings, found, pages). Raises RuntimeError if malformed."""
        if self._requests:
            await asyncio.sleep(self._delay)
        self._requests += 1
        request_params = {"search_field": "name", **query, "per_page": per_page, "page": page}
        resp = await self._http.get(API_URL, params=request_params, headers=self._headers)
        resp.raise_for_status()
        try:
            data = resp.json()
            items = data["items"]
            found = data["found"]
            pages = data["pages"]
            if not (
                isinstance(items, list) and isinstance(found, int) and isinstance(pages, int)
            ):
                raise ValueError("items/found/pages have unexpected types")
            postings = [_map(item) for item in items]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"hh: malformed vacancies response for query {query.get('text')!r} page {page}: "
                f"{type(exc).__name__}: {str(exc)[:100]}"
            ) from exc
        return postings, found, pages

    async def fetch(self) -> list[Posting]:
        per_page = self._params.get("per_page", 100)
        max_pages = self._params.get("max_pages", 20)
        found: dict[str, Posting] = {}
        self._requests = 0
        for query in self._params["queries"]:
            text = query.get("text")
            postings, total, pages = await self._get_page(query, per_page, 0)
            if total > pages * per_page:
                raise RuntimeError(
                    f"hh: query {text!r} found {total} vacancies but hh returns at most "
                    f"{pages * per_page} ({pages} pages of {per_page}); narrow the query "
                    "(area, work_format, experience) in sources.hh.queries"
                )
            if pages > max_pages:
                raise RuntimeError(
                    f"hh: query {text!r} has {pages} pages, more than max_pages={max_pages}; "
                    "raise sources.hh.max_pages or narrow the query"
                )
            for page in range(1, pages):
                more, _, _ = await self._get_page(query, per_page, page)
                postings += more
            for posting in postings:
                found.setdefault(posting.external_id, posting)
        return list(found.values())
