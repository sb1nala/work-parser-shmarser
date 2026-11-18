import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import async_sessionmaker

from vacancy_bot.bot.handlers import build_router
from vacancy_bot.bot.messages import format_quiet_summary
from vacancy_bot.bot.notifier import send_unnotified
from vacancy_bot.config import AppConfig, load_config
from vacancy_bot.db.session import make_sessionmaker
from vacancy_bot.health import health_alert
from vacancy_bot.pipeline import RunResult, format_error, run_source
from vacancy_bot.settings import get_settings
from vacancy_bot.sources.base import Source
from vacancy_bot.sources.registry import build_sources, make_tbank_client

log = logging.getLogger(__name__)


async def _notify(bot: Bot, owner_id: int, text: str) -> bool:
    """Send a message to the owner. Returns False (and logs) if sending failed."""
    try:
        await bot.send_message(owner_id, text)
    except Exception:
        log.exception("sending message to owner failed")
        return False
    return True


async def run_cycle(
    bot: Bot,
    sm: async_sessionmaker,
    sources: list[Source],
    cfg: AppConfig,
    owner_id: int,
    now: Callable[[], datetime],
) -> None:
    for src in sources:
        try:
            result = await run_source(sm, src, cfg, now)
        except Exception as exc:
            log.exception("source %s crashed", src.name)
            error = format_error(exc)
            result = RunResult(src.name, ok=False, fetched=0, error=error, quiet_count=None)
        try:
            if result.quiet_count is not None:
                await _notify(bot, owner_id, format_quiet_summary(src.name, result.quiet_count))
            async with sm() as s:
                alert = await health_alert(s, result, cfg, now())
                # persist the state transition only once the owner has been told,
                # otherwise the next cycle detects it again and retries the alert
                if alert is None or await _notify(bot, owner_id, alert):
                    await s.commit()
                else:
                    await s.rollback()
        except Exception:
            log.exception("post-processing of source %s failed", src.name)
    try:
        await send_unnotified(bot, sm, owner_id, now)
    except Exception:
        log.exception("sending vacancies failed")


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    cfg = load_config(settings.config_path)
    sm = make_sessionmaker(settings.database_url)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_router(sm, settings.owner_id))

    async with httpx.AsyncClient(timeout=30.0) as http, make_tbank_client() as tbank_http:
        sources = build_sources(cfg, http, settings, tbank_http)
        log.info("sources: %s", [s.name for s in sources])
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            run_cycle,
            "interval",
            hours=cfg.schedule_every_hours,
            args=[bot, sm, sources, cfg, settings.owner_id, _utcnow],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,  # a late run (e.g. after sleep) still runs, once
            next_run_time=_utcnow(),
        )
        scheduler.start()
        try:
            await dp.start_polling(bot)
        finally:
            scheduler.shutdown(wait=False)
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
