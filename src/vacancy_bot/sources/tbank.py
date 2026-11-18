"""T-Bank careers source.

The careers site (https://www.tbank.ru/career/vacancies/) is a JS app, but its
list comes from a plain JSON endpoint of its own backend-for-frontend (found via
the app bundle and the browser network panel, no JavaScript execution needed):

    POST https://www.tbank.ru/pfpjobs/papi/getVacancies
    Content-Type: application/json
    {
      "filters": {"generatedGraphQL": {
          "type": "T_CAREER", "status": "ACTIVE",
          "includeInternshipPublications": true,   # without it internships are hidden
          "userGroup": {"groups": ["control"], "type": "SPECIFIC"},
          "or": [{"category": "tcareer_it"}, {"category": "tcareer_back_office"}]
      }},
      "pagination": {"offset": 0},
      "limit": 100
    }

Without "or" the endpoint returns only category "tcareer_work_with_clients"
(client-facing jobs), so categories are listed in config (sources.tbank).
Response: {"resultCode": "OK", "payload": {"vacancies": [...], "nextPagination":
{"offset": N, "isFinished": bool, "totalCount": N}}}. Pagination is offset based:
send the returned nextPagination.offset until isFinished. limit 100 is accepted.

A vacancy has: title, subtitle (city, optional), category, shortDescription, tags
(grade "Стажер"/"Junior"/..., work format "Офис"/"Гибрид"/"Удаленный", experience
"Без опыта"/"До года"/"1 – 5 лет"/"Более 5 лет", schedule), urlSlug (id), seoSlug
and redirectUrl (set instead of seoSlug for some internship landings). There is no
publication date, full description, or salary in the list.
Page of a vacancy: /career/{it|back-office}/vacancy/{city}/{seoSlug}/{urlSlug}/
(the city segment does not affect resolving, "all" works).
"""

import asyncio

import httpx

from vacancy_bot.domain import Employment, Posting, WorkFormat

API_URL = "https://www.tbank.ru/pfpjobs/papi/getVacancies"
SITE_URL = "https://www.tbank.ru/career"
COMPANY = "Т-Банк"

_CATEGORY_PATH = {"tcareer_it": "it", "tcareer_back_office": "back-office"}
_WORK_FORMATS = {
    "Офис": WorkFormat.OFFICE,
    "Гибрид": WorkFormat.HYBRID,
    "Удаленный": WorkFormat.REMOTE,
}
_EXPERIENCE = {"Без опыта": 0, "До года": 0, "1 – 5 лет": 1, "Более 5 лет": 5}
_INTERNSHIP_GRADE = "Стажер"


def _url(item: dict) -> str:
    path = _CATEGORY_PATH.get(item.get("category"))
    if item.get("seoSlug") and path:
        return f"{SITE_URL}/{path}/vacancy/all/{item['seoSlug']}/{item['urlSlug']}/"
    return item.get("redirectUrl") or f"{SITE_URL}/vacancies/"


def _map(item: dict) -> Posting:
    tags = item.get("tags") or []
    experience = [_EXPERIENCE[t] for t in tags if t in _EXPERIENCE]
    return Posting(
        source="tbank",
        external_id=item["urlSlug"],
        url=_url(item),
        company=COMPANY,
        title=item["title"],
        cities=((item["subtitle"],) if item.get("subtitle") else ()),
        work_formats=frozenset(_WORK_FORMATS[t] for t in tags if t in _WORK_FORMATS),
        employment=Employment.INTERNSHIP if _INTERNSHIP_GRADE in tags else None,
        experience_years_min=experience[0] if experience else None,
        description=item.get("shortDescription") or "",
        published_at=None,
        raw=item,
    )


class TBankSource:
    name = "tbank"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay

    async def fetch(self) -> list[Posting]:
        limit = self._params.get("limit", 100)
        max_pages = self._params.get("max_pages", 20)
        graphql: dict = {
            "type": "T_CAREER",
            "status": "ACTIVE",
            "includeInternshipPublications": self._params.get("include_internships", True),
            "userGroup": {"groups": ["control"], "type": "SPECIFIC"},
            "or": [{"category": c} for c in self._params["categories"]],
        }
        found: dict[str, Posting] = {}
        offset = 0
        for page in range(max_pages):
            if page:
                await asyncio.sleep(self._delay)
            resp = await self._http.post(
                API_URL,
                json={
                    "filters": {"generatedGraphQL": graphql},
                    "pagination": {"offset": offset},
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("resultCode") != "OK":
                raise RuntimeError(f"T-Bank getVacancies failed: {data.get('resultCode')}")
            payload = data["payload"]
            for item in payload["vacancies"]:
                posting = _map(item)
                found.setdefault(posting.external_id, posting)
            pagination = payload["nextPagination"]
            if pagination["isFinished"] or not payload["vacancies"]:
                break
            offset = pagination["offset"]
        return list(found.values())
