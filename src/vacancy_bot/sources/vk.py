"""VK careers source (team.vk.company/vacancy + internship.vk.company).

Two public parts of VK's careers sites, both checked with plain requests: no cookies,
tokens, captcha or JavaScript execution needed (the pages load a captcha SDK script
but never served a challenge to these requests; the TLS chain is a public CA, certifi
verifies it).

1. Regular vacancies: the JSON API that the vacancy list of https://team.vk.company/vacancy/
   calls (found in the page's script chunk):

       GET https://team.vk.company/career/api/v2/vacancies/
           ?limit=50&offset=<n>&specialty=<id>

   * specialty takes ONE id (a second `specialty` is ignored, a comma list matches
     nothing), so every configured specialty is a separate query
     (config: sources.vk.specialties; the ids are in the page's embedded filter state,
     e.g. 295 Продуктовая аналитика, 269 Data-аналитика, 283 Machine Learning,
     281 Data Science, 268 BI аналитика, 316 Бизнес-аналитика). Other filters (town, tags,
     title, remote) are not used: the bot filters on its own. The unfiltered list is ~230
     vacancies, the configured specialties give ~47.
   * limit is capped at 50 by the server. Pagination is offset based: the response
     is {"count", "next", "previous", "results"}; "next" (an absolute http:// URL,
     null on the last page; its host is not reused) carries the `offset` of the next page.
   * List item: id, title, town ({"id","name"} or null), work_format ("Офисный",
     "Комбинированный", "Дистанционный" or "гибкий"), remote (bool), tags, group
     (project), prof_area, specialty, external_program, external_bonus. It carries no
     description, grade or employment.
   * Those come from the vacancy page, server-rendered HTML (the JSON API has no detail
     resource: /vacancies/<id>/ returns the same short item):
       GET https://team.vk.company/vacancy/<id>/
     schema.org JobPosting microdata (`itemprop="datePosted"` meta, the
     `itemprop="description"` article) and `<h4 class="vacancy-title">` blocks with
     `<div class="vacancy-tag">` values: "Уровень" (junior / middle / senior / lead / head,
     a range), "График работы" (полный), "Формат работы". One page is fetched for each
     listed vacancy (a polite delay between requests). The page is parsed with an HTML
     parser (by itemprop/class, not by attribute order). Layout-change guards: a page with
     the JobPosting marker where both the description and datePosted are missing raises;
     a missing "Уровень" block on one page is allowed (experience stays None), but if it
     is missing on EVERY page of a fetch (with at least 3 pages) the fetch raises. The
     list `count` is checked against the unique ids fetched per specialty.
   Vacancies of the regular site carry no explicit internship flag: the titles of internship
   roles are caught by the bot's own filter, and the "Уровень" tag marks juniors.

2. Internships and junior vacancies: https://internship.vk.company/internship and
   https://internship.vk.company/vacancy ("Стажировка VK", VK Education). These are
   Next.js pages that embed their whole vacancy list as JSON in
   `<script id="__NEXT_DATA__">` (props.pageProps.page.vacancies); the list is filtered
   on the client, there is no separate list API. A vacancy:
   {"id", "title", "business_unit" (-> page.units[].name), "direction"
   (-> page.directions[].name), "city" ("Любой" = anywhere, else a city), "format"
   (remote / hybrid / office), "employment" (full_time / part_time / contract), "skills"
   (ids -> page.skills[id].name), "date_work_starts", "date_work_ends" (free text),
   "is_opened", "internship_type" ("internship" / "junior"), "for_only_students"}.
   Closed vacancies (is_opened false) are skipped. The page of a vacancy is
   https://internship.vk.company/vacancy/<id>; ids are prefixed `intern-` so they cannot
   collide with the ids of the regular site. The state has no description, so a short one
   is composed from the stated fields only (project, direction, dates, skills). The pages
   are listed in config (sources.vk.portal_pages); when none is configured the portal is
   not requested. At the time of writing "internship" had 5 vacancies; "vacancy" had an
   empty list, and a missing/null `vacancies` key is treated as empty (a present value
   of the wrong type raises).

`raw` keeps only an allowlist of non-personal keys; no contact data is ever copied.
"""

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from vacancy_bot.domain import Employment, Posting, WorkFormat

