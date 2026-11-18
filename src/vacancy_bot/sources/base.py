from typing import Protocol

from vacancy_bot.domain import Posting


class Source(Protocol):
    name: str

    async def fetch(self) -> list[Posting]:
        """Return all current postings.

        Must raise if any page failed to load: a partial result is not allowed,
        because missing postings would be treated as closed.
        """
        ...
