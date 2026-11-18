from vacancy_bot.bot.messages import (
    format_page,
    format_quiet_summary,
    format_vacancy,
    page_keyboard,
    parse_status_callback,
    status_keyboard,
)
from vacancy_bot.db.tables import VacancyRow
from vacancy_bot.domain import VacancyStatus


def make(**kw) -> VacancyRow:
    d = dict(
        id=1, company="Co", title="Intern", url="http://x/?a=1&b=2", cities=["Москва", "Казань"],
        work_formats=["remote", "hybrid"], flags=[], reopened=False,
    )
    d.update(kw)
    return VacancyRow(**d)


def test_format_vacancy_reopened_and_flags():
    t = format_vacancy(make(reopened=True, flags=["format_unknown"]))
    assert t.startswith("🔁 снова открыта")
    assert "⚠️ формат не указан" in t


def test_format_vacancy_basic():
    t = format_vacancy(make())
    assert "<b>Co</b> — Intern" in t
    assert "📍 Москва, Казань · удалёнка, гибрид" in t
    assert "&amp;" in t


def test_format_vacancy_unknown_city():
    assert "город не указан" in format_vacancy(make(cities=[], flags=["city_unknown"]))


def test_format_vacancy_escapes_html():
    t = format_vacancy(make(title="<script>", company="A&B"))
    assert "<script>" not in t
    assert "&lt;script&gt;" in t
    assert "A&amp;B" in t


def test_parse_status_callback_roundtrip():
    kb = status_keyboard(42)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:42:interesting" in datas and "st:42:hidden" in datas and "st:42:applied" in datas
    assert parse_status_callback("st:42:hidden") == (42, VacancyStatus.HIDDEN)


def test_parse_status_callback_garbage():
    assert parse_status_callback("st:abc:zzz") is None
    assert parse_status_callback("st:1:zzz") is None
    assert parse_status_callback("nope") is None


def test_quiet_summary():
    assert format_quiet_summary("sber", 5) == (
        "Найдено 5 открытых подходящих вакансий (sber). Посмотреть: /backlog"
    )


def test_page_keyboard():
    assert page_keyboard("backlog", 0, 25, 10).inline_keyboard[0][0].callback_data == "pg:backlog:10"
    assert page_keyboard("backlog", 20, 25, 10) is None


def test_format_page():
    t = format_page([make(), make(id=2, title="<b>")], 12, 0)
    assert "12" in t and "&lt;b&gt;" in t
    assert format_page([], 0, 0)


def test_format_vacancy_huge_fields_within_telegram_limit():
    v = make(
        company="&" * 5000, title="<" * 5000, url="http://x/?" + "&" * 9000,
        cities=["Ё&" * 500], flags=list(["city_unknown", "format_unknown", "experience_unknown",
                                          "employment_unknown"]), reopened=True,
    )
    assert len(format_vacancy(v)) <= 4096


def test_format_page_huge_rows_within_telegram_limit():
    rows = [
        make(id=i, company="&" * 5000, title="<" * 5000, url="http://x/?" + "&" * 9000)
        for i in range(10)
    ]
    assert len(format_page(rows, 10, 0)) <= 4096
    assert "http://x" in format_page(rows, 10, 0)
