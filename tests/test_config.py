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