API_URL = "https://team.vk.company/career/api/v2/vacancies/"
SITE_URL = "https://team.vk.company/vacancy"
PORTAL_URL = "https://internship.vk.company"
COMPANY = "VK"

_WORK_FORMATS = {  # list item work_format; "гибкий" (flexible) says nothing definite
    "офисный": WorkFormat.OFFICE,
    "комбинированный": WorkFormat.HYBRID,
    "дистанционный": WorkFormat.REMOTE,
}
_SCHEDULES = {
    "полный": Employment.FULL,
    "частичный": Employment.PART,
    "неполный": Employment.PART,
}
_EXPERIENCE = {  # lower bound of the stated grade tags, years
    "intern": 0,
    "стажер": 0,
    "junior": 0,
    "middle": 1,
    "senior": 3,
    "lead": 5,
    "head": 5,
}
_INTERN_LEVELS = {"intern", "стажер"}
_PORTAL_FORMATS = {
    "remote": WorkFormat.REMOTE,
    "hybrid": WorkFormat.HYBRID,
    "office": WorkFormat.OFFICE,
}
_PORTAL_EMPLOYMENT = {
    "full_time": Employment.FULL,
    "part_time": Employment.PART,
    "contract": Employment.PROJECT,
}
_JOB_POSTING = "schema.org/JobPosting"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<br\s*/?>|</(?:p|li|tr|td|h[1-6])>", re.IGNORECASE)
_SPACES = re.compile(r"[ \t\xa0]+")
_BLANKS = re.compile(r"\n\s*\n+")


