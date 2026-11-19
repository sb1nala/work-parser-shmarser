from pathlib import Path

from vacancy_bot.config import load_config
from vacancy_bot.dedup import dedup_key, normalize_company, normalize_title

A = load_config(Path("config/config.yaml")).company_aliases


def test_title_variants_same_key():
    assert dedup_key("Сбер", "Стажёр-Аналитик", A) == dedup_key("СБЕР", "стажер аналитик", A)


def test_parenthesised_team_names_differ():
    risk = dedup_key("Сбер", "Стажер Data Scientist (Управление риск-моделирования)", A)
    devices = dedup_key("Сбер", "Стажер Data Scientist (SberDevices)", A)
    assert risk != devices


def test_parentheses_keep_words():
    assert dedup_key("Сбер", "Стажёр-аналитик (Data)", A) == dedup_key(
        "СБЕР", "стажер аналитик data", A
    )


def test_company_aliases():
    assert normalize_company("ПАО Сбербанк", A) == "сбер"
    assert normalize_company("АО «ТБанк»", A) == "т-банк"


def test_bigtech_company_spellings_share_a_key():
    for a, b in [("Yandex", "Яндекс"), ("ВК", "VK"), ("Avito", "Авито"), ("VK Tech", "VK")]:
        assert dedup_key(a, "Стажёр-аналитик", A) == dedup_key(b, "Стажёр-аналитик", A)


def test_unknown_company_kept():
    assert normalize_company("ООО Ромашка", A) == "ооо ромашка"


def test_different_titles_differ():
    assert dedup_key("Сбер", "Стажёр-аналитик", A) != dedup_key(
        "Сбер", "Стажёр ML-инженер", A
    )


def test_normalize_title_steps():
    assert normalize_title("  Стажёр-аналитик (Data)  /  ML ") == "стажер аналитик data ml"
