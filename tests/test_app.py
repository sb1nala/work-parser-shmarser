import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from vacancy_bot.app import run_cycle
from vacancy_bot.config import load_config
from vacancy_bot.db import repo
from vacancy_bot.domain import Employment, Posting, WorkFormat
from vacancy_bot.settings import Settings
from vacancy_bot.sources.alfa import AlfaSource
from vacancy_bot.sources.avito import AvitoSource
from vacancy_bot.sources.hh import HHSource
from vacancy_bot.sources.registry import build_sources
from vacancy_bot.sources.sber import SberSource
from vacancy_bot.sources.tbank import TBankSource
from vacancy_bot.sources.telegram import TelegramSource
from vacancy_bot.sources.vk import VKSource
from vacancy_bot.sources.vtb import VTBSource
from vacancy_bot.sources.yandex import YandexSource

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg():
    return load_config(Path("config/config.yaml"))


class FakeBot:
    def __init__(self, fail=False):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


class FakeSource:
    def __init__(self, name, result):
        self.name = name
        self.result = result

    async def fetch(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def posting() -> Posting:
    return Posting(
        source="second",
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


async def mark_initialized(sm, *names):
    async with sm() as s:
        for n in names:
            (await repo.get_source_state(s, n)).initialized = True
        await s.commit()


async def test_run_cycle_isolates_source_failures(sm, cfg):
    await mark_initialized(sm, "first", "second")
    bot = FakeBot()
    sources = [FakeSource("first", RuntimeError("boom")), FakeSource("second", [posting()])]
    await run_cycle(bot, sm, sources, cfg, 42, lambda: NOW)
    texts = [t for _, t in bot.sent]
    assert all(chat == 42 for chat, _ in bot.sent)
    assert any("first" in t and "boom" in t for t in texts)
    assert any("Стажёр-аналитик данных" in t for t in texts)


async def test_run_cycle_survives_pipeline_bug(sm, cfg, monkeypatch):
    await mark_initialized(sm, "second")
    import vacancy_bot.app as app

    real = app.run_source

    async def flaky(sm_, src, cfg_, now):
        if src.name == "first":
            raise ValueError("bug in processing")
        return await real(sm_, src, cfg_, now)

    monkeypatch.setattr(app, "run_source", flaky)
    bot = FakeBot()
    sources = [FakeSource("first", []), FakeSource("second", [posting()])]
    await run_cycle(bot, sm, sources, cfg, 42, lambda: NOW)
    texts = [t for _, t in bot.sent]
    assert any("first" in t and "ValueError: bug in processing" in t for t in texts)
    assert any("Стажёр-аналитик данных" in t for t in texts)


async def test_run_cycle_sends_quiet_summary(sm, cfg):
    bot = FakeBot()
    await run_cycle(bot, sm, [FakeSource("second", [posting()])], cfg, 42, lambda: NOW)
    assert len(bot.sent) == 1
    assert "second" in bot.sent[0][1]


async def test_run_cycle_telegram_failure_does_not_crash(sm, cfg, caplog):
    await mark_initialized(sm, "first")
    bot = FakeBot(fail=True)
    with caplog.at_level(logging.ERROR):
        await run_cycle(bot, sm, [FakeSource("first", RuntimeError("boom"))], cfg, 42, lambda: NOW)
    assert "telegram down" in caplog.text


# --- registry ---


def settings(token=None) -> Settings:
    return Settings(
        bot_token="t", owner_id=1, database_url="x", hh_user_agent="ua", hh_access_token=token,
        _env_file=None,
    )


async def names(cfg, token=None, telegram=False):
    """Source types built from cfg; Telegram, Yandex, VK and Avito ones are left out (own tests)."""
    async with httpx.AsyncClient() as http:
        types = [type(s) for s in build_sources(cfg, http, settings(token))]
    newer = (YandexSource, VKSource, AvitoSource)
    return [t for t in types if (telegram or t is not TelegramSource) and t not in newer]


async def test_registry_default_config_skips_hh(cfg):
    assert await names(cfg) == [TBankSource, SberSource, VTBSource, AlfaSource]


async def test_registry_hh_enabled_with_token(cfg):
    on = replace(cfg, sources={**cfg.sources, "hh": {**cfg.sources["hh"], "enabled": True}})
    assert await names(on, "tok") == [HHSource, TBankSource, SberSource, VTBSource, AlfaSource]


async def test_registry_hh_enabled_without_token_warns(cfg, caplog):
    on = replace(cfg, sources={**cfg.sources, "hh": {**cfg.sources["hh"], "enabled": True}})
    with caplog.at_level(logging.WARNING):
        assert await names(on) == [TBankSource, SberSource, VTBSource, AlfaSource]
    assert "HH_ACCESS_TOKEN" in caplog.text


async def test_registry_respects_disabled(cfg):
    off = replace(cfg, sources={**cfg.sources, "sber": {**cfg.sources["sber"], "enabled": False}})
    assert await names(off) == [TBankSource, VTBSource, AlfaSource]


async def test_run_cycle_commits_health_state(sm, cfg):
    await mark_initialized(sm, "first")
    bot = FakeBot()
    sources = [FakeSource("first", RuntimeError("boom"))]
    await run_cycle(bot, sm, sources, cfg, 42, lambda: NOW)
    await run_cycle(bot, sm, sources, cfg, 42, lambda: NOW)
    async with sm() as fresh:
        assert (await repo.get_source_state(fresh, "first")).failing is True
    assert sum("first" in t and "boom" in t for _, t in bot.sent) == 1


async def test_ru_ca_client_trusts_russian_root_ca(cfg):
    from vacancy_bot.sources.registry import RUSSIAN_ROOT_CA, make_ru_ca_client

    assert RUSSIAN_ROOT_CA.exists()
    async with make_ru_ca_client() as client:
        ctx = client._transport._pool._ssl_context
        subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
    assert any(s.get("commonName") == "Russian Trusted Root CA" for s in subjects)
    assert len(subjects) > 50  # certifi bundle is still loaded


async def test_only_tbank_and_alfa_get_dedicated_client(cfg):
    on = replace(cfg, sources={**cfg.sources, "hh": {**cfg.sources["hh"], "enabled": True}})
    async with httpx.AsyncClient() as shared, httpx.AsyncClient() as ru_ca:
        built = build_sources(on, shared, settings("tok"), ru_ca)
        clients = {type(s): s._http for s in built}
    assert clients[TBankSource] is ru_ca
    assert clients[HHSource] is shared
    assert clients[SberSource] is shared
    assert clients[VTBSource] is shared
    assert clients[AlfaSource] is ru_ca


async def test_registry_respects_disabled_vtb(cfg):
    off = replace(cfg, sources={**cfg.sources, "vtb": {**cfg.sources["vtb"], "enabled": False}})
    assert await names(off) == [TBankSource, SberSource, AlfaSource]


async def test_registry_respects_disabled_alfa(cfg):
    off = replace(cfg, sources={**cfg.sources, "alfa": {**cfg.sources["alfa"], "enabled": False}})
    assert await names(off) == [TBankSource, SberSource, VTBSource]


async def test_alert_not_persisted_when_send_fails_and_retried(sm, cfg):
    await mark_initialized(sm, "first")
    sources = [FakeSource("first", RuntimeError("boom"))]
    down = FakeBot(fail=True)
    await run_cycle(down, sm, sources, cfg, 42, lambda: NOW)
    async with sm() as fresh:
        assert (await repo.get_source_state(fresh, "first")).failing is False
    up = FakeBot()
    await run_cycle(up, sm, sources, cfg, 42, lambda: NOW)
    assert sum("first" in t and "boom" in t for _, t in up.sent) == 1
    async with sm() as fresh:
        assert (await repo.get_source_state(fresh, "first")).failing is True


async def test_notify_reports_success():
    from vacancy_bot.app import _notify

    assert await _notify(FakeBot(), 1, "hi") is True
    assert await _notify(FakeBot(fail=True), 1, "hi") is False


async def test_registry_builds_telegram_source_per_channel(cfg):
    on = replace(
        cfg,
        sources={
            **cfg.sources,
            "telegram": {**cfg.sources["telegram"], "enabled": True, "channels": ["aa", "bb"]},
        },
    )
    async with httpx.AsyncClient() as http:
        built = [s for s in build_sources(on, http, settings()) if isinstance(s, TelegramSource)]
    assert [s.name for s in built] == ["tg_aa", "tg_bb"]


async def test_registry_telegram_disabled_builds_none(cfg):
    off = replace(
        cfg, sources={**cfg.sources, "telegram": {**cfg.sources["telegram"], "enabled": False}}
    )
    assert await names(off, telegram=True) == await names(off)


async def test_registry_builds_yandex_with_shared_client(cfg):
    async with httpx.AsyncClient() as shared, httpx.AsyncClient() as ru_ca:
        built = build_sources(cfg, shared, settings(), ru_ca)
        (yandex,) = [s for s in built if isinstance(s, YandexSource)]
    assert yandex.name == "yandex"
    assert yandex._http is shared  # yandex.ru verifies with certifi; no Russian root CA needed


async def test_registry_respects_disabled_yandex(cfg):
    yandex_off = {**cfg.sources["yandex"], "enabled": False}
    off = replace(cfg, sources={**cfg.sources, "yandex": yandex_off})
    async with httpx.AsyncClient() as http:
        assert not [s for s in build_sources(off, http, settings()) if isinstance(s, YandexSource)]


async def test_registry_builds_vk_with_shared_client(cfg):
    async with httpx.AsyncClient() as shared, httpx.AsyncClient() as ru_ca:
        built = build_sources(cfg, shared, settings(), ru_ca)
        (vk,) = [s for s in built if isinstance(s, VKSource)]
    assert vk.name == "vk"
    assert vk._http is shared  # VK sites verify with certifi; no Russian root CA needed
    assert vk._params["specialties"] and vk._params["portal_pages"]


async def test_registry_respects_disabled_vk(cfg):
    vk_off = {**cfg.sources["vk"], "enabled": False}
    off = replace(cfg, sources={**cfg.sources, "vk": vk_off})
    async with httpx.AsyncClient() as http:
        assert not [s for s in build_sources(off, http, settings()) if isinstance(s, VKSource)]


async def test_registry_builds_avito_with_shared_client(cfg):
    async with httpx.AsyncClient() as shared, httpx.AsyncClient() as ru_ca:
        built = build_sources(cfg, shared, settings(), ru_ca)
        (avito,) = [s for s in built if isinstance(s, AvitoSource)]
    assert avito.name == "avito"
    assert avito._http is shared  # career.avito.com verifies with certifi; no Russian root CA needed
    assert avito._params["directions"] and avito._params["keep_directions"]
    assert set(avito._params["keep_directions"]) <= set(avito._params["directions"])


async def test_registry_respects_disabled_avito(cfg):
    avito_off = {**cfg.sources["avito"], "enabled": False}
    off = replace(cfg, sources={**cfg.sources, "avito": avito_off})
    async with httpx.AsyncClient() as http:
        assert not [s for s in build_sources(off, http, settings()) if isinstance(s, AvitoSource)]
