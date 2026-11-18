from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vacancy_bot.config import load_config
from vacancy_bot.db import repo
from vacancy_bot.health import health_alert
from vacancy_bot.pipeline import RunResult

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg():
    base = load_config(Path("config/config.yaml"))
    return replace(base, empty_alert_after_hours=24)


def ok(fetched: int = 1) -> RunResult:
    return RunResult(source="tbank", ok=True, fetched=fetched, error=None, quiet_count=None)


def bad(error: str = "boom") -> RunResult:
    return RunResult(source="tbank", ok=False, fetched=0, error=error, quiet_count=None)


async def fresh_state(session, created_at=T0, last_nonempty_at=None):
    st = await repo.get_source_state(session, "tbank")
    st.created_at = created_at
    st.last_nonempty_at = last_nonempty_at
    await session.flush()
    return st


async def test_alerts_once_on_failure(session, cfg):
    st = await fresh_state(session)
    first = await health_alert(session, bad("timeout"), cfg, T0)
    second = await health_alert(session, bad("timeout"), cfg, T0 + timedelta(hours=1))
    assert first == "❗ Источник tbank упал: timeout"
    assert second is None
    assert st.failing is True


async def test_recovery_message(session, cfg):
    st = await fresh_state(session, last_nonempty_at=T0)
    await health_alert(session, bad(), cfg, T0)
    msg = await health_alert(session, ok(), cfg, T0 + timedelta(hours=1))
    assert msg == "✅ Источник tbank снова работает"
    assert st.failing is False
    assert await health_alert(session, ok(), cfg, T0 + timedelta(hours=2)) is None


async def test_empty_too_long_alerts_once(session, cfg):
    st = await fresh_state(session)
    assert await health_alert(session, ok(0), cfg, T0 + timedelta(hours=24)) is None
    msg = await health_alert(session, ok(0), cfg, T0 + timedelta(hours=25))
    assert msg == "⚠️ Источник tbank ничего не возвращает больше 24 ч — возможно, сломался парсер"
    assert st.empty_alerted is True
    assert await health_alert(session, ok(0), cfg, T0 + timedelta(hours=30)) is None


async def test_nonempty_resets_empty_alert(session, cfg):
    st = await fresh_state(session)
    await health_alert(session, ok(0), cfg, T0 + timedelta(hours=25))
    assert st.empty_alerted is True
    st.last_nonempty_at = T0 + timedelta(hours=26)  # as run_source does
    assert await health_alert(session, ok(3), cfg, T0 + timedelta(hours=26)) is None
    assert st.empty_alerted is False
    msg = await health_alert(session, ok(0), cfg, T0 + timedelta(hours=51))
    assert msg is not None and "ничего не возвращает" in msg


async def test_failed_run_never_triggers_empty_alert(session, cfg):
    st = await fresh_state(session)
    msg = await health_alert(session, bad("x"), cfg, T0 + timedelta(days=5))
    assert msg == "❗ Источник tbank упал: x"
    assert st.empty_alerted is False


async def test_recovery_and_still_empty_joins_both(session, cfg):
    await fresh_state(session)
    await health_alert(session, bad(), cfg, T0 + timedelta(hours=1))
    msg = await health_alert(session, ok(0), cfg, T0 + timedelta(hours=30))
    assert msg is not None
    first, second = msg.split("\n")
    assert first.startswith("✅") and second.startswith("⚠️")


async def test_alert_text_is_html_escaped(session, cfg):
    await fresh_state(session)
    msg = await health_alert(session, bad("GET https://x/?a=1&b=2 -> <html>"), cfg, T0)
    assert msg == "❗ Источник tbank упал: GET https://x/?a=1&amp;b=2 -&gt; &lt;html&gt;"
