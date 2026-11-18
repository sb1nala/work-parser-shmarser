"""Sber careers source.

The careers site (https://rabota.sber.ru/search/) is a Next.js app whose list is
filled client-side from a public JSON gateway (found in the browser network
panel; plain GET, no cookies/tokens, no JavaScript execution needed):

    GET https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1/publications
        ?skip=0&take=100&profAreas=<id>&profAreas=<id>...

* take is capped at 100 by the server (101 and 500 both return 100).
* profAreas (repeatable; comma-joined does not work) filters by professional
  area; ids come from GET .../api/v1/profAreas (config: sources.sber.prof_areas).
  Other filters exist (experiences=<id>, schedules=<id>) but are not used: the
  bot filters on its own. There is no free-text search parameter.
* Response: {"success": true, "data": {"vacancies": [...], "total": N}}. Offset
  pagination: advance skip by the number of items received until skip >= total.
  The unfiltered list is ~2400 vacancies; the configured areas give ~350.

A vacancy has: internalId (stable numeric id), requisitionId (uuid), title,
company ("ПАО Сбербанк" etc.), publicationDate (ISO, UTC), introduction, duties,
requirements, conditions (markdown-ish text), city ("г Москва"), region,
specialization, salary_min/salary_max (often null), workScheduleId and
experienceId. The id lookups are GET .../api/v1/schedules (1 "Полный день",
2 "Сменный график", 3 "Гибкий график", 4 "Удаленная работа", 5 "Вахта") and
.../api/v1/experiences ("Без опыта", "От 1 года до 3 лет", "От 3 до 6 лет",
"Более 6 лет"); the uuids are mapped below. There is no employment-type field
(internships are only recognisable by title), so employment stays unknown.
Page of a vacancy: https://rabota.sber.ru/search/{internalId}/ (redirects to the
canonical slug URL).
"""

import asyncio
from datetime import datetime

import httpx

from vacancy_bot.domain import Posting, WorkFormat

API_URL = "https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1/publications"
SITE_URL = "https://rabota.sber.ru/search"
COMPANY = "Сбер"

_REMOTE_SCHEDULE_ID = 4  # "Удаленная работа"; other schedules say nothing about the place
_EXPERIENCE = {
    "4a14da73-ed71-43f0-aa42-0c5ffe5033e7": 0,  # Без опыта
    "07544111-3b15-475c-9980-1d0fa3d031c5": 1,  # От 1 года до 3 лет
    "7d2adf16-4635-4f0f-bf6e-f6089a702b5d": 3,  # От 3 до 6 лет
    "17b55c6a-0559-499b-aa2c-d76559d46dc0": 6,  # Более 6 лет
}
_DESCRIPTION_FIELDS = ("introduction", "duties", "requirements", "conditions")


def _city(item: dict) -> tuple[str, ...]:
    city = (item.get("city") or "").strip()
    if city.startswith("г "):
        city = city[2:].strip()
    return (city,) if city else ()


def _map(item: dict) -> Posting:
    internal_id = str(item["internalId"])
    published = item.get("publicationDate")
    description = "\n\n".join(
        item[f].strip() for f in _DESCRIPTION_FIELDS if (item.get(f) or "").strip()
    )
    return Posting(
        source="sber",
        external_id=internal_id,
        url=f"{SITE_URL}/{internal_id}/",
        company=COMPANY,
        title=item["title"],
        cities=_city(item),
        work_formats=(
            frozenset({WorkFormat.REMOTE})
            if item.get("workScheduleId") == _REMOTE_SCHEDULE_ID
            else frozenset()
        ),
        employment=None,
        experience_years_min=_EXPERIENCE.get(item.get("experienceId")),
        description=description,
        published_at=datetime.fromisoformat(published) if published else None,
        raw=item,
    )


class SberSource:
    name = "sber"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay

    async def fetch(self) -> list[Posting]:
        take = self._params.get("take", 100)
        max_pages = self._params.get("max_pages", 10)
        query: list[tuple[str, str | int]] = [("take", take)]
        query += [("profAreas", area) for area in self._params["prof_areas"]]
        found: dict[str, Posting] = {}
        skip = 0
        for page in range(max_pages):
            if page:
                await asyncio.sleep(self._delay)
            resp = await self._http.get(API_URL, params=[("skip", skip), *query])
            resp.raise_for_status()
            try:
                data = resp.json()
                if data.get("success") is not True:
                    raise ValueError(f"success {data.get('success')!r}")
                items = data["data"]["vacancies"]
                total = data["data"]["total"]
                if not isinstance(total, int) or not isinstance(items, list):
                    raise ValueError("total/vacancies have unexpected types")
                postings = [_map(item) for item in items]
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise RuntimeError(
                    f"sber: malformed publications response at skip {skip}: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                ) from exc
            for posting in postings:
                found.setdefault(posting.external_id, posting)
            skip += len(items)
            if skip >= total:
                return list(found.values())
            if not items:
                raise RuntimeError(
                    f"sber: empty page at skip {skip} before total {total} was reached"
                )
        raise RuntimeError(
            f"sber: not finished after max_pages={max_pages} (skip {skip}); "
            "raise sources.sber.max_pages/take"
        )
