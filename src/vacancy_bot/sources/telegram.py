"""Telegram channels as a vacancy source, via the public web preview https://t.me/s/<channel>.

No login or API key: the preview is plain HTML with the latest ~20 posts of a public channel.
Older ones come from `?before=<post number>` (posts with a smaller number than given). Posts
are `div.tgme_widget_message[data-post="<channel>/<n>"]`; the text sits in
`.tgme_widget_message_text` (line breaks are `<br>`), the date in `time[datetime]`.

Channel posts are free text, so the mapping is heuristic: the first line is the title, with
"Role / Company" split on the first " / ", " — " or " | "; a line starting with 🗺 or 📍 lists
cities and work formats. Postings are marked `require_level_keyword` so that the filter drops
posts whose title has no level word (digests, ads, news).

Only posts of the last `window_days` are returned; the pipeline closes the rest.
"""

import asyncio
import html
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from vacancy_bot.domain import Posting, WorkFormat

BASE_URL = "https://t.me/s"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_SEPARATORS = (" — ", " | ")  # used only when the title has no " / "
_LABEL = re.compile(r"^[\W_]*(?P<label>[^\W\d_]+)\s*:\s*(?P<value>.+)$")
_TITLE_LABELS = ("позиция", "должность", "вакансия")
_COMPANY_LABELS = ("компания",)
_LOCATION_LABELS = ("локация", "город")
_LABEL_BLOCK_LINES = 6  # labels count only in the post's leading block (up to a blank line)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "yclid", "_openstat", "ref", "referrer"})
_ITEM_MARKERS = ("- ", "• ", "– ")
_DIGEST_MIN_ITEMS = 3
_TRAILING_PAREN = re.compile(r"\(([^()]*)\)\s*$")
_LOCATION_NOISE = re.compile(r"\bг\.\s*|[()…;]")
_CITY_MARKERS = ("🗺", "📍")
_REMOTE = ("удалённ", "удаленн", "удалёнк", "удаленк", "remote")
_HYBRID = ("гибрид", "hybrid")
_OFFICE = ("офис", "office")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_decoration(ch: str) -> bool:
    # whitespace, emoji and invisible joiners/selectors at the edges of a title
    # (variation selectors U+FE0E/U+FE0F are category Mn, so they are listed explicitly)
    return (
        ch.isspace()
        or ch in "\ufe0e\ufe0f"
        or unicodedata.category(ch) in ("So", "Sk", "Cf", "Cn", "Cs")
    )


def _clean_edges(text: str) -> str:
    start, end = 0, len(text)
    while start < end and _is_decoration(text[start]):
        start += 1
    while end > start and _is_decoration(text[end - 1]):
        end -= 1
    return text[start:end]


def _text_of(node: Tag) -> str:
    for br in node.find_all("br"):
        br.replace_with("\n")
    return node.get_text("")


def _own_text_node(node: Tag) -> Tag | None:
    # a reply's quoted preview is also .tgme_widget_message_text: take the post's own
    return next(
        (
            n
            for n in node.select(".tgme_widget_message_text")
            if n.find_parent(class_="tgme_widget_message_reply") is None
        ),
        None,
    )


def _segments(text_node: Tag) -> list[tuple[str, Tag | None]]:
    """The post text split on <br>: (text, first <a> of the line or None)."""
    lines: list[list] = [[]]
    for child in text_node.children:
        if isinstance(child, Tag) and child.name == "br":
            lines.append([])
        else:
            lines[-1].append(child)
    result = []
    for parts in lines:
        text = "".join(
            str(c) if isinstance(c, NavigableString) else c.get_text("") for c in parts
        ).strip()
        link = None
        for c in parts:
            if isinstance(c, Tag):
                link = c if c.name == "a" else c.find("a")
                if link is not None:
                    break
        result.append((text, link))
    return result


def _item_link(link: Tag | None) -> str | None:
    """The item's canonical own URL (not a t.me channel link): lowercase scheme/host,
    tracking params and fragment dropped, no trailing slash. Also the item's identity."""
    # hrefs in the preview are double-escaped ("&amp;amp;"): unescape until stable
    href = str(link.get("href", "")) if link is not None else ""
    while (unescaped := html.unescape(href)) != href:
        href = unescaped
    parts = urlsplit(href)
    if parts.scheme not in ("http", "https") or parts.netloc.endswith("t.me"):
        return None
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.startswith("utm_") and k not in _TRACKING_PARAMS
    )
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/") or "/",
        urlencode(query),
        "",
    ))


def _is_item(text: str, link: Tag | None) -> bool:
    return text.startswith(_ITEM_MARKERS) and _item_link(link) is not None


def _is_bullet(text: str) -> bool:
    return text.startswith(_ITEM_MARKERS) and len(text) > 2


