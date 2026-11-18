import asyncio
from datetime import datetime

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
        published_at=datetime.fromisoformat(published) if published else None,
        raw=item,
    )


class HHSource:
    name = "hh"

    def __init__(
        self, http: httpx.AsyncClient, user_agent: str, params: dict, delay: float = 0.3
    ) -> None:
        self._http = http
        self._user_agent = user_agent
        self._params = params
        self._delay = delay

    async def fetch(self) -> list[Posting]:
        per_page = self._params.get("per_page", 100)
        max_pages = self._params.get("max_pages", 20)
        found: dict[str, Posting] = {}
        first_request = True
        for query in self._params["queries"]:
            page = 0
            while True:
                if not first_request:
                    await asyncio.sleep(self._delay)
                first_request = False
                request_params = {
                    "text": query["text"],
                    "search_field": query.get("search_field", "name"),
                    "per_page": per_page,
                    "page": page,
                }
                if query.get("experience"):
                    request_params["experience"] = query["experience"]
                resp = await self._http.get(
                    API_URL, params=request_params, headers={"HH-User-Agent": self._user_agent}
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("items", []):
                    posting = _map(item)
                    found.setdefault(posting.external_id, posting)
                page += 1
                if page >= min(data.get("pages", 1), max_pages):
                    break
        return list(found.values())
