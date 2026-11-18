from pathlib import Path

from vacancy_bot.settings import get_settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("HH_USER_AGENT", "ua")
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    get_settings.cache_clear()

    s = get_settings()

    assert s.owner_id == 42
    assert s.config_path == Path("config/config.yaml")
