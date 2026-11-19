from datetime import datetime, timedelta
from html import escape

from sqlalchemy.ext.asyncio import AsyncSession

from vacancy_bot.config import AppConfig
from vacancy_bot.db import repo
from vacancy_bot.pipeline import RunResult


async def health_alert(
    s: AsyncSession, result: RunResult, cfg: AppConfig, now: datetime
) -> str | None:
    """Update source_state for this run and return an owner alert text, or None.

    Flushes state changes but does not commit; the caller commits.
    If a run both recovers and is still empty past the threshold, both messages are
    returned joined by a newline (recovery first).
    """
    state = await repo.get_source_state(s, result.source)
    messages: list[str] = []
    name = escape(result.source)  # alerts are sent with HTML parse mode

    if not result.ok:
        if not state.failing:
            state.failing = True
            messages.append(f"❗ Источник {name} упал: {escape(result.error or '')}")
        await s.flush()
        return "\n".join(messages) or None

    if state.failing:
        state.failing = False
        messages.append(f"✅ Источник {name} снова работает")

    if result.fetched > 0:
        state.empty_alerted = False
    else:
        reference = state.last_nonempty_at or state.created_at
        limit = result.empty_alert_after_hours or cfg.empty_alert_after_hours
        if not state.empty_alerted and now - reference > timedelta(hours=limit):
            state.empty_alerted = True
            messages.append(
                f"⚠️ Источник {name} ничего не возвращает больше {limit} ч"
                " — возможно, сломался парсер"
            )

    await s.flush()
    return "\n".join(messages) or None
