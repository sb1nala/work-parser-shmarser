"""Yandex careers source (yandex.ru/jobs).

yandex.ru/jobs is a React app (server-rendered state embedded in the page) that
fills its vacancy list from a public JSON API of the "jobs" service. Found in the
page's embedded filter state and checked with plain requests: no cookies, tokens
or JavaScript execution needed, no captcha was served for the API or the pages
(the TLS chain is a public CA, certifi verifies it):

    GET https://yandex.ru/jobs/api/publications
        ?professions=<slug>&professions=<slug>...&pro_levels=intern&pro_levels=junior
        &page_size=40[&cursor=<opaque>]
    GET https://yandex.ru/jobs/api/publications/<id>

* professions (repeatable, OR) filters by public profession slug; the slugs come
  from the filter state embedded in https://yandex.ru/jobs/vacancies
  (config: sources.yandex.professions). pro_levels (repeatable, OR; intern, junior,
  middle, senior) matches a vacancy whose grade range starts or ends at that level,
  so asking for intern+junior returns the entry-level roles only. Other filters
  (cities, work_modes, employment_types, skills, public_services) are not used: the
  bot filters on its own.
* page_size is capped at 40 by the server. Pagination is cursor based: the response
  carries "next" (a relative URL, null/absent on the last page; its host/path are
  not reused, they point at a redirecting alias) whose `cursor` query value is sent
  back as `cursor=`. The unfiltered list is ~290 publications; the configured
  professions give ~40, the entry-level subset a handful.
* List response: {"count": N, "next": ..., "results": [{"id", "publication_slug_url",
  "title", "short_summary", "modified", "vacancy": {"id", "cities", "skills",
  "work_modes"}, ...}], "counters": {...}}. The list does not say the grade, so every
  publication found is then fetched one by one from the detail endpoint (a polite
  delay between requests; the list is short because of the filters).
* Detail response: id, publication_slug_url, title, published_at ("YYYY-MM-DD"),
  modified, description / key_qualifications / duties / conditions /
  additional_requirements (text with a few HTML tags), vacancy.cities
  ([{"slug", "name"}]), vacancy.work_modes ([{"slug": office|mixed|remote}]),
  vacancy.employment_types (office / remote / intern), vacancy.pro_level_min_display
  and pro_level_max_display ("publications.pro_level.<level>"), vacancy.profession,
  public_service. `contacts` (recruiter data, empty in practice) is never copied.
  `raw` keeps only an allowlist of non-personal keys.
Page of a vacancy: https://yandex.ru/jobs/vacancies/{publication_slug_url}.

Internship programmes (Young&&Yandex, yandex.ru/yaintern) are pages, not a list:
their openings are ordinary publications of this API with the intern grade, so
they are caught by pro_levels=intern.
"""

import asyncio
import html
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from vacancy_bot.domain import Employment, Posting, WorkFormat

API_URL = "https://yandex.ru/jobs/api/publications"
SITE_URL = "https://yandex.ru/jobs/vacancies"
COMPANY = "Яндекс"

_WORK_FORMATS = {
    "office": WorkFormat.OFFICE,
    "mixed": WorkFormat.HYBRID,
    "remote": WorkFormat.REMOTE,
}
_EXPERIENCE = {  # lower bound of the stated grade range, years
    "intern": 0,
    "junior": 0,
    "middle": 1,
    "senior": 3,
    "chief": 5,
}
_LEVEL_PREFIX = "publications.pro_level."
_DESCRIPTION_FIELDS = (
    "description", "key_qualifications", "additional_requirements", "duties", "conditions",
)  # fmt: skip
_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>|</p>|</li>", re.IGNORECASE)
_BLANKS = re.compile(r"\n{3,}")


def _text(value: str | None) -> str:
    if not value:
        return ""
    value = _TAG.sub("", _BR.sub("\n", value.replace("\r\n", "\n")))
    return _BLANKS.sub("\n\n", html.unescape(value)).strip()


def _published(day: str | None) -> datetime | None:
    if not day:
        return None
    dt = datetime.fromisoformat(day)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # the API speaks UTC


def _names(vacancy: dict, key: str) -> list[dict]:
    value = vacancy.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"vacancy.{key} is not a list")
    return value


def _level(vacancy: dict) -> int | None:
    shown = vacancy.get("pro_level_min_display")
    if not isinstance(shown, str) or not shown.startswith(_LEVEL_PREFIX):
        return None
    return _EXPERIENCE.get(shown[len(_LEVEL_PREFIX) :])


