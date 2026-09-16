"""Focused R4 M3 preference inspection and reversal service coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database import preferences as preference_database
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.preferences import (
    list_preference_evidence,
    list_suppressions,
)
from careeros.database.schema import initialize_schema
from careeros.preferences.models import ExplicitPreferenceProfile
from careeros.preferences.service import (
    inspect_preferences,
    replace_explicit_profile,
    reset_learned_preferences,
    restore_learned_preference,
    suppress_learned_preference,
)
from careeros.ranking.scorer import CandidateProfile
from careeros.review.service import get_decision, set_decision

NOW = datetime(2026, 9, 16, 14, tzinfo=UTC)


def _open(path: Path):
    connection = connect(path / "preferences-m3.sqlite3")
    initialize_schema(connection)
    return connection


def _job(index: int) -> CollectedJob:
    return CollectedJob(
        source=f"source-{index}",
        source_job_id=str(index),
        title="Senior Python Engineer",
        company=f"Company {index}",
        location=f"City {index}",
        description=f"private description {index}",
        url=f"https://private.example/{index}",
        employment_type="Full-time",
        remote_status="remote",
        published_at=None,
        collected_at=NOW.isoformat(),
    )


def _seed(connection, decisions: tuple[str, ...]) -> list[int]:
    save_jobs(connection, [_job(index) for index in range(len(decisions))])
    ensure_grouped(connection)
    ids = [int(row[0]) for row in connection.execute("SELECT id FROM jobs ORDER BY id")]
    for job_id, decision in zip(ids, decisions, strict=True):
        set_decision(connection, job_id=job_id, decision=decision, now=NOW)
    return ids


def _title(inspection):
    return next(item for item in inspection.learned if item.dimension == "title")


def test_inspection_distinguishes_absent_configured_empty_and_complete_profile(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        absent = inspect_preferences(connection)
        assert absent.explicit.configured is False
        assert absent.explicit.profile == ExplicitPreferenceProfile()

        replace_explicit_profile(connection, CandidateProfile(), now=NOW)
        empty = inspect_preferences(connection)
        assert empty.explicit.configured is True
        assert empty.explicit.profile == ExplicitPreferenceProfile()
        assert empty.explicit.updated_at == NOW.isoformat()

        profile = CandidateProfile(
            desired_titles=("Backend", "Backend", "Platform"),
            skills=("Python", "SQL", "Python"),
            required_skills=("Git", "Git"),
            preferred_locations=("Athens", "Berlin", "Athens"),
            remote_preference="hybrid",
            excluded_terms=("crypto", "crypto"),
            hard_excluded_terms=("unpaid", "unpaid"),
            preferred_employment_types=("full-time", "contract", "full-time"),
            preferred_seniority=("senior", "lead", "senior"),
        )
        replace_explicit_profile(connection, profile, now=NOW)
        populated = inspect_preferences(connection)
        assert (
            populated.explicit.profile
            == ExplicitPreferenceProfile.from_candidate_profile(profile)
        )
        assert populated.explicit.locked_dimensions == frozenset(
            {"title", "location", "remote", "employment_type", "seniority"}
        )


def test_inspection_shows_active_inactive_suppressed_and_bounded_safe_provenance(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        _seed(connection, ("shortlist",) * 5 + ("dismiss",) * 2)
        initial = inspect_preferences(connection)
        title = _title(initial)
        inactive = next(item for item in initial.learned if item.dimension == "company")
        assert title.active is True
        assert inactive.active is False
        assert title.positive_count == 5
        assert title.negative_count == 2
        assert len(title.positive_examples) == 3
        assert len(title.negative_examples) == 2
        assert "private description" not in repr(initial)
        assert "private.example" not in repr(initial)
        assert "source-" not in repr(initial)

        suppress_learned_preference(connection, title.preference_key, now=NOW)
        suppressed = _title(inspect_preferences(connection))
        assert suppressed.suppressed is True
        assert suppressed.active is False
        assert suppressed.polarity == "positive"
        assert suppressed.strength_level == 2


def test_suppress_and_restore_validate_identity_and_preserve_all_evidence_and_reviews(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        ids = _seed(connection, ("shortlist", "shortlist"))
        learned = _title(inspect_preferences(connection))
        evidence = list_preference_evidence(connection)
        events = tuple(connection.execute("SELECT * FROM review_events ORDER BY id"))
        decisions = tuple(
            connection.execute("SELECT * FROM review_decisions ORDER BY id")
        )

        first = suppress_learned_preference(connection, learned.preference_key, now=NOW)
        suppression = list_suppressions(connection)
        second = suppress_learned_preference(
            connection, learned.preference_key, now=NOW + timedelta(hours=1)
        )
        assert first.suppressed is second.suppressed is True
        assert list_suppressions(connection) == suppression
        assert list_preference_evidence(connection) == evidence
        assert first.positive_count == second.positive_count == 2
        assert (
            tuple(connection.execute("SELECT * FROM review_events ORDER BY id"))
            == events
        )
        assert (
            tuple(connection.execute("SELECT * FROM review_decisions ORDER BY id"))
            == decisions
        )
        assert all(
            get_decision(connection, job_id=job_id) is not None for job_id in ids
        )

        restored = restore_learned_preference(connection, learned.preference_key)
        repeated = restore_learned_preference(connection, learned.preference_key)
        assert restored.active is repeated.active is True
        assert restored.suppressed is repeated.suppressed is False
        assert list_suppressions(connection) == ()
        assert list_preference_evidence(connection) == evidence

        for invalid in ("bad", "0" * 32):
            with pytest.raises(ValueError):
                suppress_learned_preference(connection, invalid, now=NOW)
            with pytest.raises(ValueError):
                restore_learned_preference(connection, invalid)


def test_global_reset_preserves_explicit_review_and_workflow_state_and_sets_boundary(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        ids = _seed(connection, ("shortlist", "shortlist"))
        replace_explicit_profile(connection, CandidateProfile(), now=NOW)
        learned = _title(inspect_preferences(connection))
        suppress_learned_preference(connection, learned.preference_key, now=NOW)
        connection.execute(
            "INSERT INTO applications(job_id, current_status, created_at, updated_at) "
            "VALUES (?, 'interested', ?, ?)",
            (ids[0], NOW.isoformat(), NOW.isoformat()),
        )
        connection.commit()
        reviews_before = tuple(connection.execute("SELECT * FROM review_decisions"))
        events_before = tuple(connection.execute("SELECT * FROM review_events"))
        applications_before = tuple(connection.execute("SELECT * FROM applications"))
        boundary = max(int(row["id"]) for row in events_before)

        state = reset_learned_preferences(connection, now=NOW + timedelta(days=1))

        assert list_preference_evidence(connection) == ()
        assert list_suppressions(connection) == ()
        assert state.learning_reset_at == (NOW + timedelta(days=1)).isoformat()
        assert state.learning_reset_review_event_id == boundary
        assert state.explicit_configured is True
        assert (
            inspect_preferences(connection).explicit.profile
            == ExplicitPreferenceProfile()
        )
        assert (
            tuple(connection.execute("SELECT * FROM review_decisions"))
            == reviews_before
        )
        assert tuple(connection.execute("SELECT * FROM review_events")) == events_before
        assert (
            tuple(connection.execute("SELECT * FROM applications"))
            == applications_before
        )

        set_decision(
            connection,
            job_id=ids[0],
            decision="shortlist",
            note="private note only",
            now=NOW + timedelta(days=2),
        )
        assert list_preference_evidence(connection) == ()
        set_decision(
            connection,
            job_id=ids[0],
            decision="dismiss",
            now=NOW + timedelta(days=3),
        )
        assert list_preference_evidence(connection)
        assert all(
            row.review_event_id > boundary
            for row in list_preference_evidence(connection)
        )


def test_reset_uses_zero_boundary_without_review_events(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        state = reset_learned_preferences(connection, now=NOW)
        assert state.learning_reset_review_event_id == 0


def test_reset_failure_rolls_back_evidence_suppression_and_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _open(tmp_path) as connection:
        _seed(connection, ("shortlist", "shortlist"))
        learned = _title(inspect_preferences(connection))
        suppress_learned_preference(connection, learned.preference_key, now=NOW)
        evidence = list_preference_evidence(connection)
        suppressions = list_suppressions(connection)
        state = preference_database.get_preference_state(connection)

        def fail(*args, **kwargs):
            raise RuntimeError("injected reset failure")

        monkeypatch.setattr(preference_database, "set_learning_reset_boundary", fail)
        with pytest.raises(RuntimeError, match="injected reset failure"):
            reset_learned_preferences(connection, now=NOW + timedelta(days=1))

        assert list_preference_evidence(connection) == evidence
        assert list_suppressions(connection) == suppressions
        assert preference_database.get_preference_state(connection) == state
