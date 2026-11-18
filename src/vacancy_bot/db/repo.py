from datetime import datetime

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vacancy_bot.db.tables import PostingRow, SourceRunRow, SourceStateRow, VacancyRow
from vacancy_bot.domain import VacancyStatus


async def find_posting(s: AsyncSession, source: str, external_id: str) -> PostingRow | None:
    res = await s.execute(
        select(PostingRow).where(
            PostingRow.source == source, PostingRow.external_id == external_id
        )
    )
    return res.scalar_one_or_none()


async def find_open_vacancy(s: AsyncSession, dedup_key: str) -> VacancyRow | None:
    res = await s.execute(
        select(VacancyRow).where(VacancyRow.dedup_key == dedup_key, VacancyRow.is_open)
    )
    return res.scalar_one_or_none()


async def has_closed_vacancy(s: AsyncSession, dedup_key: str) -> bool:
    res = await s.execute(
        select(
            exists().where(VacancyRow.dedup_key == dedup_key, VacancyRow.is_open.is_(False))
        )
    )
    return bool(res.scalar())


async def last_closed_seen_at(s: AsyncSession, dedup_key: str) -> datetime | None:
    """When a closed vacancy with this key was last seen (latest of them), or None."""
    res = await s.execute(
        select(func.max(VacancyRow.last_seen_at)).where(
            VacancyRow.dedup_key == dedup_key, VacancyRow.is_open.is_(False)
        )
    )
    return res.scalar()


async def has_open_posting_from(s: AsyncSession, vacancy_id: int, source: str) -> bool:
    res = await s.execute(
        select(
            exists().where(
                PostingRow.vacancy_id == vacancy_id,
                PostingRow.source == source,
                PostingRow.is_open,
            )
        )
    )
    return bool(res.scalar())


async def open_postings_of_source(s: AsyncSession, source: str) -> list[PostingRow]:
    res = await s.execute(
        select(PostingRow).where(PostingRow.source == source, PostingRow.is_open)
    )
    return list(res.scalars())


async def close_vacancies_without_open_postings(s: AsyncSession) -> None:
    has_open = (
        select(PostingRow.id)
        .where(PostingRow.vacancy_id == VacancyRow.id, PostingRow.is_open)
        .exists()
    )
    await s.execute(
        update(VacancyRow)
        .where(VacancyRow.is_open, ~has_open)
        .values(is_open=False)
        .execution_options(synchronize_session="fetch")
    )
    await s.flush()


async def get_vacancy(s: AsyncSession, vacancy_id: int) -> VacancyRow | None:
    return await s.get(VacancyRow, vacancy_id)


async def set_status(s: AsyncSession, vacancy_id: int, status: VacancyStatus) -> bool:
    row = await s.get(VacancyRow, vacancy_id)
    if row is None:
        return False
    row.status = str(status)
    await s.flush()
    return True


async def unnotified(s: AsyncSession) -> list[VacancyRow]:
    res = await s.execute(
        select(VacancyRow)
        .where(VacancyRow.accepted, VacancyRow.is_open, VacancyRow.notified_at.is_(None))
        .order_by(VacancyRow.first_seen_at, VacancyRow.id)
    )
    return list(res.scalars())


async def mark_notified(s: AsyncSession, vacancy_id: int, at: datetime) -> None:
    await s.execute(
        update(VacancyRow).where(VacancyRow.id == vacancy_id).values(notified_at=at)
    )
    await s.flush()


async def list_by_status(
    s: AsyncSession, status: VacancyStatus, offset: int, limit: int
) -> tuple[list[VacancyRow], int]:
    cond = (
        VacancyRow.status == str(status),
        VacancyRow.accepted,
        VacancyRow.is_open,
    )
    total = (await s.execute(select(func.count()).select_from(VacancyRow).where(*cond))).scalar_one()
    res = await s.execute(
        select(VacancyRow)
        .where(*cond)
        .order_by(VacancyRow.first_seen_at.desc(), VacancyRow.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(res.scalars()), total


async def get_source_state(s: AsyncSession, source: str) -> SourceStateRow:
    row = await s.get(SourceStateRow, source)
    if row is None:
        row = SourceStateRow(source=source, initialized=False, failing=False, empty_alerted=False)
        s.add(row)
        await s.flush()
    return row


async def record_run(
    s: AsyncSession,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    ok: bool,
    error: str | None,
    fetched: int,
) -> None:
    s.add(
        SourceRunRow(
            source=source,
            started_at=started_at,
            finished_at=finished_at,
            ok=ok,
            error=error,
            fetched=fetched,
        )
    )
    await s.flush()