def _map(item: dict) -> Posting:
    slug = item["publication_slug_url"]
    vacancy = item["vacancy"]
    employment_types = vacancy.get("employment_types") or []
    if not isinstance(employment_types, list):
        raise ValueError("vacancy.employment_types is not a list")
    formats = {
        _WORK_FORMATS[m["slug"]]
        for m in _names(vacancy, "work_modes")
        if m["slug"] in _WORK_FORMATS
    }
    if "remote" in employment_types:
        formats.add(WorkFormat.REMOTE)
    names = (c["name"].strip() for c in _names(vacancy, "cities"))
    cities = tuple(dict.fromkeys(n for n in names if n))
    is_intern = "intern" in employment_types or (
        vacancy.get("pro_level_min_display") == f"{_LEVEL_PREFIX}intern"
    )
    description = "\n\n".join(t for f in _DESCRIPTION_FIELDS if (t := _text(item.get(f))))
    service = item.get("public_service") or {}
    profession = vacancy.get("profession") or {}
    return Posting(
        source="yandex",
        external_id=str(item["id"]),
        url=f"{SITE_URL}/{slug}",
        company=COMPANY,
        title=item["title"],
        cities=cities,
        work_formats=frozenset(formats),
        employment=Employment.INTERNSHIP if is_intern else None,
        experience_years_min=_level(vacancy),
        description=description,
        published_at=_published(item.get("published_at")),
        raw={
            "id": item["id"],
            "publication_slug_url": slug,
            "title": item["title"],
            "published_at": item.get("published_at"),
            "modified": item.get("modified"),
            "profession": profession.get("slug"),
            "pro_level_min": vacancy.get("pro_level_min_display"),
            "pro_level_max": vacancy.get("pro_level_max_display"),
            "employment_types": list(employment_types),
            "work_modes": [m["slug"] for m in _names(vacancy, "work_modes")],
            "cities": [c["slug"] for c in _names(vacancy, "cities") if "slug" in c],
            "service": service.get("slug"),
        },
    )


class YandexSource:
    name = "yandex"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay
        # entry-level openings come and go by season; a layout change raises in fetch()
        self.empty_alert_after_hours: int | None = params.get("empty_alert_after_hours")

    async def _list_ids(self) -> list[int]:
        page_size = self._params.get("page_size", 40)
        max_pages = self._params.get("max_pages", 10)
        query: list[tuple[str, str | int]] = [("page_size", page_size)]
        query += [("professions", p) for p in self._params["professions"]]
        query += [("pro_levels", lv) for lv in self._params.get("pro_levels") or []]
        ids: dict[int, None] = {}
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for page in range(max_pages):
            if page:
                await asyncio.sleep(self._delay)
            extra = [("cursor", cursor)] if cursor else []
            resp = await self._http.get(API_URL, params=[*query, *extra])
            resp.raise_for_status()
            try:
                data = resp.json()
                results = data["results"]
                nxt = data.get("next")
                if not isinstance(results, list) or not isinstance(nxt, str | None):
                    raise ValueError("results/next have unexpected types")
                page_ids = [item["id"] for item in results]
                if not all(isinstance(i, int) for i in page_ids):
                    raise ValueError("publication id is not an integer")
                next_cursor = None
                if nxt:
                    values = parse_qs(urlparse(nxt).query).get("cursor")
                    if not values or not values[0]:
                        raise ValueError("next has no cursor")
                    next_cursor = values[0]
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise RuntimeError(
                    f"yandex: malformed publications response at page {page}: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                ) from exc
            ids.update(dict.fromkeys(page_ids))
            if next_cursor is None:
                return list(ids)
            if not page_ids:
                raise RuntimeError(f"yandex: empty page {page} before the end of the list")
            if next_cursor in seen_cursors:
                raise RuntimeError(f"yandex: cursor did not advance at page {page}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(
            f"yandex: not finished after max_pages={max_pages}; "
            "raise sources.yandex.max_pages/page_size"
        )

    async def _detail(self, pub_id: int) -> Posting:
        resp = await self._http.get(f"{API_URL}/{pub_id}")
        resp.raise_for_status()
        try:
            data = resp.json()
            if data["id"] != pub_id:
                raise ValueError(f"asked for {pub_id}, got {data['id']!r}")
            return _map(data)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"yandex: malformed publication {pub_id}: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc

    async def fetch(self) -> list[Posting]:
        found: dict[str, Posting] = {}
        for pub_id in await self._list_ids():
            await asyncio.sleep(self._delay)
            posting = await self._detail(pub_id)
            found.setdefault(posting.external_id, posting)
        return list(found.values())
