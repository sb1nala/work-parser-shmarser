import re

from vacancy_bot.config import normalize

_PARENS = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^\w]|_", re.UNICODE)
_QUOTES = re.compile(r"[«»\"']")


def normalize_title(title: str) -> str:
    """Название для ключа склейки: без скобок и пунктуации."""
    text = _PARENS.sub(" ", normalize(title))
    return normalize(_NON_ALNUM.sub(" ", text))


def normalize_company(name: str, aliases: dict[str, str]) -> str:
    """Имя компании без кавычек, приведённое к каноническому по алиасам."""
    text = normalize(_QUOTES.sub("", normalize(name)))
    return aliases.get(text, text)


def dedup_key(company: str, title: str, aliases: dict[str, str]) -> str:
    return f"{normalize_company(company, aliases)}|{normalize_title(title)}"
