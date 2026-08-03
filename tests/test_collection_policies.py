"""Tests for source polling policies."""

from datetime import timedelta

from careeros.collection_policies import SOURCE_POLICIES, SourcePolicy


def test_jobicy_minimum_interval_is_one_hour() -> None:
    assert SOURCE_POLICIES["jobicy"] == SourcePolicy(
        "jobicy", timedelta(minutes=60), documented=True
    )


def test_jobicy_interval_is_documented() -> None:
    assert SOURCE_POLICIES["jobicy"].documented is True


def test_other_intervals_are_provisional() -> None:
    for source in ("arbeitnow", "remoteok", "weworkremotely"):
        assert SOURCE_POLICIES[source].documented is False


def test_every_registered_source_has_a_policy() -> None:
    assert set(SOURCE_POLICIES) == {"arbeitnow", "jobicy", "remoteok", "weworkremotely"}


def test_all_intervals_are_positive() -> None:
    for policy in SOURCE_POLICIES.values():
        assert policy.min_interval > timedelta(0)