def _split_item(body: str, default_company: str) -> tuple[str, str]:
    """"Role в Company": split on the LAST " в "; else the title rules."""
    if " в " in body:
        left, _, right = body.rpartition(" в ")
        left, right = _clean_edges(left), _clean_edges(right)
        if left and right:
            return left, right
    return _split_title(body, default_company)


def _split_title(first_line: str, default_company: str) -> tuple[str, str]:
    """Split "Role / Company" on the LAST " / " (the role may contain slashes itself);
    " — " / " | " (first occurrence) only when there is no " / "."""
    if " / " in first_line:
        left, _, right = first_line.rpartition(" / ")
    else:
        found = [(first_line.find(sep), sep) for sep in _SEPARATORS if sep in first_line]
        if not found:
            return first_line, default_company
        index, sep = min(found)
        left, right = first_line[:index], first_line[index + len(sep):]
    left, right = _clean_edges(left), _clean_edges(right)
    if left and right:
        return left, right
    return first_line, default_company


def _parse_location(line: str) -> tuple[tuple[str, ...], frozenset[WorkFormat]]:
    cities: list[str] = []
    formats: set[WorkFormat] = set()
    text = _LOCATION_NOISE.sub(",", _clean_edges(line))
    for chunk in text.replace(",", ".").split("."):
        part = chunk.strip()
        if not part:
            continue
        low = part.lower()
        if any(word in low for word in _REMOTE):
            formats.add(WorkFormat.REMOTE)
        elif any(word in low for word in _HYBRID):
            formats.add(WorkFormat.HYBRID)
        elif any(word in low for word in _OFFICE):
            formats.add(WorkFormat.OFFICE)
        else:
            cities.append(part)
    return tuple(cities), frozenset(formats)


def _parse_date(node: Tag) -> datetime:
    time_tag = node.select_one("time[datetime]")
    if time_tag is None:
        raise ValueError("post has no time[datetime]")
    published = datetime.fromisoformat(str(time_tag["datetime"]))
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return published