def _norm(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def _text(value: str) -> str:
    value = _TAG.sub("", _BREAK.sub("\n", value.replace("\r\n", "\n")))
    lines = (_SPACES.sub(" ", line).strip() for line in html.unescape(value).split("\n"))
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _block(soup: BeautifulSoup, title: str) -> list[str] | None:
    """Tag values of the vacancy page block headed `title`; None when the block is absent."""
    for head in soup.find_all("h4", class_="vacancy-title"):
        if head.get_text(strip=True) == title:
            row = head.parent.parent if head.parent and head.parent.parent else head.parent
            if not isinstance(row, Tag) or row.name != "div":
                row = head.parent
            return [t for v in row.find_all(class_="vacancy-tag") if (t := _text(v.get_text()))]
    return None


def _experience(levels: list[str]) -> int | None:
    known = [_EXPERIENCE[n] for lv in levels if (n := _norm(lv)) in _EXPERIENCE]
    return min(known) if known else None


def _day(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # the site speaks UTC


def _name(value: object, what: str) -> str | None:
    """`name` of an optional {"id", "name"} object."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{what} is not an object")
    name = value.get("name")
    return name.strip() or None if isinstance(name, str) else None


def _map_vacancy(item: dict, page: str) -> Posting:
    soup = BeautifulSoup(page, "html.parser")
    if soup.find(attrs={"itemtype": re.compile(re.escape(_JOB_POSTING))}) is None:
        raise ValueError("page is not a vacancy (no JobPosting markup)")
    vacancy_id = item["id"]
    town = _name(item.get("town"), "town")
    formats = set()
    if (shown := _WORK_FORMATS.get(_norm(item.get("work_format") or ""))) is not None:
        formats.add(shown)
    if item.get("remote") is True:
        formats.add(WorkFormat.REMOTE)
    level_block = _block(soup, "Уровень")
    levels = level_block or []
    schedule = _block(soup, "График работы") or []
    schedules = [_SCHEDULES[n] for s in schedule if (n := _norm(s)) in _SCHEDULES]
    is_intern = any(_norm(lv) in _INTERN_LEVELS for lv in levels)
    meta = soup.find("meta", attrs={"itemprop": "datePosted"})
    posted = (meta.get("content") or "").strip() if isinstance(meta, Tag) else ""
    body = soup.find(attrs={"itemprop": "description"})
    description = _text(body.decode_contents()) if isinstance(body, Tag) else ""
    if not description and not posted:
        raise ValueError("neither description nor datePosted found (page layout changed?)")
    tags = item.get("tags") or []
    if not isinstance(tags, list):
        raise ValueError("tags is not a list")
    return Posting(
        source="vk",
        external_id=str(vacancy_id),
        url=f"{SITE_URL}/{vacancy_id}/",
        company=COMPANY,
        title=item["title"].strip(),
        cities=(town,) if town else (),
        work_formats=frozenset(formats),
        employment=Employment.INTERNSHIP if is_intern else (schedules[0] if schedules else None),
        experience_years_min=_experience(levels),
        description=description,
        published_at=_day(posted) if posted else None,
        raw={
            "id": vacancy_id,
            "title": item["title"].strip(),
            "specialty": _name(item.get("specialty"), "specialty"),
            "group": _name(item.get("group"), "group"),
            "prof_area": _name(item.get("prof_area"), "prof_area"),
            "town": town,
            "work_format": item.get("work_format"),
            "remote": item.get("remote"),
            "tags": [t["name"] for t in tags if isinstance(t, dict) and "name" in t],
            "levels": levels,
            "schedule": schedule,
            "date_posted": posted or None,
            "has_level_block": level_block is not None,
        },
    )


def _list(value: object, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} is not a list")
    return value


def _map_portal(v: dict, units: dict, directions: dict, skills: dict) -> Posting:
    vacancy_id = v["id"]
    city = v.get("city")
    city = city.strip() if isinstance(city, str) else ""
    skill_names = [
        skills[str(s)]["name"] for s in _list(v.get("skills"), "skills") if str(s) in skills
    ]
    unit = units.get(v.get("business_unit"))
    direction = directions.get(v.get("direction"))
    starts, ends = v.get("date_work_starts"), v.get("date_work_ends")
    parts = []
    if unit:
        parts.append(f"Проект: {unit}")
    if direction:
        parts.append(f"Направление: {direction}")
    if starts and ends:
        parts.append(f"Сроки: {starts} — {ends}")
    elif starts or ends:
        parts.append(f"Сроки: {starts or ends}")
    if skill_names:
        parts.append(f"Навыки: {', '.join(skill_names)}")
    if v.get("for_only_students") is True:
        parts.append("Только для студентов")
    is_intern = v.get("internship_type") == "internship"
    employment = Employment.INTERNSHIP if is_intern else _PORTAL_EMPLOYMENT.get(v.get("employment"))
    work_format = _PORTAL_FORMATS.get(v.get("format"))
    return Posting(
        source="vk",
        external_id=f"intern-{vacancy_id}",
        url=f"{PORTAL_URL}/vacancy/{vacancy_id}",
        company=COMPANY,
        title=v["title"].strip(),
        cities=(city,) if city and _norm(city) != "любой" else (),
        work_formats=frozenset({work_format}) if work_format else frozenset(),
        employment=employment,
        # internships and junior vacancies need no experience; other types state nothing
        experience_years_min=0 if v.get("internship_type") in ("internship", "junior") else None,
        description="\n".join(parts),
        published_at=None,
        raw={
            "id": vacancy_id,
            "title": v["title"].strip(),
            "unit": unit,
            "direction": direction,
            "internship_type": v.get("internship_type"),
            "format": v.get("format"),
            "city": city or None,
            "employment": v.get("employment"),
            "date_work_starts": starts,
            "date_work_ends": ends,
            "for_only_students": v.get("for_only_students"),
            "skills": skill_names,
        },
    )


class VKSource:
    name = "vk"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay
        self._requested = False

    async def _get(self, url: str, **kw) -> httpx.Response:
        if self._requested:
            await asyncio.sleep(self._delay)
        self._requested = True
        resp = await self._http.get(url, **kw)
        resp.raise_for_status()
        return resp

    async def _list_specialty(self, specialty: int) -> list[dict]:
        page_size = self._params.get("page_size", 50)
        max_pages = self._params.get("max_pages", 10)
        items: list[dict] = []
        offset = 0
        for page in range(max_pages):
            resp = await self._get(
                API_URL, params={"limit": page_size, "offset": offset, "specialty": specialty}
            )
            where = f"specialty {specialty}, page {page}"
            try:
                data = resp.json()
                results = data["results"]
                nxt = data.get("next")
                count = data["count"]
                if type(count) is not int:
                    raise ValueError("count is not an integer")
                if not isinstance(results, list) or not isinstance(nxt, str | None):
                    raise ValueError("results/next have unexpected types")
                for item in results:
                    if type(item["id"]) is not int:
                        raise ValueError("vacancy id is not an integer")
                    if not isinstance(item["title"], str) or not item["title"].strip():
                        raise ValueError("vacancy title is empty")
                next_offset = None
                if nxt:
                    values = parse_qs(urlparse(nxt).query).get("offset")
                    if not values:
                        raise ValueError("next has no offset")
                    next_offset = int(values[0])
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise RuntimeError(
                    f"vk: malformed vacancies response ({where}): "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                ) from exc
            items.extend(results)
            if next_offset is None:
                unique = len({i["id"] for i in items})
                if unique < count:
                    raise RuntimeError(
                        f"vk: got {unique} vacancies for specialty {specialty}, "
                        f"the API reports count={count}"
                    )
                return items
            if not results:
                raise RuntimeError(f"vk: empty page ({where}) before the end of the list")
            if next_offset <= offset:
                raise RuntimeError(f"vk: offset did not advance ({where})")
            offset = next_offset
        raise RuntimeError(
            f"vk: specialty {specialty} not finished after max_pages={max_pages}; "
            "raise sources.vk.max_pages/page_size"
        )

    async def _detail(self, item: dict) -> Posting:
        resp = await self._get(f"{SITE_URL}/{item['id']}/")
        try:
            return _map_vacancy(item, resp.text)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"vk: malformed vacancy {item['id']}: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc

    async def _portal(self, path: str) -> list[Posting]:
        resp = await self._get(f"{PORTAL_URL}/{path}")
        try:
            found = _NEXT_DATA.search(resp.text)
            if not found:
                raise ValueError("no embedded page state")
            page = json.loads(found.group(1))["props"]["pageProps"]["page"]
            vacancies = _list(page.get("vacancies"), "vacancies")  # absent = empty page
            units = {u["id"]: u["name"] for u in _list(page.get("units"), "units")}
            directions = {d["id"]: d["name"] for d in _list(page.get("directions"), "directions")}
            skills = page.get("skills") or {}
            if not isinstance(skills, dict):
                raise ValueError("skills is not an object")
            postings = []
            for v in vacancies:
                if type(v["id"]) is not int:
                    raise ValueError("vacancy id is not an integer")
                if not isinstance(v["title"], str) or not v["title"].strip():
                    raise ValueError("vacancy title is empty")
                if v["is_opened"] is True:
                    postings.append(_map_portal(v, units, directions, skills))
            return postings
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"vk: malformed internship portal page {path}: "
                f"{type(exc).__name__}: {str(exc)[:100]}"
            ) from exc

    async def fetch(self) -> list[Posting]:
        self._requested = False
        listed: dict[int, dict] = {}
        for specialty in self._params.get("specialties") or []:
            for item in await self._list_specialty(specialty):
                listed.setdefault(item["id"], item)
        found: dict[str, Posting] = {}
        for item in listed.values():
            posting = await self._detail(item)
            found.setdefault(posting.external_id, posting)
        pages = list(found.values())
        if len(pages) >= 3 and not any(p.raw["has_level_block"] for p in pages):
            raise RuntimeError(
                f"vk: no vacancy page has a 'Уровень' block ({len(pages)} pages); "
                "the page layout probably changed"
            )
        for path in self._params.get("portal_pages") or []:
            for posting in await self._portal(path):
                found.setdefault(posting.external_id, posting)
        return list(found.values())
