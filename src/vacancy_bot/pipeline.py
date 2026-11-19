import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vacancy_bot.config import AppConfig
from vacancy_bot.db import repo
from vacancy_bot.db.tables import PostingRow, VacancyRow
from vacancy_bot.dedup import dedup_key
from vacancy_bot.domain import Decision, Posting, VacancyStatus
from vacancy_bot.filtering import evaluate
from vacancy_bot.sources.base import Source

ERROR_MAX_LEN = 500


def format_error(exc: BaseException, timeout: int | None = None) -> str:
    """Owner-readable error text; never empty (asyncio.wait_for raises a bare TimeoutError)."""
    detail = str(exc)
    if not detail and timeout is not None and isinstance(exc, TimeoutError):
        detail = f"no response within {timeout}s"
    text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return text[:ERROR_MAX_LEN]


@dataclass
class RunResult:
    source: str
    ok: bool
    fetched: int
    error: str | None
    quiet_count: int | None  # not None for a quiet (first) run: number of matching open vacancies
    empty_alert_after_hours: int | None = None  # per-source override of the config threshold


def _new_season(closed_at: datetime | None, cfg: AppConfig, now: datetime) -> bool:
    """Closed long enough ago that coming back is a new season, not a fetch glitch."""
    return closed_at is not None and now - closed_at > timedelta(hours=cfg.reopen_after_hours)


def _reopen(vacancy: VacancyRow, cfg: AppConfig, now: datetime) -> None:
    """A closed vacancy is back. last_seen_at (not yet updated) is when it closed."""
    vacancy.is_open = True
    if _new_season(vacancy.last_seen_at, cfg, now):
        vacancy.reopened = True
        vacancy.notified_at = None
        vacancy.status = str(VacancyStatus.NEW)
    # else: a short gap; keep notified_at, status and reopened as they were


def _new_vacancy(p: Posting, key: str, decision: Decision, now: datetime) -> VacancyRow:
    return VacancyRow(
        dedup_key=key,
        company=p.company,
        title=p.title,
        url=p.url,
        cities=list(p.cities),
        work_formats=[str(w) for w in p.work_formats],
        employment=str(p.employment) if p.employment is not None else None,
        experience_years_min=p.experience_years_min,
        accepted=decision.accepted,
        reject_reason=decision.reason,
        flags=list(decision.flags),
        reopened=False,
        first_seen_at=now,
        last_seen_at=now,
    )


def _own_key(base_key: str, source: str, p: Posting) -> str:
    """Key of a vacancy that must not merge with the open one holding base_key.

    Unique per posting, so the partial unique index (dedup_key WHERE is_open) stays valid.
    """
    return f"{base_key}#{source}:{p.external_id}"


async def _can_join(s: AsyncSession, vacancy: VacancyRow, source: str, accepted: bool) -> bool:
    """Merge only across sources and only postings with the same filter verdict."""
    return vacancy.accepted == accepted and not await repo.has_open_posting_from(
        s, vacancy.id, source
    )


async def _revive_posting(
    s: AsyncSession, row: PostingRow, p: Posting, source: str, cfg: AppConfig, now: datetime
) -> VacancyRow:
    """A closed posting reappeared. Returns the vacancy it now belongs to."""
    vacancy = await repo.get_vacancy(s, row.vacancy_id)
    assert vacancy is not None
    row.is_open = True
    if vacancy.is_open:
        return vacancy
    other = await repo.find_open_vacancy(s, vacancy.dedup_key)
    if other is None:
        _reopen(vacancy, cfg, now)
        return vacancy
    decision = evaluate(p, cfg.criteria)
    if await _can_join(s, other, source, decision.accepted):
        # keep the partial unique index (dedup_key WHERE is_open) valid: join the open one
        row.vacancy_id = other.id
        return other
    # its key is held by an open vacancy it must not merge with: give it its own vacancy
    base = dedup_key(p.company, p.title, cfg.company_aliases)
    own = _new_vacancy(p, _own_key(base, source, p), decision, now)
    if _new_season(vacancy.last_seen_at, cfg, now):
        own.reopened = True
    else:
        own.notified_at = vacancy.notified_at
        own.status = vacancy.status
        own.reopened = vacancy.reopened
    s.add(own)
    await s.flush()
    row.vacancy_id = own.id
    return own


