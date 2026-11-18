import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy.ext.asyncio import async_sessionmaker

from vacancy_bot.bot.messages import format_vacancy, status_keyboard
from vacancy_bot.db import repo

log = logging.getLogger(__name__)


async def _send(bot: Bot, owner_id: int, text: str, vacancy_id: int) -> None:
    await bot.send_message(
        owner_id,
        text,
        reply_markup=status_keyboard(vacancy_id),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def send_unnotified(
    bot: Bot,
    sm: async_sessionmaker,
    owner_id: int,
    now: Callable[[], datetime],
    delay: float = 1.0,
) -> int:
    async with sm() as s:
        items = [(v.id, format_vacancy(v)) for v in await repo.unnotified(s)]
    sent = 0
    for vacancy_id, text in items:
        try:
            try:
                await _send(bot, owner_id, text, vacancy_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await _send(bot, owner_id, text, vacancy_id)
        except Exception:
            log.exception("sending vacancy %s failed; stopping broadcast", vacancy_id)
            break
        async with sm() as s:
            await repo.mark_notified(s, vacancy_id, now())
            await s.commit()
        sent += 1
        await asyncio.sleep(delay)
    return sent
