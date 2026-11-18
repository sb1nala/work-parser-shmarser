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


def _place_line(v: VacancyRow) -> str:
    cities = ", ".join(escape(c) for c in v.cities) if v.cities else "город не указан"
    formats = ", ".join(escape(FORMAT_TEXT.get(f, f)) for f in v.work_formats)
    return f"📍 {cities} · {formats}" if formats else f"📍 {cities}"


def format_vacancy(v: VacancyRow) -> str:
    lines: list[str] = []
    if v.reopened:
        lines.append("🔁 снова открыта")
    lines.append(f"<b>{escape(v.company)}</b> — {escape(v.title)}")
    lines.append(_place_line(v))
    lines.extend(FLAG_TEXT[f] for f in v.flags if f in FLAG_TEXT)
    lines.append(escape(v.url))
    return "\n".join(lines)


def status_keyboard(vacancy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"st:{vacancy_id}:{status}")
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
    for i, v in enumerate(rows, start=offset + 1):
        lines.append(
            f'{i}. <b>{escape(v.company)}</b> — '
            f'<a href="{escape(v.url, quote=True)}">{escape(v.title)}</a>'
        )
    return "\n".join(lines)


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
