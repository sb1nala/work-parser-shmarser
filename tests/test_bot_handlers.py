from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from vacancy_bot.bot.handlers import (
    OwnerMiddleware,
    build_router,
    handle_list,
    handle_page,
    handle_start,
    handle_status,
)
from vacancy_bot.bot.messages import status_keyboard
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
    cb.message.edit_reply_markup.assert_awaited_once_with(
        reply_markup=status_keyboard(vid, current=VacancyStatus.APPLIED)
    )
    async with sm() as s:
        assert (await s.get(VacancyRow, vid)).status == VacancyStatus.APPLIED


async def test_status_can_change_after_first_choice(sm):
    async with sm() as s:
        v = vac("b")
        s.add(v)
        await s.commit()
        vid = v.id
    await handle_status(fake_cb(f"st:{vid}:interesting"), sm)
    cb = fake_cb(f"st:{vid}:applied")
    await handle_status(cb, sm)
    cb.message.edit_reply_markup.assert_awaited_once_with(
        reply_markup=status_keyboard(vid, current=VacancyStatus.APPLIED)
    )
    async with sm() as s:
        assert (await s.get(VacancyRow, vid)).status == VacancyStatus.APPLIED


async def test_start_shows_backlog_count(sm):
    async with sm() as s:
        s.add_all([vac("n1"), vac("n2"), vac("n3", status="hidden"), vac("n4", accepted=False)])
        await s.commit()
    msg = SimpleNamespace(answer=AsyncMock())
    await handle_start(msg, sm)
    text = msg.answer.await_args.args[0]
    assert text.startswith("Бот собирает вакансии")
    assert "В бэклоге: 2 вакансий — /backlog" in text


async def test_page_callback_edits_message(sm):
    async with sm() as s:
        s.add_all([vac(f"p{i}") for i in range(12)])
        await s.commit()
    cb = fake_cb("pg:backlog:10")
    await handle_page(cb, sm)
    cb.message.edit_text.assert_awaited_once()
    args, kw = cb.message.edit_text.await_args
    assert "11–12 из 12" in args[0]
    assert kw["reply_markup"] is None
    cb.answer.assert_awaited()


async def test_page_callback_middle_page_has_next_button(sm):
    async with sm() as s:
        s.add_all([vac(f"q{i}") for i in range(25)])
        await s.commit()
    cb = fake_cb("pg:backlog:10")
    await handle_page(cb, sm)
    args, kw = cb.message.edit_text.await_args
    assert "11–20 из 25" in args[0]
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "pg:backlog:20"


@pytest.mark.parametrize(
    "kind,status",
    [("backlog", "new"), ("interesting", "interesting"), ("applied", "applied")],
)
async def test_list_commands_route_to_status(sm, kind, status):
    async with sm() as s:
        s.add_all(
            [vac(f"r-{st}", title=f"title-{st}", status=st)
             for st in ("new", "interesting", "applied", "hidden")]
        )
        await s.commit()
    msg = SimpleNamespace(answer=AsyncMock())
    await handle_list(msg, sm, kind)
    text = msg.answer.await_args.args[0]
    assert f"title-{status}" in text
    assert text.count("title-") == 1


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


# --- router wiring: real Update objects through a Dispatcher -----------------

OWNER, STRANGER = 100, 200


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    return calls


def _user(uid):
    return User(id=uid, is_bot=False, first_name="u")


def _message(uid, text="/backlog"):
    ents = [MessageEntity(type="bot_command", offset=0, length=len(text))] if text.startswith("/") else None
    return Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
        from_user=_user(uid), text=text, entities=ents,
    )


def _cb_update(uid, data):
    cq = CallbackQuery(
        id="1", from_user=_user(uid), chat_instance="c", data=data, message=_message(uid, "hi")
    )
    return Update(update_id=2, callback_query=cq)


async def _feed(sm, update):
    dp = Dispatcher()
    dp.include_router(build_router(sm, OWNER))
    await dp.feed_update(Bot("123:abc"), update)


async def _status_of(sm, vid):
    async with sm() as s:
        return (await s.get(VacancyRow, vid)).status


async def _seed_one(sm):
    async with sm() as s:
        v = vac("w")
        s.add(v)
        await s.commit()
        return v.id


async def test_router_drops_stranger_message(sm, sent):
    await _feed(sm, Update(update_id=1, message=_message(STRANGER)))
    assert sent == []


async def test_router_drops_stranger_callback(sm, sent):
    vid = await _seed_one(sm)
    await _feed(sm, _cb_update(STRANGER, f"st:{vid}:hidden"))
    assert sent == []
    assert await _status_of(sm, vid) == "new"


async def test_router_owner_message_replies(sm, sent):
    await _feed(sm, Update(update_id=1, message=_message(OWNER)))
    assert len(sent) == 1 and isinstance(sent[0], SendMessage)


async def test_router_owner_callback_changes_status(sm, sent):
    vid = await _seed_one(sm)
    await _feed(sm, _cb_update(OWNER, f"st:{vid}:hidden"))
    assert await _status_of(sm, vid) == "hidden"
    answers = [m for m in sent if isinstance(m, AnswerCallbackQuery)]
    assert answers and answers[0].text == "Готово"
