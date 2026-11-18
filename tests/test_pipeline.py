from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vacancy_bot.config import load_config
from vacancy_bot.db import repo
from vacancy_bot.db.tables import PostingRow, SourceRunRow, VacancyRow
from vacancy_bot.domain import Employment, Posting, WorkFormat
from vacancy_bot.pipeline import process_postings, run_source

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def hours(n: int) -> datetime:
    return T0 + timedelta(hours=n)


@pytest.fixture(scope="module")
def cfg():
    return load_config(Path("config/config.yaml"))


def mk(**o) -> Posting:
    base = Posting(
        source="hh",
        external_id="1",
        url="https://example.com/1",
        company="Компания",
        title="Стажёр-аналитик данных",
        cities=("Москва",),
        work_formats=frozenset({WorkFormat.HYBRID}),
        employment=Employment.INTERNSHIP,
        experience_years_min=0,
        description="",
        published_at=None,
        raw={},
    )
    return replace(base, **o)


class FakeSource:
    def __init__(self, name, result):
        self.name = name
        self.result = result

    async def fetch(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def all_vacancies(s):
    return (await s.execute(select(VacancyRow).order_by(VacancyRow.id))).scalars().all()


async def test_first_run_is_quiet(session, cfg):
    batch = [mk(external_id=str(i), company=f"Co{i}") for i in range(3)]
    n = await process_postings(session, "hh", batch, cfg, T0)
    assert n == 3
    assert await repo.unnotified(session) == []


async def test_second_run_new_posting_is_unnotified(session, cfg):
    batch = [mk(external_id=str(i), company=f"Co{i}") for i in range(3)]
    await process_postings(session, "hh", batch, cfg, T0)
    new = mk(external_id="99", company="NewCo")
    assert await process_postings(session, "hh", [*batch, new], cfg, hours(1)) is None
    un = await repo.unnotified(session)
    assert [v.company for v in un] == ["NewCo"]


async def test_same_vacancy_from_two_sources_merges(session, cfg):
    await process_postings(session, "hh", [mk()], cfg, T0)
    await process_postings(
        session,
        "tbank",
        [mk(source="tbank", external_id="t1", title="стажер аналитик данных")],
        cfg,
        T0,
    )
    vs = await all_vacancies(session)
    assert len(vs) == 1
    ps = (await session.execute(select(PostingRow))).scalars().all()
    assert len(ps) == 2 and {p.vacancy_id for p in ps} == {vs[0].id}


async def test_duplicate_postings_in_one_batch(session, cfg):
    await process_postings(session, "hh", [mk(), mk()], cfg, T0)
    assert len(await all_vacancies(session)) == 1


async def test_disappeared_posting_closes_vacancy(session, cfg):
    await process_postings(session, "hh", [mk()], cfg, T0)
    await process_postings(session, "hh", [], cfg, hours(1))
    (v,) = await all_vacancies(session)
    await session.refresh(v)
    assert v.is_open is False


async def test_new_season_same_title_is_reopened(session, cfg):
    await process_postings(session, "hh", [mk()], cfg, T0)
    await process_postings(session, "hh", [], cfg, hours(1))
    await process_postings(session, "hh", [mk(external_id="2")], cfg, hours(2))
    un = await repo.unnotified(session)
    assert len(un) == 1 and un[0].reopened is True and un[0].is_open


async def test_closed_posting_reappears_reopens_vacancy(session, cfg):
    await process_postings(session, "hh", [mk()], cfg, T0)
    await process_postings(session, "hh", [], cfg, hours(1))
    await process_postings(session, "hh", [mk()], cfg, hours(2))
    (v,) = await all_vacancies(session)
    await session.refresh(v)
    assert v.is_open and v.reopened and v.notified_at is None


async def test_reappeared_posting_attaches_to_other_open_vacancy(session, cfg):
    await process_postings(session, "hh", [mk()], cfg, T0)
    await process_postings(session, "hh", [], cfg, hours(1))
    await process_postings(session, "tbank", [mk(source="tbank", external_id="t1")], cfg, hours(2))
    await process_postings(session, "hh", [mk()], cfg, hours(3))
    p = await repo.find_posting(session, "hh", "1")
    vs = await all_vacancies(session)
    open_vs = [v for v in vs if v.is_open]
    assert len(open_vs) == 1
    assert p.is_open and p.vacancy_id == open_vs[0].id
    ps = (await session.execute(select(PostingRow).where(PostingRow.is_open))).scalars().all()
    assert len(ps) == 2 and {x.vacancy_id for x in ps} == {open_vs[0].id}


async def test_failed_fetch_closes_nothing(sm, cfg):
    async with sm() as s:
        await process_postings(s, "hh", [mk()], cfg, T0)
        await s.commit()
    res = await run_source(sm, FakeSource("hh", TimeoutError("boom")), cfg, lambda: T0)
    assert res.ok is False and res.quiet_count is None and res.fetched == 0
    assert res.error == "TimeoutError: boom"
    async with sm() as s:
        (v,) = await all_vacancies(s)
        assert v.is_open
        runs = (await s.execute(select(SourceRunRow))).scalars().all()
        assert [r.ok for r in runs] == [False]


async def test_run_source_success(sm, cfg):
    res = await run_source(sm, FakeSource("hh", [mk()]), cfg, lambda: T0)
    assert res.ok and res.fetched == 1 and res.quiet_count == 1 and res.error is None
    async with sm() as s:
        st = await repo.get_source_state(s, "hh")
        assert st.initialized and st.last_nonempty_at == T0
        assert (await s.execute(select(SourceRunRow))).scalar_one().ok


async def test_rejected_posting_stored_with_reason(session, cfg):
    await process_postings(session, "hh", [mk(title="Senior Data Scientist")], cfg, T0)
    (v,) = await all_vacancies(session)
    assert v.accepted is False and v.reject_reason == "seniority"
