import re
from functools import lru_cache

from vacancy_bot.config import Criteria, normalize
from vacancy_bot.domain import Decision, Posting, WorkFormat


@lru_cache(maxsize=None)
def _compile(words: tuple[str, ...]) -> re.Pattern[str] | None:
    """Один regex на список слов/фраз; граница = не буква и не цифра (Unicode)."""
    if not words:
        return None
    alternatives = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![^\W_])(?:{alternatives})(?![^\W_])")


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    pattern = _compile(words)
    return pattern is not None and pattern.search(text) is not None


def _reject(reason: str) -> Decision:
    return Decision(accepted=False, reason=reason, flags=())


def evaluate(posting: Posting, criteria: Criteria) -> Decision:
    t = normalize(posting.title)

    if _has_any(t, criteria.seniority_stopwords):
        return _reject("seniority")
    if _has_any(t, criteria.excluded_role_patterns):
        return _reject("excluded_role")
    if not _has_any(t, criteria.direction_keywords):
        return _reject("not_relevant")

    exp = posting.experience_years_min
    if exp is not None and exp > criteria.max_experience_years:
        return _reject("experience")

    flags: list[str] = []
    formats = posting.work_formats
    if WorkFormat.REMOTE in formats:
        pass
    elif formats:
        if not posting.cities:
            flags.append("city_unknown")
        else:
            cities = {normalize(c) for c in posting.cities}
            if not cities & set(criteria.office_cities):
                return _reject("location")
    else:
        flags.append("format_unknown")

    if (
        posting.employment is not None
        and posting.employment not in criteria.allowed_employment
    ):
        return _reject("employment")

    if exp is None and not _has_any(t, criteria.level_keywords):
        flags.append("experience_unknown")
    if posting.employment is None:
        flags.append("employment_unknown")

    return Decision(accepted=True, reason=None, flags=tuple(flags))
