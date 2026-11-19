"""Avito careers source (career.avito.com).

Avito's careers site is a Bitrix site that renders the vacancy lists on the server: there is
no JSON list API behind it (checked: the page scripts call none, /api/vacancies/ is a 404,
the filter on /vacancies/ works on the client over data-attributes). The pages are public
and were served to plain requests with no cookies, tokens, captcha or JS challenge, and the
TLS chain is a public CA (certifi verifies it). Everything below is therefore parsed from
server-rendered HTML with an HTML parser (bs4), by attribute presence and class names, never
by attribute order.

1. Main page, https://career.avito.com/vacancies/ (GET, no parameters that matter). It shows
   only the first 4 vacancies of each section, but every section header carries the section's
   total (`<div class="vacancies-section__title"><h2>Аналитика данных</h2><span
   class="vacancies-section__items-count">15</span>`). These totals are the cross-check
   for the direction pages: a direction page that lists fewer vacancies of a section than the
   main page reports raises instead of silently "closing" the missing ones.
2. Direction pages, https://career.avito.com/vacancies/<direction>/ (GET). One page per
   direction lists ALL its vacancies (no pagination; Bitrix PAGEN parameters are not used by
   the site), so there is no cursor, offset or page limit to exhaust. The directions are the
   radio values of the filter form on /vacancies/ (22 at the time of writing, 16 of them
   non-empty, 126 vacancies in all; an empty direction is a normal answer). All configured
   `directions` are listed on every fetch: the list pages are cheap (one request each) and
   it is the only way to see the internship flag of every vacancy. A card is
   `<div class="vacancies-section__item" data-vacancy-id=... data-vacancy-section=...
   data-vacancy-geo="Москва, Казань" data-vacancy-team=... data-vacancy-remote=...
   data-vacancy-intern="Да"|"Нет">` with an `<a href="/vacancies/<direction>/<url id>/"
   class="vacancies-section__item-name">title</a>` and an optional
   `<span class="vacancies-section__item-format">` label (Гибрид / Удалённая работа /
   Офис или гибрид / ...). `data-vacancy-id` is the internal element id, the id in the URL
   is the identity used here (it is what the vacancy page is served under).
   `data-vacancy-remote` is ignored: it is "Да" for hybrid vacancies too, so it does not tell
   remote from hybrid; the visible format label is the only statement used.
3. Vacancy pages, https://career.avito.com/vacancies/<direction>/<url id>/ (GET): one
   `<script type="application/ld+json">` holds a schema.org JobPosting with `datePosted`
   (ISO 8601 with offset), `employmentType` (FULL_TIME ...), and a plain-text `description`.
   One page is fetched per kept vacancy (a polite delay between requests). A page without a
   JobPosting block, with broken JSON or with neither a description nor a date raises.
   Requirements ("не менее двух лет") are free text only, so experience stays None.

Which vacancies are kept: those of `keep_directions` (analytics/ML-like directions) and any
vacancy of any listed direction with the internship flag "Да" (the "Стажировка" checkbox of
the site; start.avito.ru/internships links "single open internships" to the same flag
filter). At the time of writing no vacancy carried the flag (all internship programs of
start.avito.ru were closed), so internships arrive here only when they open.

Layout-change guards (raise, never degrade silently): a card missing its id, title link,
geo or intern attribute, or with a flag other than Да/Нет; a direction page that has vacancy
links but no cards; no vacancies on any listed direction page; no section counts on the main
page; no format label on any card when there are at least 3 cards.

`raw` keeps only an allowlist of non-personal keys; the vacancy text is never copied.
"""

import asyncio
import json
import re
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from vacancy_bot.domain import Employment, Posting, WorkFormat

SITE_URL = "https://career.avito.com"
LIST_URL = f"{SITE_URL}/vacancies/"
COMPANY = "Авито"

_VACANCY_HREF = re.compile(r"^/vacancies/([a-z0-9-]+)/(\d+)/$")
_EMPLOYMENT = {
    "FULL_TIME": Employment.FULL,
    "PART_TIME": Employment.PART,
    "CONTRACTOR": Employment.PROJECT,
    "INTERN": Employment.INTERNSHIP,
}
_FLAGS = {"Да": True, "Нет": False}
_SPACES = re.compile(r"\s+")


