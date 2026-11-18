from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from vacancy_bot.bot.messages import (
    format_page,
    page_keyboard,
    parse_status_callback,
    status_keyboard,
)
from vacancy_bot.db import repo
from vacancy_bot.domain import VacancyStatus

PAGE_SIZE = 10
KINDS = {
    "backlog": VacancyStatus.NEW,
    "interesting": VacancyStatus.INTERESTING,
    "applied": VacancyStatus.APPLIED,
}
HELP = (
    "Бот собирает вакансии аналитика/ML-стажёра.\n"
    "/backlog — новые, /interesting — интересные, /applied — отклики."
)


class OwnerMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id != self.owner_id:
            return None
        return await handler(event, data)


async def _page(sm: async_sessionmaker, kind: str, offset: int) -> tuple[str, Any]:
    async with sm() as s:
        rows, total = await repo.list_by_status(s, KINDS[kind], offset, PAGE_SIZE)
    return format_page(rows, total, offset), page_keyboard(kind, offset, total, PAGE_SIZE)


async def handle_start(message: Message, sm: async_sessionmaker) -> None:
    async with sm() as s:
        _, total = await repo.list_by_status(s, VacancyStatus.NEW, 0, 0)
    await message.answer(f"{HELP}\n\nВ бэклоге: {total} вакансий — /backlog")


async def handle_list(message: Message, sm: async_sessionmaker, kind: str) -> None:
    text, kb = await _page(sm, kind, 0)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


async def handle_status(cb: CallbackQuery, sm: async_sessionmaker) -> None:
    parsed = parse_status_callback(cb.data or "")
    if parsed is None:
        await cb.answer("Вакансия не найдена")
        return
    vacancy_id, status = parsed
    async with sm() as s:
        ok = await repo.set_status(s, vacancy_id, status)
        await s.commit()
    if not ok:
        await cb.answer("Вакансия не найдена")
        return
    await cb.answer("Готово")
    if cb.message is not None:
        # keep the buttons so the status can still change (Интересно -> Откликнулся)
        await cb.message.edit_reply_markup(
            reply_markup=status_keyboard(vacancy_id, current=status)
        )


async def handle_page(cb: CallbackQuery, sm: async_sessionmaker) -> None:
    parts = (cb.data or "").split(":")
    try:
        if len(parts) != 3 or parts[0] != "pg" or parts[1] not in KINDS:
            raise ValueError
        kind, offset = parts[1], int(parts[2])
        if offset < 0:
            raise ValueError
    except ValueError:
        await cb.answer("Страница не найдена")
        return
    text, kb = await _page(sm, kind, offset)
    await cb.answer()
    if cb.message is not None:
        await cb.message.edit_text(
            text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True
        )


def build_router(sm: async_sessionmaker, owner_id: int) -> Router:
    router = Router(name="vacancy_bot")
    mw = OwnerMiddleware(owner_id)
    router.message.outer_middleware(mw)
    router.callback_query.outer_middleware(mw)

    @router.message(Command("start"))
    async def _start(message: Message) -> None:
        await handle_start(message, sm)

    for kind in KINDS:

        @router.message(Command(kind))
        async def _list(message: Message, _kind: str = kind) -> None:
            await handle_list(message, sm, _kind)

    @router.callback_query(F.data.startswith("st:"))
    async def _status(cb: CallbackQuery) -> None:
        await handle_status(cb, sm)

    @router.callback_query(F.data.startswith("pg:"))
    async def _pg(cb: CallbackQuery) -> None:
        await handle_page(cb, sm)

    return router
