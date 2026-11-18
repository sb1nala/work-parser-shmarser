import logging

import httpx

from vacancy_bot.config import AppConfig
from vacancy_bot.settings import Settings
from vacancy_bot.sources.base import Source
from vacancy_bot.sources.hh import HHSource
from vacancy_bot.sources.sber import SberSource
from vacancy_bot.sources.tbank import TBankSource

log = logging.getLogger(__name__)


def build_sources(cfg: AppConfig, http: httpx.AsyncClient, settings: Settings) -> list[Source]:
    sources: list[Source] = []
    hh = cfg.sources.get("hh")
    if hh and hh.get("enabled"):
        if settings.hh_access_token:
            sources.append(
                HHSource(
                    http, settings.hh_user_agent, hh, access_token=settings.hh_access_token
                )
            )
        else:
            log.warning("source hh is enabled but HH_ACCESS_TOKEN is not set; skipping it")
    tbank = cfg.sources.get("tbank")
    if tbank and tbank.get("enabled"):
        sources.append(TBankSource(http, tbank))
    sber = cfg.sources.get("sber")
    if sber and sber.get("enabled"):
        sources.append(SberSource(http, sber))
    return sources
