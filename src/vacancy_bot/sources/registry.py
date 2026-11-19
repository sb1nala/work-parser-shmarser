import logging
import ssl
from pathlib import Path

import certifi
import httpx

from vacancy_bot.config import AppConfig
from vacancy_bot.settings import Settings
from vacancy_bot.sources.alfa import AlfaSource
from vacancy_bot.sources.base import Source
from vacancy_bot.sources.hh import HHSource
from vacancy_bot.sources.sber import SberSource
from vacancy_bot.sources.tbank import TBankSource
from vacancy_bot.sources.telegram import TelegramSource
from vacancy_bot.sources.vtb import VTBSource

log = logging.getLogger(__name__)

# Root CA of the Russian Ministry of Digital Development: www.tbank.ru and job.alfabank.ru chain
# to it and it is not in certifi. Trusted only by the dedicated client given to the sources that
# need it (T-Bank, Alfa-Bank), never globally.
RUSSIAN_ROOT_CA = Path(__file__).resolve().parents[3] / "certs" / "russian_trusted_root_ca.pem"


def make_ru_ca_client(ca_path: Path = RUSSIAN_ROOT_CA) -> httpx.AsyncClient:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(ca_path))
    return httpx.AsyncClient(verify=ctx, timeout=30.0)


make_tbank_client = make_ru_ca_client  # backwards-compatible name


def build_sources(
    cfg: AppConfig,
    http: httpx.AsyncClient,
    settings: Settings,
    ru_ca_http: httpx.AsyncClient | None = None,
) -> list[Source]:
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
        sources.append(TBankSource(ru_ca_http or http, tbank))
    sber = cfg.sources.get("sber")
    if sber and sber.get("enabled"):
        sources.append(SberSource(http, sber))
    vtb = cfg.sources.get("vtb")
    if vtb and vtb.get("enabled"):
        sources.append(VTBSource(http, vtb))  # backend.honeyleads.ru verifies with certifi
    alfa = cfg.sources.get("alfa")
    if alfa and alfa.get("enabled"):
        sources.append(AlfaSource(ru_ca_http or http, alfa))
    telegram = cfg.sources.get("telegram")
    if telegram and telegram.get("enabled"):
        for channel in telegram.get("channels") or []:
            sources.append(TelegramSource(http, channel, telegram))
    return sources
