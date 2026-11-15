import pytest
from pydantic import ValidationError

from job_finder.domain import Filters, FitResult, Profile, Query, Skill


def test_fit_result_bounds_the_score() -> None:
    assert FitResult(score=85, summary="ok").score == 85
    for invalid in (-1, 101):
        with pytest.raises(ValidationError):
            FitResult(score=invalid, summary="x")


def test_skill_weight_between_1_and_5() -> None:
    with pytest.raises(ValidationError):
        Skill(name="AWS", weight=6)


def test_country_code_is_uppercase_iso2() -> None:
    assert Query(term="Cloud Architect", countries=["BR", "PT"]).countries == ["BR", "PT"]
    with pytest.raises(ValidationError):
        Query(term="x", countries=["bra"])


def test_profile_has_usable_defaults() -> None:
    profile = Profile(name="daniel")
    assert profile.filters == Filters()
    assert profile.filters.max_age_days == 14
    assert profile.queries == []


def test_profile_round_trips_through_json() -> None:
    profile = Profile(
        name="daniel",
        telegram_chat_id=8_000_000_000,
        queries=[Query(term="DevOps Lead", countries=["BR"])],
    )
    assert Profile.model_validate_json(profile.model_dump_json()) == profile
