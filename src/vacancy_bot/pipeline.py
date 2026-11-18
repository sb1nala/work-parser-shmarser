import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vacancy_bot.config import AppConfig
from vacancy_bot.db import repo
from vacancy_bot.db.tables import PostingRow, VacancyRow
from vacancy_bot.dedup import dedup_key
from vacancy_bot.domain import Posting
from vacancy_bot.filtering import evaluate
from vacancy_bot.sources.base import Source

ERROR_MAX_LEN = 500


@dataclass
class RunResult:
    source: str
    ok: bool
    fetched: int
    error: str | None
    quiet_count: int | None  # not None for a quiet (first) run: number of matching open vacancies


async def _revive_posting(s: AsyncSession, row: PostingRow, now: datetime) -> VacancyRow:
    """A closed posting reappeared. Returns the vacancy it now belongs to."""
    vacancy = await repo.get_vacancy(s, row.vacancy_id)
    assert vacancy is not None
    row.is_open = True
    if vacancy.is_open:
        return vacancy
    other = await repo.find_open_vacancy(s, vacancy.dedup_key)
    if other is not None:
        # keep the partial unique index (dedup_key WHERE is_open) valid: join the open one
        row.vacancy_id = other.id
        return other
    vacancy.is_open = True
    vacancy.reopened = True
    vacancy.notified_at = None
    return vacancy


async def _ingest(s: AsyncSession, p: Posting, cfg: AppConfig, now: datetime) -> VacancyRow:
    row = await repo.find_posting(s, p.source, p.external_id)
    if row is not None:
        row.last_seen_at = now
        if row.is_open:
            vacancy = await repo.get_vacancy(s, row.vacancy_id)
            assert vacancy is not None
        else:
            vacancy = await _revive_posting(s, row, now)
        vacancy.last_seen_at = now
        await s.flush()
        return vacancy

    key = dedup_key(p.company, p.title, cfg.company_aliases)
    vacancy = await repo.find_open_vacancy(s, key)
    if vacancy is None:
        decision = evaluate(p, cfg.criteria)
        vacancy = VacancyRow(
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
            reopened=await repo.has_closed_vacancy(s, key),
            first_seen_at=now,
            last_seen_at=now,
        )
        s.add(vacancy)
        await s.flush()
    else:
        vacancy.last_seen_at = now
    s.add(
        PostingRow(
            vacancy_id=vacancy.id,
            source=p.source,
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
        vacancy = await _ingest(s, p, cfg, now)
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
            error = f"{type(exc).__name__}: {exc}"[:ERROR_MAX_LEN]
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
    return RunResult(src.name, ok=True, fetched=len(batch), error=None, quiet_count=quiet)
