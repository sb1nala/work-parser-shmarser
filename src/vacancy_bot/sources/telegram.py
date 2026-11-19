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
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from vacancy_bot.domain import Posting, WorkFormat

BASE_URL = "https://t.me/s"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_SEPARATORS = (" / ", " — ", " | ")
_CITY_MARKERS = ("🗺", "📍")
_REMOTE = ("удалённо", "удаленно", "удалёнка", "удаленка", "remote")
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


def _split_title(first_line: str, default_company: str) -> tuple[str, str]:
    found = [(first_line.find(sep), sep) for sep in _SEPARATORS if sep in first_line]
    if found:
        index, sep = min(found)
        left = _clean_edges(first_line[:index])
        right = _clean_edges(first_line[index + len(sep):])
        if left and right:
            return left, right
    return first_line, default_company


def _parse_location(line: str) -> tuple[tuple[str, ...], frozenset[WorkFormat]]:
    cities: list[str] = []
    formats: set[WorkFormat] = set()
    for chunk in _clean_edges(line).replace(",", ".").split("."):
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

    def _map(self, node: Tag, number: int, published_at: datetime) -> Posting | None:
        # a reply's quoted preview is also .tgme_widget_message_text: take the post's own
        text_node = next(
            (
                n
                for n in node.select(".tgme_widget_message_text")
                if n.find_parent(class_="tgme_widget_message_reply") is None
            ),
            None,
        )
        if text_node is None:
            return None
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
            description="\n".join(lines[first + 1:]).strip(),
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
        found: dict[int, Posting | None] = {}
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
            for number, published_at, node in posts:
                if number in found or published_at < cutoff:
                    continue
                try:
                    found[number] = self._map(node, number, published_at)
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
        return [p for p in found.values() if p is not None]
