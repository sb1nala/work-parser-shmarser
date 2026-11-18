"""Alfa-Bank careers source (job.alfabank.ru).

job.alfabank.ru is a Next.js site whose list is filled from its own public JSON
API (found in the page's JS chunks and checked with plain requests; no cookies,
tokens or JavaScript execution needed, certifi is enough for TLS):

    GET https://job.alfabank.ru/api/vacancies?take=100&skip=0
        &businessLine=1022&businessLine=1018
    GET https://job.alfabank.ru/api/vacancies/options?listId=cities&take=2000
        &businessLine=1022&businessLine=1018

* businessLine (repeatable, OR) filters by direction; ids come from
  .../options?listId=business_lines (config: sources.alfa.business_lines). Used:
  1022 "Аналитика", 1018 "Data". The unfiltered list is ~2150 vacancies, ~1170
  of them couriers and the rest sales/retail, so the bot asks only for these
  two. An `experience` filter exists but is deliberately not used: the project
  filter decides, so its rejection reasons stay visible.
* take is accepted up to at least 500. Offset pagination: advance skip by the
  number of items received until skip >= total.
* Response: {"total": N, "totalInternal": 0, "take": N, "items": [...]}.
* The options endpoint returns {"optionLists": {"cities": [{"id", "text"}]}} and
  honours the same filters, so one call gives the names of the cities in use.

A vacancy has: id (stable numeric string), name, slug ("/moskva/remote-job/..._id"),
cityId (code such as "0100", resolved through the cities list; "location_2" and
unknown codes stay unresolved), archetypeId (work format: 1001 Офис, 1002 "Гибридный -
больше в офисе", 1003 "Гибридный - больше удалённо", 1004 Удалённо), experienceId
(custom_voc_5_entry_1 "Без опыта", _2 "До 1 года", _5 "1-3 года", _3 "1-5 лет",
_6 "3-5 лет", _4 "Более 5 лет"), businessLineId, description (HTML) and
descriptionText/duties/requirements/conditions (plain text), createdAt (ISO, UTC),
and recruiter contact fields (contactFullName/Phone/Email), which are dropped from
`raw` on purpose. There is no employment-type field, so employment stays unknown.
Page of a vacancy: https://job.alfabank.ru/vacancies{slug}.

Internships ("I Choose Alfa", Alfa Campus, ...) are NOT on this API: they live on
alfabank.ru/alfafuture/ichoosealfa/, behind a servicepipe.tech JS challenge. They are
intentionally not scraped; this source only catches junior/no-experience analytics
and data roles posted on the regular careers site (currently there are none).
"""

import asyncio
from datetime import datetime

import httpx

from vacancy_bot.domain import Posting, WorkFormat

API_URL = "https://job.alfabank.ru/api/vacancies"
OPTIONS_URL = "https://job.alfabank.ru/api/vacancies/options"
SITE_URL = "https://job.alfabank.ru/vacancies"
COMPANY = "Альфа-Банк"

_WORK_FORMATS = {
    "1001": WorkFormat.OFFICE,
    "1002": WorkFormat.HYBRID,
    "1003": WorkFormat.HYBRID,
    "1004": WorkFormat.REMOTE,
}
_EXPERIENCE = {
    "custom_voc_5_entry_1": 0,  # Без опыта
    "custom_voc_5_entry_2": 0,  # До 1 года
    "custom_voc_5_entry_5": 1,  # 1-3 года
    "custom_voc_5_entry_3": 1,  # 1-5 лет
    "custom_voc_5_entry_6": 3,  # 3-5 лет
    "custom_voc_5_entry_4": 5,  # Более 5 лет
}


def _map(item: dict, cities: dict[str, str]) -> Posting:
    created = item.get("createdAt")
    work_format = _WORK_FORMATS.get(item.get("archetypeId"))
    city = cities.get(item.get("cityId"))
    return Posting(
        source="alfa",
        external_id=str(item["id"]),
        url=f"{SITE_URL}{item['slug']}",
        company=COMPANY,
        title=item["name"],
        cities=(city,) if city else (),
        work_formats=frozenset({work_format}) if work_format else frozenset(),
        employment=None,
        experience_years_min=_EXPERIENCE.get(item.get("experienceId")),
        description=(item.get("descriptionText") or "").strip(),
        published_at=datetime.fromisoformat(created) if created else None,
        raw={k: v for k, v in item.items() if not k.startswith("contact")},
    )


class AlfaSource:
    name = "alfa"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay

    async def _cities(self, lines: list[tuple[str, str]]) -> dict[str, str]:
        resp = await self._http.get(
            OPTIONS_URL, params=[("listId", "cities"), ("take", 2000), *lines]
        )
        resp.raise_for_status()
        try:
            options = resp.json()["optionLists"]["cities"]
            return {str(o["id"]): str(o["text"]) for o in options}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"alfa: malformed cities response: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc

    async def fetch(self) -> list[Posting]:
        take = self._params.get("take", 100)
        max_pages = self._params.get("max_pages", 10)
        lines = [("businessLine", str(b)) for b in self._params["business_lines"]]
        cities = await self._cities(lines)
        found: dict[str, Posting] = {}
        skip = 0
        for page in range(max_pages):
            await asyncio.sleep(self._delay)
            resp = await self._http.get(
                API_URL, params=[("take", take), ("skip", skip), *lines]
            )
            resp.raise_for_status()
            try:
                data = resp.json()
                items = data["items"]
                total = data["total"]
                if not isinstance(total, int) or not isinstance(items, list):
                    raise ValueError("total/items have unexpected types")
                postings = [_map(item, cities) for item in items]
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise RuntimeError(
                    f"alfa: malformed vacancies response at skip {skip}: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                ) from exc
            for posting in postings:
                found.setdefault(posting.external_id, posting)
            skip += len(items)
            if skip >= total:
                return list(found.values())
            if not items:
                raise RuntimeError(
                    f"alfa: empty page at skip {skip} before total {total} was reached"
                )
        raise RuntimeError(
            f"alfa: not finished after max_pages={max_pages} (skip {skip}); "
            "raise sources.alfa.max_pages/take"
        )
