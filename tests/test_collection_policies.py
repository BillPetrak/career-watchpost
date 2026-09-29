"""Tests for source polling policies."""

from datetime import timedelta

from careeros.collection_policies import SOURCE_POLICIES, SourcePolicy


def test_jobicy_minimum_interval_is_one_hour() -> None:
    assert SOURCE_POLICIES["jobicy"] == SourcePolicy(
        "jobicy", timedelta(minutes=60), documented=True
    )


def test_jobicy_interval_is_documented() -> None:
    assert SOURCE_POLICIES["jobicy"].documented is True


def test_remotive_minimum_interval_is_six_hours() -> None:
    assert SOURCE_POLICIES["remotive"] == SourcePolicy(
        "remotive", timedelta(hours=6), documented=True
    )


def test_remotive_interval_is_documented() -> None:
    assert SOURCE_POLICIES["remotive"].documented is True


def test_himalayas_minimum_interval_is_one_day() -> None:
    assert SOURCE_POLICIES["himalayas"] == SourcePolicy(
        "himalayas", timedelta(days=1), documented=True
    )


def test_himalayas_interval_is_documented() -> None:
    assert SOURCE_POLICIES["himalayas"].documented is True


def test_other_intervals_are_provisional() -> None:
    for source in ("arbeitnow", "remoteok", "weworkremotely", "workingnomads"):
        assert SOURCE_POLICIES[source].documented is False


def test_every_registered_source_has_a_policy() -> None:
    assert set(SOURCE_POLICIES) == {
        "arbeitnow",
        "himalayas",
        "jobicy",
        "remoteok",
        "remotive",
        "weworkremotely",
        "workingnomads",
        "lever",
        "greenhouse",
    }


def test_lever_minimum_interval_is_six_hours_and_provisional() -> None:
    assert SOURCE_POLICIES["lever"] == SourcePolicy("lever", timedelta(hours=6))
    assert SOURCE_POLICIES["lever"].documented is False


def test_greenhouse_minimum_interval_is_six_hours_and_provisional() -> None:
    assert SOURCE_POLICIES["greenhouse"] == SourcePolicy(
        "greenhouse", timedelta(hours=6)
    )
    assert SOURCE_POLICIES["greenhouse"].documented is False


def test_workingnomads_minimum_interval_is_six_hours() -> None:
    assert SOURCE_POLICIES["workingnomads"] == SourcePolicy(
        "workingnomads", timedelta(hours=6)
    )


def test_workingnomads_interval_is_provisional() -> None:
    assert SOURCE_POLICIES["workingnomads"].documented is False


def test_all_intervals_are_positive() -> None:
    for policy in SOURCE_POLICIES.values():
        assert policy.min_interval > timedelta(0)
