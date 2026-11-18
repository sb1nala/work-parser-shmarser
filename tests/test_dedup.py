from pathlib import Path

from vacancy_bot.config import load_config
from vacancy_bot.dedup import dedup_key, normalize_company, normalize_title

A = load_config(Path("config/config.yaml")).company_aliases


def test_title_variants_same_key():
    assert dedup_key("Сбер", "Стажёр-аналитик (Data)", A) == dedup_key(
        "СБЕР", "стажер аналитик", A
    )


def test_company_aliases():
    assert normalize_company("ПАО Сбербанк", A) == "сбер"
    assert normalize_company("АО «ТБанк»", A) == "т-банк"


def test_unknown_company_kept():
    assert normalize_company("ООО Ромашка", A) == "ооо ромашка"


def test_different_titles_differ():
    assert dedup_key("Сбер", "Стажёр-аналитик", A) != dedup_key(
        "Сбер", "Стажёр ML-инженер", A
    )


def test_normalize_title_steps():
    assert normalize_title("  Стажёр-аналитик (Data)  /  ML ") == "стажер аналитик ml"
