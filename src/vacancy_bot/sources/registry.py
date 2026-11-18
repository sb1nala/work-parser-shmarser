import logging
import ssl
from pathlib import Path

import certifi
import httpx

from vacancy_bot.config import AppConfig
from vacancy_bot.settings import Settings
from vacancy_bot.sources.base import Source
from vacancy_bot.sources.hh import HHSource
from vacancy_bot.sources.sber import SberSource
from vacancy_bot.sources.tbank import TBankSource
from vacancy_bot.sources.vtb import VTBSource

log = logging.getLogger(__name__)

# Root CA of the Russian Ministry of Digital Development: www.tbank.ru chains to it and it is not
# in certifi. Trusted for T-Bank requests only (a dedicated client), never globally.
RUSSIAN_ROOT_CA = Path(__file__).resolve().parents[3] / "certs" / "russian_trusted_root_ca.pem"


def make_tbank_client(ca_path: Path = RUSSIAN_ROOT_CA) -> httpx.AsyncClient:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(ca_path))
    return httpx.AsyncClient(verify=ctx, timeout=30.0)


def build_sources(
    cfg: AppConfig,
    http: httpx.AsyncClient,
    settings: Settings,
    tbank_http: httpx.AsyncClient | None = None,
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
        sources.append(TBankSource(tbank_http or http, tbank))
    sber = cfg.sources.get("sber")
    if sber and sber.get("enabled"):
        sources.append(SberSource(http, sber))
    vtb = cfg.sources.get("vtb")
    if vtb and vtb.get("enabled"):
        sources.append(VTBSource(http, vtb))  # backend.honeyleads.ru verifies with certifi
    return sources
