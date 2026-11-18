from dataclasses import replace
from pathlib import Path

import pytest

from vacancy_bot.config import load_config
from vacancy_bot.domain import Employment, Posting, WorkFormat
from vacancy_bot.filtering import evaluate


@pytest.fixture(scope="module")
def criteria():
    return load_config(Path("config/config.yaml")).criteria


def make_posting(**overrides) -> Posting:
    base = Posting(
        source="hh",
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
    return replace(base, **overrides)


def test_accepts_typical_internship(criteria):
    d = evaluate(make_posting(), criteria)
    assert d.accepted
    assert d.flags == ()


def test_rejects_senior(criteria):
    d = evaluate(make_posting(title="Senior Data Scientist"), criteria)
    assert not d.accepted and d.reason == "seniority"


def test_rejects_1c_analyst(criteria):
    d = evaluate(make_posting(title="Стажёр аналитик 1С"), criteria)
    assert d.reason == "excluded_role"


def test_rejects_business_analyst_even_in_bank(criteria):
    d = evaluate(make_posting(company="Сбер", title="Бизнес-аналитик стажёр"), criteria)
    assert d.reason == "excluded_role"


def test_accepts_risk_analyst_in_bank(criteria):
    d = evaluate(make_posting(company="Т-Банк", title="Стажёр риск-аналитик"), criteria)
    assert d.accepted


def test_rejects_irrelevant_title(criteria):
    d = evaluate(make_posting(title="Стажёр frontend-разработчик"), criteria)
    assert d.reason == "not_relevant"


def test_ml_does_not_match_html(criteria):
    d = evaluate(make_posting(title="HTML-верстальщик стажёр"), criteria)
    assert d.reason == "not_relevant"


def test_ml_matches_ml_engineer(criteria):
    d = evaluate(make_posting(title="ML-инженер стажёр"), criteria)
    assert d.accepted


def test_rejects_experience_over_zero(criteria):
    d = evaluate(make_posting(experience_years_min=1), criteria)
    assert d.reason == "experience"


def test_remote_from_any_city(criteria):
    d = evaluate(
        make_posting(cities=("Казань",), work_formats=frozenset({WorkFormat.REMOTE})),
        criteria,
    )
    assert d.accepted


def test_rejects_office_outside_allowed_cities(criteria):
    d = evaluate(
        make_posting(cities=("Казань",), work_formats=frozenset({WorkFormat.OFFICE})),
        criteria,
    )
    assert d.reason == "location"


def test_accepts_hybrid_in_tver(criteria):
    d = evaluate(make_posting(cities=("Тверь",)), criteria)
    assert d.accepted


def test_unknown_format_passes_with_flag(criteria):
    d = evaluate(
        make_posting(work_formats=frozenset(), cities=("Казань",)), criteria
    )
    assert d.accepted
    assert "format_unknown" in d.flags


def test_unknown_city_passes_with_flag(criteria):
    d = evaluate(make_posting(cities=()), criteria)
    assert d.accepted
    assert "city_unknown" in d.flags


def test_unknown_experience_without_level_word_flags(criteria):
    d = evaluate(
        make_posting(title="Аналитик данных", experience_years_min=None), criteria
    )
    assert d.accepted
    assert "experience_unknown" in d.flags


def test_unknown_experience_with_level_word_no_flag(criteria):
    d = evaluate(
        make_posting(title="Стажёр аналитик", experience_years_min=None), criteria
    )
    assert "experience_unknown" not in d.flags


def test_rejects_disallowed_employment(criteria):
    d = evaluate(make_posting(employment=Employment.OTHER), criteria)
    assert d.reason == "employment"
