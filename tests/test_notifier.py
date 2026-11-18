from datetime import UTC, datetime

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select

from vacancy_bot.bot.messages import status_keyboard
from vacancy_bot.bot.notifier import send_unnotified
from vacancy_bot.db.tables import VacancyRow

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeBot:
    def __init__(self, script):
        self.script = list(script)  # per call: None = ok, or exception
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.calls.append((chat_id, text, reply_markup, kw))
        exc = self.script.pop(0) if self.script else None
        if exc:
            raise exc


async def seed(sm, n):
    async with sm() as s:
        for i in range(n):
            s.add(VacancyRow(dedup_key=f"k{i}", company="Co", title=f"T{i}", url="http://x",
                             cities=[], work_formats=[], accepted=True, flags=[],
                             first_seen_at=datetime(2026, 1, 1, i, tzinfo=UTC)))
        await s.commit()


async def notified(sm):
    async with sm() as s:
        rows = (await s.execute(select(VacancyRow).order_by(VacancyRow.first_seen_at))).scalars()
        return [r.notified_at for r in rows]


async def test_notifier_keeps_unsent_on_failure(sm):
    await seed(sm, 3)
    err = TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="boom")
    bot = FakeBot([None, err])
    n = await send_unnotified(bot, sm, 1, lambda: NOW, delay=0)
    assert n == 1
    got = await notified(sm)
    assert got[0] == NOW and got[1] is None and got[2] is None


async def test_notifier_retries_after_retry_after(sm):
    await seed(sm, 1)
    ra = TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="slow", retry_after=0)
    bot = FakeBot([ra])
    n = await send_unnotified(bot, sm, 1, lambda: NOW, delay=0)
    assert n == 1 and len(bot.calls) == 2
    assert (await notified(sm))[0] == NOW
    assert bot.calls[0][3].get("parse_mode") == "HTML"


async def test_notifier_sends_status_keyboard_and_logs_failure(sm, caplog):
    await seed(sm, 1)
    err = TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="boom")
    bot = FakeBot([err])
    with caplog.at_level("ERROR"):
        assert await send_unnotified(bot, sm, 1, lambda: NOW, delay=0) == 0
    assert any("failed" in r.getMessage() and r.exc_info for r in caplog.records)
    async with sm() as s:
        vid = (await s.execute(select(VacancyRow.id))).scalar_one()
    assert bot.calls[0][2] == status_keyboard(vid)
    assert bot.calls[0][0] == 1
