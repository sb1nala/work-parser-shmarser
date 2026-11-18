from types import SimpleNamespace
from unittest.mock import AsyncMock

from vacancy_bot.bot.handlers import OwnerMiddleware, build_router, handle_page, handle_status
from vacancy_bot.db.tables import VacancyRow
from vacancy_bot.domain import VacancyStatus


def vac(key, **kw):
    d = dict(dedup_key=key, company="Co", title="T", url="http://x", cities=[], work_formats=[],
             accepted=True, flags=[])
    d.update(kw)
    return VacancyRow(**d)


def fake_cb(data):
    msg = SimpleNamespace(edit_reply_markup=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=data, answer=AsyncMock(), message=msg, from_user=SimpleNamespace(id=1))


async def test_status_callback_unknown_vacancy(sm):
    cb = fake_cb("st:999999:hidden")
    await handle_status(cb, sm)
    cb.answer.assert_awaited_once_with("Вакансия не найдена")


async def test_status_callback_garbage(sm):
    cb = fake_cb("st:abc:zzz")
    await handle_status(cb, sm)
    cb.answer.assert_awaited_once_with("Вакансия не найдена")


async def test_status_callback_ok(sm):
    async with sm() as s:
        v = vac("a")
        s.add(v)
        await s.commit()
        vid = v.id
    cb = fake_cb(f"st:{vid}:applied")
    await handle_status(cb, sm)
    cb.answer.assert_awaited_once_with("Готово")
    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    async with sm() as s:
        assert (await s.get(VacancyRow, vid)).status == VacancyStatus.APPLIED


async def test_page_callback_edits_message(sm):
    async with sm() as s:
        s.add_all([vac(f"p{i}") for i in range(12)])
        await s.commit()
    cb = fake_cb("pg:backlog:10")
    await handle_page(cb, sm)
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited()


async def test_non_owner_ignored():
    mw = OwnerMiddleware(owner_id=1)
    handler = AsyncMock()
    ev = SimpleNamespace(from_user=SimpleNamespace(id=2))
    assert await mw(handler, ev, {}) is None
    handler.assert_not_awaited()
    await mw(handler, SimpleNamespace(from_user=None), {})
    handler.assert_not_awaited()
    await mw(handler, SimpleNamespace(from_user=SimpleNamespace(id=1)), {})
    handler.assert_awaited_once()


def test_router_builds(sm):
    assert build_router(sm, 1) is not None
