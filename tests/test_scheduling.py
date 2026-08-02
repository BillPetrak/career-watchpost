"""Tests for due-source selection."""

from datetime import datetime, timedelta

import pytest

from careeros.collection_policies import SOURCE_POLICIES
from careeros.collectors import COLLECTORS
from careeros.scheduling import due_sources

_NOW = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_ORDER = tuple(COLLECTORS)


def _attempts(**overrides: datetime | None) -> dict[str, datetime | None]:
    """Build a last-attempt map with all sources completed at _NOW by default."""
    mapping = {source: _NOW for source in _ORDER}
    mapping.update(overrides)
    return mapping


def test_fresh_source_is_due() -> None:
    last_attempts = _attempts(jobicy=None)
    assert "jobicy" in due_sources(
        now=_NOW,
        policies=SOURCE_POLICIES,
        last_attempts=last_attempts,
        order=_ORDER,
    )


def test_recent_source_not_due() -> None:
    last_attempts = _attempts(jobicy=_NOW)
    assert "jobicy" not in due_sources(
        now=_NOW,
        policies=SOURCE_POLICIES,
        last_attempts=last_attempts,
        order=_ORDER,
    )


def test_expired_source_is_due() -> None:
    last_attempts = _attempts(jobicy=_NOW - timedelta(hours=1))
    assert "jobicy" in due_sources(
        now=_NOW,
        policies=SOURCE_POLICIES,
        last_attempts=last_attempts,
        order=_ORDER,
    )


def test_exactly_at_interval_is_due() -> None:
    last_attempts = _attempts(jobicy=_NOW - timedelta(minutes=60))
    assert "jobicy" in due_sources(
        now=_NOW,
        policies=SOURCE_POLICIES,
        last_attempts=last_attempts,
        order=_ORDER,
    )


def test_registry_order_preserved() -> None:
    old = {source: _NOW - timedelta(days=1) for source in _ORDER}
    assert due_sources(
        now=_NOW,
        policies=SOURCE_POLICIES,
        last_attempts=old,
        order=_ORDER,
    ) == list(_ORDER)


def test_completed_failed_attempt_throttles_like_any_attempt() -> None:
    last_attempts = _attempts(jobicy=_NOW - timedelta(minutes=5))
    assert "jobicy" not in due_sources(
        now=_NOW,
        policies=SOURCE_POLICIES,
        last_attempts=last_attempts,
        order=_ORDER,
    )


def test_missing_policy_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        due_sources(
            now=_NOW,
            policies={},
            last_attempts=_attempts(),
            order=_ORDER,
        )
