"""Focused R4 M2 review-driven learning coverage."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.preferences import list_preference_evidence
from careeros.database.schema import initialize_schema
from careeros.preferences.learning import LearnedPreference, derive_learned_preferences
from careeros.preferences.models import preference_key
from careeros.ranking.feedback import CandidateJob, FeedbackSet, FeedbackSource
from careeros.ranking.scorer import CandidateProfile, MatchFacts, score_facts
from careeros.review.service import clear_decision, get_decision, set_decision

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)
TITLE_SIGNATURE = "senior python engineer"


def _job(
    source_id: str,
    *,
    source: str = "arbeitnow",
    title: str = "Senior Python Engineer",
    company: str | None = "Acme GmbH",
    location: str | None = "Berlin",
    description: str | None = "secret description skill",
    url: str | None = None,
    employment_type: str | None = "Full-time, Contract",
    remote_status: str | None = "remote",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=url or f"https://example.test/private/{source_id}",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=None,
        collected_at=NOW.isoformat(),
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "learning.sqlite3")
    initialize_schema(connection)
    return connection


def _ids(connection: sqlite3.Connection) -> list[int]:
    return [
        int(row[0]) for row in connection.execute("SELECT id FROM jobs ORDER BY id")
    ]


def _event_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0])


def _title_preference(connection: sqlite3.Connection) -> LearnedPreference:
    return next(
        item
        for item in derive_learned_preferences(connection)
        if item.dimension == "title" and item.normalized_value == TITLE_SIGNATURE
    )


def _seed_distinct_signals(
    connection: sqlite3.Connection, decisions: tuple[str, ...]
) -> None:
    jobs = [
        _job(
            f"signal-{index}",
            company=f"Company {index}",
            location=f"City {index}",
        )
        for index in range(len(decisions))
    ]
    save_jobs(connection, jobs)
    ensure_grouped(connection)
    for job_id, decision in zip(_ids(connection), decisions, strict=True):
        set_decision(connection, job_id=job_id, decision=decision, now=NOW)


def test_first_signals_and_transitions_replace_the_evidence_snapshot(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job("shortlist"), _job("dismiss", company="Other")])
        shortlist_id, dismiss_id = _ids(connection)

        set_decision(connection, job_id=shortlist_id, decision="shortlist", now=NOW)
        first_positive = list_preference_evidence(connection)
        assert first_positive
        assert {row.job_id for row in first_positive} == {shortlist_id}
        assert {row.signal for row in first_positive} == {1}
        positive_event_id = first_positive[0].review_event_id
        assert {row.review_event_id for row in first_positive} == {positive_event_id}

        set_decision(
            connection,
            job_id=shortlist_id,
            decision="dismiss",
            now=NOW + timedelta(minutes=1),
        )
        replacement = tuple(
            row
            for row in list_preference_evidence(connection)
            if row.job_id == shortlist_id
        )
        assert replacement
        assert {row.signal for row in replacement} == {-1}
        assert {row.review_event_id for row in replacement} != {positive_event_id}

        set_decision(connection, job_id=dismiss_id, decision="dismiss", now=NOW)
        first_negative = tuple(
            row
            for row in list_preference_evidence(connection)
            if row.job_id == dismiss_id
        )
        assert first_negative
        assert {row.signal for row in first_negative} == {-1}

        set_decision(
            connection,
            job_id=dismiss_id,
            decision="shortlist",
            now=NOW + timedelta(minutes=2),
        )
        positive_replacement = tuple(
            row
            for row in list_preference_evidence(connection)
            if row.job_id == dismiss_id
        )
        assert positive_replacement
        assert {row.signal for row in positive_replacement} == {1}


def test_review_later_and_clear_create_no_signal_and_remove_evidence(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job("later"), _job("clear", company="Other")])
        later_id, clear_id = _ids(connection)

        set_decision(connection, job_id=later_id, decision="review_later", now=NOW)
        assert list_preference_evidence(connection) == ()

        set_decision(
            connection,
            job_id=later_id,
            decision="shortlist",
            now=NOW + timedelta(minutes=1),
        )
        assert any(
            row.job_id == later_id for row in list_preference_evidence(connection)
        )
        set_decision(
            connection,
            job_id=later_id,
            decision="review_later",
            now=NOW + timedelta(minutes=2),
        )
        assert not any(
            row.job_id == later_id for row in list_preference_evidence(connection)
        )

        set_decision(connection, job_id=clear_id, decision="dismiss", now=NOW)
        assert any(
            row.job_id == clear_id for row in list_preference_evidence(connection)
        )
        clear_decision(connection, job_id=clear_id, now=NOW + timedelta(minutes=3))
        assert not any(
            row.job_id == clear_id for row in list_preference_evidence(connection)
        )
        assert get_decision(connection, job_id=clear_id) is None


def test_same_decision_actions_never_rewrite_evidence(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job("same")])
        job_id = _ids(connection)[0]
        set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)
        before_evidence = list_preference_evidence(connection)
        before_events = _event_count(connection)
        before_decision = get_decision(connection, job_id=job_id)
        assert before_decision is not None

        no_note = set_decision(
            connection,
            job_id=job_id,
            decision="shortlist",
            now=NOW + timedelta(hours=1),
        )
        assert no_note.changed is False
        assert _event_count(connection) == before_events
        assert list_preference_evidence(connection) == before_evidence
        assert get_decision(connection, job_id=job_id) == before_decision

        with_note = set_decision(
            connection,
            job_id=job_id,
            decision="shortlist",
            note="private audit note",
            now=NOW + timedelta(hours=2),
        )
        assert with_note.changed is True
        assert _event_count(connection) == before_events + 1
        assert list_preference_evidence(connection) == before_evidence
        latest = connection.execute(
            "SELECT note FROM review_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert latest["note"] == "private audit note"


@pytest.mark.parametrize(
    ("decisions", "positive", "negative", "net", "active", "polarity", "strength"),
    [
        (("dismiss",), 0, 1, -1, False, None, 0),
        (("dismiss", "dismiss"), 0, 2, -2, False, None, 0),
        (("dismiss", "dismiss", "dismiss"), 0, 3, -3, True, "negative", 2),
        (
            ("dismiss", "dismiss", "dismiss", "shortlist"),
            1,
            3,
            -2,
            True,
            "negative",
            1,
        ),
        (("shortlist", "shortlist"), 2, 0, 2, True, "positive", 1),
        (("shortlist", "shortlist", "dismiss"), 2, 1, 1, False, None, 0),
        (("shortlist", "dismiss", "dismiss"), 1, 2, -1, False, None, 0),
        (("shortlist", "shortlist", "shortlist"), 3, 0, 3, True, "positive", 2),
        (("shortlist",) * 6, 6, 0, 6, True, "positive", 2),
    ],
)
def test_activation_contradiction_and_bounded_strength(
    tmp_path: Path,
    decisions: tuple[str, ...],
    positive: int,
    negative: int,
    net: int,
    active: bool,
    polarity: str | None,
    strength: int,
) -> None:
    with _open(tmp_path) as connection:
        _seed_distinct_signals(connection, decisions)
        learned = _title_preference(connection)
        assert (
            learned.positive_count,
            learned.negative_count,
            learned.net_evidence,
            learned.active,
            learned.polarity,
            learned.strength_level,
        ) == (positive, negative, net, active, polarity, strength)


def test_extraction_uses_only_frozen_structured_dimensions(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _job(
                    "structured",
                    title="Senior Python Engineer",
                    company="The Acme GmbH",
                    location="Berlin, Germany",
                    description="private-description-token",
                    url="https://private.example/secret",
                    employment_type=(
                        "Full-time, full_time, FULL-TIME, Contract, contract"
                    ),
                    remote_status="remote",
                ),
                _job(
                    "ambiguous",
                    title="",
                    company=None,
                    location="Remote / Anywhere Worldwide",
                    description="sensitive-description-token",
                    employment_type=None,
                    remote_status="onsite_or_unspecified",
                ),
            ],
        )
        structured_id, ambiguous_id = _ids(connection)
        set_decision(
            connection,
            job_id=structured_id,
            decision="shortlist",
            note="private-review-note",
            now=NOW,
        )
        rows = tuple(
            row
            for row in list_preference_evidence(connection)
            if row.job_id == structured_id
        )
        assert {(row.dimension, row.normalized_value) for row in rows} == {
            ("title", "senior python engineer"),
            ("company", "acme"),
            ("location", "berlin germany"),
            ("remote", "remote"),
            ("employment_type", "full time"),
            ("employment_type", "contract"),
            ("seniority", "senior"),
        }
        assert len([row for row in rows if row.dimension == "employment_type"]) == 2
        serialized = " ".join(
            f"{row.dimension} {row.value} {row.normalized_value}" for row in rows
        )
        for excluded in (
            "private-description-token",
            "private.example",
            "private-review-note",
            "arbeitnow",
        ):
            assert excluded not in serialized

        set_decision(connection, job_id=ambiguous_id, decision="shortlist", now=NOW)
        assert not any(
            row.job_id == ambiguous_id for row in list_preference_evidence(connection)
        )


def test_canonical_duplicates_contribute_once_using_lowest_decided_job(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _job("lower", source="arbeitnow"),
                _job("higher", source="remoteok"),
            ],
        )
        ensure_grouped(connection)
        lower_id, higher_id = _ids(connection)
        set_decision(connection, job_id=lower_id, decision="shortlist", now=NOW)
        set_decision(connection, job_id=higher_id, decision="dismiss", now=NOW)

        learned = _title_preference(connection)
        assert learned.positive_count == 1
        assert learned.negative_count == 0
        assert [example.job_id for example in learned.positive_examples] == [lower_id]


def test_pre_boundary_lowest_decided_job_still_owns_duplicate_opportunity(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _job("legacy-lower", source="arbeitnow"),
                _job("post-boundary-higher", source="remoteok"),
            ],
        )
        ensure_grouped(connection)
        lower_id, higher_id = _ids(connection)
        set_decision(connection, job_id=lower_id, decision="shortlist", now=NOW)
        boundary = int(
            connection.execute("SELECT MAX(id) FROM review_events").fetchone()[0]
        )
        connection.execute("DELETE FROM preference_evidence")
        connection.execute(
            "UPDATE preference_state SET learning_reset_review_event_id = ?",
            (boundary,),
        )
        connection.commit()

        set_decision(connection, job_id=higher_id, decision="dismiss", now=NOW)
        assert any(
            row.job_id == higher_id for row in list_preference_evidence(connection)
        )
        assert derive_learned_preferences(connection) == ()


def test_learning_failure_rolls_back_transition_event_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job("atomic")])
        job_id = _ids(connection)[0]
        set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)
        decision_before = get_decision(connection, job_id=job_id)
        events_before = tuple(connection.execute("SELECT * FROM review_events"))
        evidence_before = list_preference_evidence(connection)

        def fail(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected learning failure")

        monkeypatch.setattr("careeros.review.service.replace_review_evidence", fail)
        with pytest.raises(RuntimeError, match="injected learning failure"):
            set_decision(
                connection,
                job_id=job_id,
                decision="dismiss",
                now=NOW + timedelta(hours=1),
            )

        assert get_decision(connection, job_id=job_id) == decision_before
        assert tuple(connection.execute("SELECT * FROM review_events")) == events_before
        assert list_preference_evidence(connection) == evidence_before


def test_learning_failure_rolls_back_first_decision_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job("atomic-first")])
        job_id = _ids(connection)[0]

        def fail(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected first-signal failure")

        monkeypatch.setattr("careeros.review.service.replace_review_evidence", fail)
        with pytest.raises(RuntimeError, match="injected first-signal failure"):
            set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)

        assert get_decision(connection, job_id=job_id) is None
        assert _event_count(connection) == 0
        assert list_preference_evidence(connection) == ()


@pytest.mark.parametrize("operation", ["review_later", "clear"])
def test_learning_failure_rolls_back_evidence_removal_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job(f"atomic-{operation}")])
        job_id = _ids(connection)[0]
        set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)
        decision_before = get_decision(connection, job_id=job_id)
        events_before = tuple(connection.execute("SELECT * FROM review_events"))
        evidence_before = list_preference_evidence(connection)

        def fail(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected evidence-removal failure")

        monkeypatch.setattr("careeros.review.service.remove_review_evidence", fail)
        with pytest.raises(RuntimeError, match="injected evidence-removal failure"):
            if operation == "review_later":
                set_decision(
                    connection,
                    job_id=job_id,
                    decision="review_later",
                    now=NOW + timedelta(hours=1),
                )
            else:
                clear_decision(connection, job_id=job_id, now=NOW + timedelta(hours=1))

        assert get_decision(connection, job_id=job_id) == decision_before
        assert tuple(connection.execute("SELECT * FROM review_events")) == events_before
        assert list_preference_evidence(connection) == evidence_before


def test_reset_boundary_never_seeds_or_reinterprets_old_review_history(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(connection, [_job("legacy")])
        job_id = _ids(connection)[0]
        set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)
        old_event_id = list_preference_evidence(connection)[0].review_event_id
        connection.execute(
            "UPDATE preference_state SET learning_reset_review_event_id = ?",
            (old_event_id,),
        )
        connection.commit()

        assert derive_learned_preferences(connection) == ()
        set_decision(
            connection,
            job_id=job_id,
            decision="shortlist",
            note="post-boundary audit only",
            now=NOW + timedelta(minutes=1),
        )
        assert derive_learned_preferences(connection) == ()

        set_decision(
            connection,
            job_id=job_id,
            decision="dismiss",
            now=NOW + timedelta(minutes=2),
        )
        evidence = list_preference_evidence(connection)
        assert evidence
        assert {row.signal for row in evidence} == {-1}
        assert {row.review_event_id > old_event_id for row in evidence} == {True}
        learned = _title_preference(connection)
        assert learned.negative_count == 1
        assert learned.active is False


def test_suppression_preserves_counts_and_evidence_but_disables_activation(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        _seed_distinct_signals(connection, ("shortlist", "shortlist"))
        active = _title_preference(connection)
        evidence_before = list_preference_evidence(connection)
        assert active.active is True

        connection.execute(
            """
            INSERT INTO learned_preference_suppressions(
                preference_key, dimension, normalized_value, suppressed_at
            ) VALUES (?, 'title', ?, ?)
            """,
            (
                preference_key("title", TITLE_SIGNATURE),
                TITLE_SIGNATURE,
                NOW.isoformat(),
            ),
        )
        connection.commit()

        suppressed = _title_preference(connection)
        assert list_preference_evidence(connection) == evidence_before
        assert suppressed.positive_count == active.positive_count == 2
        assert suppressed.negative_count == active.negative_count == 0
        assert suppressed.net_evidence == active.net_evidence == 2
        assert suppressed.polarity == "positive"
        assert suppressed.strength_level == 1
        assert suppressed.suppressed is True
        assert suppressed.active is False


def test_provenance_is_exact_bounded_and_contains_only_safe_references(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        _seed_distinct_signals(connection, ("shortlist",) * 5 + ("dismiss",) * 5)
        learned = _title_preference(connection)
        assert learned.positive_count == 5
        assert learned.negative_count == 5
        assert len(learned.positive_examples) == 3
        assert len(learned.negative_examples) == 3
        assert {
            key
            for example in learned.positive_examples + learned.negative_examples
            for key in example.__dict__
        } == {"job_id", "title", "company"}
        assert "secret description skill" not in repr(learned)
        assert "example.test" not in repr(learned)


def test_learning_does_not_change_ranking_or_direct_feedback_explanations(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        _seed_distinct_signals(connection, ("shortlist", "shortlist"))
        assert _title_preference(connection).active is True

        facts = MatchFacts(
            titles=("Senior Python Engineer",),
            description="Python",
            location="Berlin",
            remote_status="remote",
            employment_type="Full-time",
        )
        profile = CandidateProfile(skills=("Python",))
        candidate = CandidateJob(
            job_ids=frozenset({999}),
            titles=facts.titles,
            company="Acme",
            location=facts.location,
            remote_status=facts.remote_status,
            employment_type=facts.employment_type,
        )
        feedback = FeedbackSet(
            sources=(
                FeedbackSource(
                    job_id=1000,
                    title="Senior Python Engineer",
                    company="Acme",
                    location="Berlin",
                    remote_status="remote",
                    employment_type="Full-time",
                    decision="shortlist",
                ),
            )
        )
        plain = score_facts(facts, profile, candidate=candidate)
        direct = score_facts(facts, profile, feedback=feedback, candidate=candidate)
        repeated = score_facts(facts, profile, feedback=feedback, candidate=candidate)
        assert repeated == direct
        assert direct.score == plain.score + 5
        assert [component.kind for component in direct.components].count(
            "feedback"
        ) == 1
        assert all(
            component.kind != "learned_preference" for component in direct.components
        )
