from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from vacancy_bot.db.tables import VacancyRow
from vacancy_bot.domain import VacancyStatus

FLAG_TEXT = {
    "city_unknown": "⚠️ город не указан",
    "format_unknown": "⚠️ формат не указан",
    "experience_unknown": "⚠️ опыт не указан",
    "employment_unknown": "⚠️ занятость не указана",
}

FORMAT_TEXT = {"remote": "удалёнка", "hybrid": "гибрид", "office": "офис"}

_BUTTONS = (
    (VacancyStatus.INTERESTING, "⭐ Интересно"),
    (VacancyStatus.HIDDEN, "🙈 Скрыть"),
    (VacancyStatus.APPLIED, "✅ Откликнулся"),
)


NL = "\n"
MAX_MESSAGE = 4096
MAX_COMPANY = 100
MAX_TITLE = 200
MAX_CITIES = 200
MAX_URL_ESCAPED = 1000


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape_fit(text: str, limit: int) -> str:
    """HTML-escape text, stopping before the escaped result would exceed limit."""
    out: list[str] = []
    used = 0
    for ch in text:
        e = escape(ch, quote=True)
        if used + len(e) > limit:
            break
        out.append(e)
        used += len(e)
    return "".join(out)


def _company(v: VacancyRow) -> str:
    return escape(_cut(v.company, MAX_COMPANY))


def _title(v: VacancyRow) -> str:
    return escape(_cut(v.title, MAX_TITLE))


def _place_line(v: VacancyRow) -> str:
    cities = (
        escape(_cut(", ".join(v.cities), MAX_CITIES)) if v.cities else "город не указан"
    )
    formats = ", ".join(escape(FORMAT_TEXT.get(f, f)) for f in v.work_formats)
    return f"📍 {cities} · {formats}" if formats else f"📍 {cities}"


def format_vacancy(v: VacancyRow) -> str:
    lines: list[str] = []
    if v.reopened:
        lines.append("🔁 снова открыта")
    lines.append(f"<b>{_company(v)}</b> — {_title(v)}")
    lines.append(_place_line(v))
    lines.extend(FLAG_TEXT[f] for f in v.flags if f in FLAG_TEXT)
    lines.append(_escape_fit(v.url, MAX_URL_ESCAPED))
    return "\n".join(lines)


def status_keyboard(
    vacancy_id: int, current: VacancyStatus | None = None
) -> InlineKeyboardMarkup:
    """Status buttons; the current status (if any) is marked with "✓ "."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✓ {label}" if status == current else label,
                    callback_data=f"st:{vacancy_id}:{status}",
                )
                for status, label in _BUTTONS
            ]
        ]
    )


def parse_status_callback(data: str) -> tuple[int, VacancyStatus] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "st":
        return None
    try:
        return int(parts[1]), VacancyStatus(parts[2])
    except ValueError:
        return None


def format_quiet_summary(source: str, count: int) -> str:
    return f"Найдено {count} открытых подходящих вакансий ({source}). Посмотреть: /backlog"


def format_page(rows: list[VacancyRow], total: int, offset: int) -> str:
    if not rows:
        return "Пока пусто."
    lines = [f"Показано {offset + 1}–{offset + len(rows)} из {total}", ""]
    size = len(NL.join(lines))
    for i, v in enumerate(rows, start=offset + 1):
        href = _escape_fit(v.url, MAX_URL_ESCAPED)
        line = f'{i}. <b>{_company(v)}</b> — <a href="{href}">{_title(v)}</a>'
        if size + 1 + len(line) > MAX_MESSAGE:
            break  # keep the message within Telegram's limit; trailing rows are dropped
        lines.append(line)
        size += 1 + len(line)
    return NL.join(lines)


def page_keyboard(
    kind: str, offset: int, total: int, page_size: int
) -> InlineKeyboardMarkup | None:
    nxt = offset + page_size
    if nxt >= total:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Дальше", callback_data=f"pg:{kind}:{nxt}")]
        ]
    )
