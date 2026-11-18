from dataclasses import dataclass
from pathlib import Path

import yaml

from vacancy_bot.domain import Employment


def normalize(text: str) -> str:
    """Нижний регистр, ё -> е, лишние пробелы убраны."""
    return " ".join(text.lower().replace("ё", "е").split())


def _norm_tuple(items: list[str]) -> tuple[str, ...]:
    # порядок сохраняем, дубликаты после нормализации (стажер/стажёр) убираем
    return tuple(dict.fromkeys(normalize(str(i)) for i in items))


@dataclass(frozen=True)
class Criteria:
    level_keywords: tuple[str, ...]
    direction_keywords: tuple[str, ...]
    seniority_stopwords: tuple[str, ...]
    excluded_role_patterns: tuple[str, ...]
    max_experience_years: int
    office_cities: tuple[str, ...]
    allowed_employment: frozenset[Employment]


@dataclass(frozen=True)
class AppConfig:
    criteria: Criteria
    company_aliases: dict[str, str]  # нормализованное имя -> каноническое
    schedule_every_hours: int
    source_timeout_seconds: int
    empty_alert_after_hours: int
    reopen_after_hours: int  # закрытая меньше этого срока и вернувшаяся — без повторного уведомления
    sources: dict[str, dict]  # имя источника -> его параметры


def load_config(path: Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    c = data["criteria"]
    criteria = Criteria(
        level_keywords=_norm_tuple(c["level_keywords"]),
        direction_keywords=_norm_tuple(c["direction_keywords"]),
        seniority_stopwords=_norm_tuple(c["seniority_stopwords"]),
        excluded_role_patterns=_norm_tuple(c["excluded_role_patterns"]),
        max_experience_years=int(c["max_experience_years"]),
        office_cities=_norm_tuple(c["office_cities"]),
        allowed_employment=frozenset(Employment(e) for e in c["allowed_employment"]),
    )
    return AppConfig(
        criteria=criteria,
        company_aliases={normalize(k): v for k, v in data["company_aliases"].items()},
        schedule_every_hours=int(data["schedule"]["every_hours"]),
        source_timeout_seconds=int(data["source_timeout_seconds"]),
        empty_alert_after_hours=int(data["empty_alert_after_hours"]),
        reopen_after_hours=int(data.get("reopen_after_hours", 168)),
        sources=dict(data["sources"]),
    )
