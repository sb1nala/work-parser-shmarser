from pathlib import Path

from vacancy_bot.config import load_config
from vacancy_bot.domain import Employment


def test_load_shipped_config():
    cfg = load_config(Path("config/config.yaml"))
    assert "москва" in cfg.criteria.office_cities
    assert cfg.criteria.max_experience_years == 0
    assert Employment.PART in cfg.criteria.allowed_employment
    assert cfg.company_aliases["пао сбербанк"] == "сбер"
    assert "hh" in cfg.sources
    assert "стажер" in cfg.criteria.level_keywords
    assert "стажёр" not in cfg.criteria.level_keywords


def test_reopen_window(tmp_path):
    cfg = load_config(Path("config/config.yaml"))
    assert cfg.reopen_after_hours == 168
    assert "данных" not in cfg.criteria.direction_keywords
    text = Path("config/config.yaml").read_text(encoding="utf-8")
    no_window = tmp_path / "c.yaml"
    no_window.write_text(
        "\n".join(line for line in text.splitlines() if not line.startswith("reopen_after_hours")),
        encoding="utf-8",
    )
    assert load_config(no_window).reopen_after_hours == 168