def _norm(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def _formats(label: str) -> frozenset[WorkFormat]:
    """Formats the card label states; a label that names none of them says nothing."""
    text = _norm(label)
    found = set()
    if "гибрид" in text:
        found.add(WorkFormat.HYBRID)
    if "удален" in text:  # удалённая работа, удалёнка
        found.add(WorkFormat.REMOTE)
    if "офис" in text:
        found.add(WorkFormat.OFFICE)
    return frozenset(found)


def _aware(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # the site speaks UTC when told nothing


def _attr(card: Tag, name: str) -> str:
    value = card.get(name)
    if not isinstance(value, str):
        raise ValueError(f"card has no {name}")
    return value.strip()


def _parse_card(card: Tag, slug: str) -> dict:
    internal_id = _attr(card, "data-vacancy-id")
    if not internal_id.isdigit():
        raise ValueError(f"data-vacancy-id {internal_id!r} is not a number")
    geo = _attr(card, "data-vacancy-geo")
    flag = _attr(card, "data-vacancy-intern")
    if flag not in _FLAGS:
        raise ValueError(f"data-vacancy-intern {flag!r} is neither Да nor Нет")
    links = [
        (m, a.get_text(" ", strip=True))
        for a in card.find_all("a", href=True)
        if (m := _VACANCY_HREF.match(str(a["href"])))
    ]
    if not links:
        raise ValueError("card has no vacancy link")
    title = next((t for _, t in links if t), "")
    if not title:
        raise ValueError("card title is empty")
    direction, url_id = links[0][0].groups()
    labels = [
        _SPACES.sub(" ", e.get_text(" ", strip=True))
        for e in card.find_all(class_="vacancies-section__item-format")
    ]
    return {
        "id": internal_id,
        "url_id": url_id,
        "direction": direction,
        "title": title,
        "section": str(card.get("data-vacancy-section") or "").strip(),
        "team": str(card.get("data-vacancy-team") or "").strip() or None,
        "cities": tuple(c for c in (p.strip() for p in geo.split(",")) if c),
        "format": ", ".join(label for label in labels if label) or None,
        "intern": _FLAGS[flag],
    }


def _json_ld(soup: BeautifulSoup) -> dict:
    """The JobPosting object of a vacancy page."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        data = json.loads(script.string or script.get_text())
        for obj in data if isinstance(data, list) else [data]:
            kind = obj.get("@type") if isinstance(obj, dict) else None
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return obj
    raise ValueError("page has no JobPosting block (page layout changed?)")


def _employment(value: object) -> Employment | None:
    for kind in value if isinstance(value, list) else [value]:
        if isinstance(kind, str) and kind in _EMPLOYMENT:
            return _EMPLOYMENT[kind]
    return None


def _map(card: dict, page: str) -> Posting:
    ld = _json_ld(BeautifulSoup(page, "html.parser"))
    posted = ld.get("datePosted")
    if posted is not None and not isinstance(posted, str):
        raise ValueError("datePosted is not a string")
    description = ld.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("description is not a string")
    description = (description or "").strip()
    if not description and not posted:
        raise ValueError("neither description nor datePosted found (page layout changed?)")
    employment = _employment(ld.get("employmentType"))
    return Posting(
        source="avito",
        external_id=card["url_id"],
        url=f"{SITE_URL}/vacancies/{card['direction']}/{card['url_id']}/",
        company=COMPANY,
        title=card["title"],
        cities=card["cities"],
        work_formats=_formats(card["format"] or ""),
        employment=Employment.INTERNSHIP if card["intern"] else employment,
        experience_years_min=None,
        description=description,
        published_at=_aware(posted) if posted else None,
        raw={
            "id": card["id"],
            "url_id": card["url_id"],
            "title": card["title"],
            "direction": card["section"] or None,
            "team": card["team"],
            "cities": list(card["cities"]),
            "format": card["format"],
            "intern": card["intern"],
            "employment_type": ld.get("employmentType")
            if isinstance(ld.get("employmentType"), str)
            else None,
            "date_posted": posted,
        },
    )


class AvitoSource:
    name = "avito"

    def __init__(self, http: httpx.AsyncClient, params: dict, delay: float = 0.3) -> None:
        self._http = http
        self._params = params
        self._delay = delay
        self._requested = False

    async def _get(self, url: str) -> httpx.Response:
        if self._requested:
            await asyncio.sleep(self._delay)
        self._requested = True
        resp = await self._http.get(url)
        resp.raise_for_status()
        return resp

    async def _section_counts(self) -> dict[str, int]:
        resp = await self._get(LIST_URL)
        counts: dict[str, int] = {}
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for head in soup.find_all(class_="vacancies-section__title"):
                name = head.find("h2")
                count = head.find(class_="vacancies-section__items-count")
                if name is None or count is None:
                    raise ValueError("section header without name or count")
                counts[name.get_text(strip=True)] = int(count.get_text(strip=True))
        except ValueError as exc:
            raise RuntimeError(
                f"avito: malformed main page: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc
        if not counts:
            raise RuntimeError(
                "avito: no section counts on the main page (page layout changed?)"
            )
        return counts

    async def _direction(self, slug: str) -> list[dict]:
        resp = await self._get(f"{LIST_URL}{slug}/")
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.find_all(attrs={"data-vacancy-id": True})
            if not cards:
                has_links = any(
                    _VACANCY_HREF.match(str(a["href"])) for a in soup.find_all("a", href=True)
                )
                if has_links:
                    raise ValueError("vacancy links but no vacancy cards (page layout changed?)")
            return [_parse_card(card, slug) for card in cards]
        except ValueError as exc:
            raise RuntimeError(
                f"avito: malformed direction page {slug}: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc

    async def _detail(self, card: dict) -> Posting:
        resp = await self._get(f"{LIST_URL}{card['direction']}/{card['url_id']}/")
        try:
            return _map(card, resp.text)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"avito: malformed vacancy {card['url_id']}: {type(exc).__name__}: {str(exc)[:100]}"
            ) from exc

    async def fetch(self) -> list[Posting]:
        self._requested = False
        counts = await self._section_counts()
        keep = set(self._params.get("keep_directions") or [])
        listed: dict[str, dict] = {}
        for slug in self._params.get("directions") or []:
            for card in await self._direction(slug):
                listed.setdefault(card["url_id"], card)
        if not listed:
            raise RuntimeError(
                "avito: no vacancies on any listed direction page (page layout changed?)"
            )
        seen: dict[str, int] = {}
        for card in listed.values():
            seen[card["section"]] = seen.get(card["section"], 0) + 1
        for section, found in seen.items():
            if section in counts and found < counts[section]:
                raise RuntimeError(
                    f"avito: section {section!r}: {found} vacancies listed, "
                    f"the main page reports {counts[section]}"
                )
        if len(listed) >= 3 and not any(c["format"] for c in listed.values()):
            raise RuntimeError(
                f"avito: no card has a format label ({len(listed)} cards); "
                "the page layout probably changed"
            )
        postings = []
        for card in listed.values():
            if card["intern"] or card["direction"] in keep:
                postings.append(await self._detail(card))
        return postings
