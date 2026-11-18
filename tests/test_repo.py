from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from vacancy_bot.db import repo
from vacancy_bot.db.tables import PostingRow, VacancyRow
from vacancy_bot.domain import VacancyStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_vacancy(key="k", **kw) -> VacancyRow:
    defaults = dict(
        dedup_key=key,
        company="Co",
        title="Intern",
        url="http://x",
        cities=["Москва"],
        work_formats=["remote"],
        accepted=True,
        flags=[],
    )
    defaults.update(kw)
    return VacancyRow(**defaults)


def make_posting(vacancy_id, ext="1", **kw) -> PostingRow:
    defaults = dict(vacancy_id=vacancy_id, source="hh", external_id=ext, url="http://x", raw={})
    defaults.update(kw)
    return PostingRow(**defaults)


async def test_partial_unique_allows_closed_duplicate(session):
    session.add(make_vacancy("dup", is_open=False))
    session.add(make_vacancy("dup", is_open=True))
    await session.flush()
    session.add(make_vacancy("dup", is_open=True))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_unnotified_only_accepted_open(session):
    session.add_all(
        [
            make_vacancy("good", first_seen_at=NOW),
            make_vacancy("rej", accepted=False, reject_reason="city"),
            make_vacancy("closed", is_open=False),
            make_vacancy("sent", notified_at=NOW),
        ]
    )
    await session.flush()
    res = await repo.unnotified(session)
    assert [v.dedup_key for v in res] == ["good"]


async def test_set_status_unknown_id_returns_false(session):
    assert await repo.set_status(session, 999_999, VacancyStatus.HIDDEN) is False


async def test_set_status_updates(session):
    v = make_vacancy()
    session.add(v)
    await session.flush()
    assert await repo.set_status(session, v.id, VacancyStatus.APPLIED) is True
    got = await repo.get_vacancy(session, v.id)
    assert got.status == "applied"


async def test_list_by_status_paginates(session):
    for i in range(12):
        session.add(make_vacancy(f"k{i}", first_seen_at=NOW + timedelta(minutes=i)))
    await session.flush()
    items, total = await repo.list_by_status(session, VacancyStatus.NEW, offset=10, limit=10)
    assert len(items) == 2
    assert total == 12


async def test_close_vacancies_without_open_postings(session):
    a = make_vacancy("a")
    b = make_vacancy("b")
    session.add_all([a, b])
    await session.flush()
    session.add_all(
        [
            make_posting(a.id, "a1", is_open=False),
            make_posting(b.id, "b1", is_open=False),
            make_posting(b.id, "b2", is_open=True),
        ]
    )
    await session.flush()
    await repo.close_vacancies_without_open_postings(session)
    await session.refresh(a)
    await session.refresh(b)
    assert a.is_open is False
    assert b.is_open is True


async def test_finders_and_state(session):
    v = make_vacancy("f", is_open=False)
    session.add(v)
    await session.flush()
    p = make_posting(v.id, "e1")
    session.add(p)
    await session.flush()
    assert (await repo.find_posting(session, "hh", "e1")).id == p.id
    assert await repo.find_posting(session, "hh", "nope") is None
    assert await repo.find_open_vacancy(session, "f") is None
    assert await repo.has_closed_vacancy(session, "f") is True
    assert [x.id for x in await repo.open_postings_of_source(session, "hh")] == [p.id]
    await repo.mark_notified(session, v.id, NOW)
    await session.refresh(v)
    assert v.notified_at == NOW
    st = await repo.get_source_state(session, "hh")
    assert st.initialized is False and st.failing is False and st.empty_alerted is False
    assert st.created_at is not None
    await repo.record_run(session, "hh", NOW, NOW, True, None, 5)
