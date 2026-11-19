from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WorkFormat(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    OFFICE = "office"


class Employment(StrEnum):
    FULL = "full"
    PART = "part"
    INTERNSHIP = "internship"
    PROJECT = "project"
    OTHER = "other"


class VacancyStatus(StrEnum):
    NEW = "new"
    INTERESTING = "interesting"
    HIDDEN = "hidden"
    APPLIED = "applied"


@dataclass(frozen=True)
class Posting:
    """Одно объявление в одном источнике."""

    source: str  # "hh" | "tbank" | "sber"
    external_id: str
    url: str
    company: str
    title: str
    cities: tuple[str, ...]  # () = не указано
    work_formats: frozenset[WorkFormat]  # пусто = не указано
    employment: Employment | None  # None = не указано
    experience_years_min: int | None  # None = не указано, 0 = без опыта
    description: str
    published_at: datetime | None
    raw: dict
    require_level_keyword: bool = False  # True: отсев, если в названии нет слова уровня (посты каналов)


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str | None  # код отсева: seniority | excluded_role | not_relevant | no_level_keyword | experience | location | employment
    flags: tuple[str, ...]  # city_unknown | format_unknown | experience_unknown | employment_unknown