async def _ingest(
    s: AsyncSession, source: str, p: Posting, cfg: AppConfig, now: datetime
) -> VacancyRow:
    row = await repo.find_posting(s, source, p.external_id)
    if row is not None:
        row.last_seen_at = now
        if row.is_open:
            vacancy = await repo.get_vacancy(s, row.vacancy_id)
            assert vacancy is not None
        else:
            vacancy = await _revive_posting(s, row, p, source, cfg, now)
        vacancy.last_seen_at = now
        await s.flush()
        return vacancy

    base = dedup_key(p.company, p.title, cfg.company_aliases)
    decision = evaluate(p, cfg.criteria)
    vacancy = await repo.find_open_vacancy(s, base)
    if vacancy is not None and await _can_join(s, vacancy, source, decision.accepted):
        vacancy.last_seen_at = now
    else:
        key = base if vacancy is None else _own_key(base, source, p)
        closed_at = await repo.last_closed_seen_at(s, key)
        vacancy = _new_vacancy(p, key, decision, now)
        vacancy.reopened = _new_season(closed_at, cfg, now)
        s.add(vacancy)
        await s.flush()
    s.add(
        PostingRow(
            vacancy_id=vacancy.id,
            source=source,
            external_id=p.external_id,
            url=p.url,
            raw=p.raw,
            is_open=True,
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    await s.flush()
    return vacancy


async def process_postings(
    s: AsyncSession, source: str, postings: list[Posting], cfg: AppConfig, now: datetime
) -> int | None:
    batch = {p.external_id: p for p in postings}  # collapse duplicates by external_id
    touched: set[int] = set()
    for p in batch.values():
        vacancy = await _ingest(s, source, p, cfg, now)
        touched.add(vacancy.id)

    for row in await repo.open_postings_of_source(s, source):
        if row.external_id not in batch:
            row.is_open = False
    await s.flush()
    await repo.close_vacancies_without_open_postings(s)

    state = await repo.get_source_state(s, source)
    if state.initialized:
        return None
    state.initialized = True
    count = 0
    if touched:
        res = await s.execute(
            select(VacancyRow)
            .where(
                VacancyRow.id.in_(touched),
                VacancyRow.accepted,
                VacancyRow.is_open,
                VacancyRow.notified_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        for v in res.scalars():
            v.notified_at = now
            count += 1
    await s.flush()
    return count


async def run_source(
    sm: async_sessionmaker, src: Source, cfg: AppConfig, now: Callable[[], datetime]
) -> RunResult:
    started = now()
    async with sm() as s:
        try:
            batch = await asyncio.wait_for(src.fetch(), cfg.source_timeout_seconds)
        except Exception as exc:
            error = format_error(exc, cfg.source_timeout_seconds)
            await repo.record_run(s, src.name, started, now(), False, error, 0)
            await s.commit()
            return RunResult(src.name, ok=False, fetched=0, error=error, quiet_count=None)
        try:
            quiet = await process_postings(s, src.name, batch, cfg, started)
            if batch:
                state = await repo.get_source_state(s, src.name)
                state.last_nonempty_at = now()
            await repo.record_run(s, src.name, started, now(), True, None, len(batch))
            await s.commit()
        except BaseException:
            await s.rollback()
            raise
    return RunResult(
        src.name,
        ok=True,
        fetched=len(batch),
        error=None,
        quiet_count=quiet,
        empty_alert_after_hours=getattr(src, "empty_alert_after_hours", None),
    )