class TelegramSource:
    def __init__(
        self,
        http: httpx.AsyncClient,
        channel: str,
        params: dict,
        delay: float = 0.3,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._http = http
        self._channel = channel
        self._params = params
        self._delay = delay
        self._now = now
        self.name = f"tg_{channel}"
        # a quiet channel is normal, not a broken parser; a layout change raises in fetch()
        self.empty_alert_after_hours: int | None = params.get("empty_alert_after_hours")

    def _map(self, node: Tag, number: int, published_at: datetime) -> list[Posting]:
        text_node = _own_text_node(node)
        if text_node is None:
            return []
        segments = _segments(text_node)
        linked = [(t, a) for t, a in segments if _is_item(t, a)]
        first_text = next((t for t, _ in segments if _clean_edges(t)), "")
        if "подборк" in first_text.lower():  # a declared digest: link-less bullets count too
            bullets = [(t, a) for t, a in segments if _is_bullet(t)]
            return self._map_digest(bullets, number, published_at)
        if len(linked) >= _DIGEST_MIN_ITEMS:
            return self._map_digest(linked, number, published_at)
        post = self._map_post(text_node, number, published_at)
        return [post] if post else []

    def _map_digest(
        self, items: list[tuple[str, Tag | None]], number: int, published_at: datetime
    ) -> list[Posting]:
        """A digest post is a list of vacancies: one posting per item line, none for the post."""
        postings = []
        for index, (text, link) in enumerate(items, 1):
            body = text[2:].strip()
            url = _item_link(link)
            link_text = link.get_text("").strip() if link is not None else ""
            trailing = ""
            if link_text and body.startswith(link_text):
                trailing = body[len(link_text):].strip()
                body = link_text
            cities: tuple[str, ...] = ()
            formats: frozenset[WorkFormat] = frozenset()
            if trailing:  # "(Армения)", "(удалённо)" after the link
                cities, formats = _parse_location(trailing)
            match = _TRAILING_PAREN.search(body)
            if match and any(
                w in match[1].lower() for w in (*_REMOTE, *_HYBRID, *_OFFICE)
            ):
                _, inner_formats = _parse_location(match[1])
                formats |= inner_formats
                body = body[: match.start()].strip()
            title, company = _split_item(_clean_edges(body), f"Telegram @{self._channel}")
            if not title:
                continue
            postings.append(
                Posting(
                    source=self.name,
                    # the same vacancy in later digests of the channel is the same posting
                    external_id=url or f"{number}:{index}",
                    url=url or f"https://t.me/{self._channel}/{number}",
                    company=company,
                    title=title,
                    cities=cities,
                    work_formats=formats,
                    employment=None,
                    experience_years_min=None,
                    description="",
                    published_at=published_at,
                    raw={
                        "channel": self._channel,
                        "post": str(number),
                        "item": index,
                        "text": text[:2000],
                    },
                    require_level_keyword=bool(self._params.get("require_level_keyword", False)),
                )
            )
        return postings

    def _map_post(self, text_node: Tag, number: int, published_at: datetime) -> Posting | None:
        text = _text_of(text_node).strip()
        lines = [line.strip() for line in text.split("\n")]
        first = next((i for i, line in enumerate(lines) if _clean_edges(line)), None)
        if first is None:
            return None
        title, company = _split_title(_clean_edges(lines[first]), f"Telegram @{self._channel}")
        cities: tuple[str, ...] = ()
        formats: frozenset[WorkFormat] = frozenset()
        for line in lines[first + 1:]:
            if line.startswith(_CITY_MARKERS):
                cities, formats = _parse_location(line)
                break
        # labelled lines ("Позиция: ...", "Компания: ...", "Локация: ...") win over heuristics,
        # but only in the leading block: a body line "Компания: мы растём" must not count
        block_end = next(
            (i for i, line in enumerate(lines[:_LABEL_BLOCK_LINES]) if not line),
            min(len(lines), _LABEL_BLOCK_LINES),
        )
        labelled: dict[str, str] = {}
        consumed = {first}
        for i, line in enumerate(lines[:block_end]):
            match = _LABEL.match(line)
            if not match:
                continue
            label, value = match["label"].lower(), _clean_edges(match["value"])
            kind = (
                "title" if label in _TITLE_LABELS
                else "company" if label in _COMPANY_LABELS
                else "location" if label in _LOCATION_LABELS
                else None
            )
            if kind and value and kind not in labelled:
                labelled[kind] = value
                consumed.add(i)
        if "title" in labelled:
            title = labelled["title"]
        if "company" in labelled:
            company = labelled["company"]
        if "location" in labelled:
            cities, formats = _parse_location(labelled["location"])
        description = "\n".join(
            line for i, line in enumerate(lines) if i > first and i not in consumed
        ).strip()
        return Posting(
            source=self.name,
            external_id=str(number),
            url=f"https://t.me/{self._channel}/{number}",
            company=company,
            title=title,
            cities=cities,
            work_formats=formats,
            employment=None,
            experience_years_min=None,
            description=description,
            published_at=published_at,
            raw={"channel": self._channel, "post": str(number), "text": text[:2000]},
            require_level_keyword=bool(self._params.get("require_level_keyword", False)),
        )

    async def _get_page(
        self, before: int | None, cutoff: datetime
    ) -> tuple[list[tuple[int, datetime, Tag]], bool]:
        """Posts of one page as (number, date, node) and whether one is older than the window."""
        url = f"{BASE_URL}/{self._channel}"
        try:
            resp = await self._http.get(
                url,
                params={"before": before} if before is not None else None,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"{self.name}: request failed (before={before}): "
                f"{type(exc).__name__}: {str(exc)[:100]}"
            ) from exc
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            posts = []
            for node in soup.select("div.tgme_widget_message[data-post]"):
                number = int(str(node["data-post"]).rsplit("/", 1)[1])
                posts.append((number, _parse_date(node), node))
        except (ValueError, KeyError, IndexError) as exc:
            raise RuntimeError(
                f"{self.name}: malformed preview page (before={before}): "
                f"{type(exc).__name__}: {str(exc)[:100]}"
            ) from exc
        return posts, any(date < cutoff for _, date, _ in posts)

    async def fetch(self) -> list[Posting]:
        cutoff = self._now() - timedelta(days=self._params.get("window_days", 14))
        max_pages = self._params.get("max_pages", 10)
        seen: set[int] = set()
        found: dict[str, Posting] = {}
        before: int | None = None
        for page in range(max_pages):
            if page:
                await asyncio.sleep(self._delay)
            posts, window_end = await self._get_page(before, cutoff)
            if not posts:
                if page == 0:
                    raise RuntimeError(
                        f"{self.name}: no posts on {BASE_URL}/{self._channel}; the channel is "
                        "missing, renamed or has no public preview"
                    )
                break  # ran out of history
            if page == 0 and not any(_own_text_node(node) for _, _, node in posts):
                raise RuntimeError(
                    f"{self.name}: {len(posts)} posts on the first page but none has text; "
                    "the preview layout probably changed"
                )
            for number, published_at, node in posts:
                if number in seen or published_at < cutoff:
                    continue
                seen.add(number)
                try:
                    for posting in self._map(node, number, published_at):
                        found.setdefault(posting.external_id, posting)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise RuntimeError(
                        f"{self.name}: cannot parse post {number}: "
                        f"{type(exc).__name__}: {str(exc)[:100]}"
                    ) from exc
            if window_end:
                break
            before = min(number for number, _, _ in posts)
        else:
            raise RuntimeError(
                f"{self.name}: window not reached after max_pages={max_pages}; "
                "raise sources.telegram.max_pages or shorten window_days"
            )
        return list(found.values())
