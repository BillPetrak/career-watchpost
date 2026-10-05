from __future__ import annotations

from datetime import UTC, datetime

import pytest

from careeros.cli.commands.group import run_group_regroup
from careeros.collecting import refresh_opportunity_authority
from careeros.database import preferences as preference_database
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.review_ranking import fully_prepared, get_authority
from careeros.database.schema import initialize_schema
from careeros.preferences.models import (
    ExplicitPreferenceProfile,
    explicit_ranking_authority_fingerprint,
    preference_key,
)
from careeros.preferences.service import (
    clear_explicit_profile,
    load_stored_explicit_profile,
    replace_explicit_profile,
    resolve_explicit_profile,
)
from careeros.ranking.rebuild import rebuild_once
from careeros.ranking.scorer import CandidateProfile, MatchFacts, score_facts
from careeros.review.service import set_decision
from careeros.today.service import build_today


def _connection(tmp_path):
    connection = connect(tmp_path / "preferences.sqlite3")
    initialize_schema(connection)
    return connection


def _all_fields_profile() -> CandidateProfile:
    return CandidateProfile(
        desired_titles=("Backend Engineer", "Backend Engineer", "Platform Engineer"),
        skills=("Python", "SQL", "Python"),
        required_skills=("Git", "Git"),
        preferred_locations=("Athens", "Berlin", "Athens"),
        remote_preference="remote",
        excluded_terms=("crypto", "crypto"),
        hard_excluded_terms=("unpaid", "unpaid"),
        preferred_employment_types=("full-time", "contract", "full-time"),
        preferred_seniority=("senior", "lead", "senior"),
    )


def test_explicit_profile_round_trip_preserves_every_field_order_and_duplicates(
    tmp_path,
) -> None:
    connection = _connection(tmp_path)
    profile = _all_fields_profile()
    now = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)

    replace_explicit_profile(connection, profile, now=now)

    stored = load_stored_explicit_profile(connection)
    assert stored == ExplicitPreferenceProfile.from_candidate_profile(profile)
    state = preference_database.get_preference_state(connection)
    assert state.explicit_configured is True
    assert state.explicit_updated_at == now.isoformat()
    rows = preference_database.list_explicit_preferences(connection)
    assert [(row.dimension, row.position, row.value) for row in rows[:3]] == [
        ("desired_titles", 0, "Backend Engineer"),
        ("desired_titles", 1, "Backend Engineer"),
        ("desired_titles", 2, "Platform Engineer"),
    ]


def test_configured_empty_profile_is_distinct_from_absence(tmp_path) -> None:
    connection = _connection(tmp_path)
    assert load_stored_explicit_profile(connection) is None
    absent = resolve_explicit_profile(connection)
    assert absent.source == "default"
    assert absent.explicit_profile == ExplicitPreferenceProfile()

    replace_explicit_profile(connection, CandidateProfile())
    assert load_stored_explicit_profile(connection) == ExplicitPreferenceProfile()
    configured_empty = resolve_explicit_profile(connection)
    assert configured_empty.source == "stored"
    assert configured_empty.explicit_profile == ExplicitPreferenceProfile()
    assert configured_empty.locked_dimensions == frozenset()

    clear_explicit_profile(connection)
    assert load_stored_explicit_profile(connection) is None
    assert resolve_explicit_profile(connection).source == "default"


def test_explicit_ranking_identity_preserves_order_duplicates_and_null(
    tmp_path,
) -> None:
    first = ExplicitPreferenceProfile(
        desired_titles=("Engineer", "Engineer"),
        remote_preference=None,
    )
    reordered = ExplicitPreferenceProfile(
        desired_titles=("Engineer",),
        remote_preference="remote",
    )
    assert explicit_ranking_authority_fingerprint(first) != (
        explicit_ranking_authority_fingerprint(reordered)
    )
    assert explicit_ranking_authority_fingerprint(ExplicitPreferenceProfile()) == (
        explicit_ranking_authority_fingerprint(ExplicitPreferenceProfile())
    )


def test_clear_does_not_touch_other_preference_or_review_state(tmp_path) -> None:
    connection = _connection(tmp_path)
    job_id = int(
        connection.execute(
            "INSERT INTO jobs(source, source_job_id, title, collected_at) "
            "VALUES ('test', 'one', 'Engineer', 'now')"
        ).lastrowid
    )
    event_id = int(
        connection.execute(
            "INSERT INTO review_events(job_id, old_decision, new_decision, created_at) "
            "VALUES (?, NULL, 'shortlist', 'now')",
            (job_id,),
        ).lastrowid
    )
    connection.execute(
        "INSERT INTO preference_evidence VALUES (?, ?, 'title', 'Engineer', "
        "'engineer', 1, 'now')",
        (job_id, event_id),
    )
    key = preference_key("title", "engineer")
    connection.execute(
        "INSERT INTO learned_preference_suppressions VALUES "
        "(?, 'title', 'engineer', 'now')",
        (key,),
    )
    connection.commit()
    replace_explicit_profile(connection, CandidateProfile(skills=("Python",)))

    clear_explicit_profile(connection)

    assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM preference_evidence").fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM learned_preference_suppressions"
        ).fetchone()[0]
        == 1
    )


def test_explicit_profile_precedence_including_supplied_empty(tmp_path) -> None:
    connection = _connection(tmp_path)
    stored = CandidateProfile(desired_titles=("Stored",))
    replace_explicit_profile(connection, stored)

    supplied = CandidateProfile(desired_titles=("Supplied",))
    resolved = resolve_explicit_profile(connection, supplied_profile=supplied)
    assert resolved.source == "supplied"
    assert resolved.explicit_profile.desired_titles == ("Supplied",)

    supplied_empty = resolve_explicit_profile(
        connection, supplied_profile=CandidateProfile()
    )
    assert supplied_empty.source == "supplied"
    assert supplied_empty.explicit_profile == ExplicitPreferenceProfile()

    resolved_stored = resolve_explicit_profile(connection)
    assert resolved_stored.source == "stored"
    assert resolved_stored.explicit_profile.desired_titles == ("Stored",)


def test_populated_explicit_dimensions_create_only_frozen_locks(tmp_path) -> None:
    connection = _connection(tmp_path)
    profile = CandidateProfile(
        desired_titles=("Engineer",),
        skills=("Python",),
        required_skills=("SQL",),
        excluded_terms=("unpaid",),
        hard_excluded_terms=("commission only",),
        preferred_locations=(),
        remote_preference=None,
        preferred_employment_types=("full-time",),
        preferred_seniority=("senior",),
    )
    replace_explicit_profile(connection, profile)

    assert resolve_explicit_profile(connection).locked_dimensions == frozenset(
        {"title", "employment_type", "seniority"}
    )


def test_replacement_is_atomic_and_deterministic(tmp_path, monkeypatch) -> None:
    connection = _connection(tmp_path)
    original = CandidateProfile(desired_titles=("Original",), skills=("Python",))
    replace_explicit_profile(connection, original)

    real_insert = preference_database.insert_explicit_preference
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected replacement failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(
        preference_database, "insert_explicit_preference", fail_on_second
    )
    with pytest.raises(RuntimeError, match="injected replacement failure"):
        replace_explicit_profile(
            connection,
            CandidateProfile(desired_titles=("New", "Second"), skills=("Rust",)),
        )
    assert load_stored_explicit_profile(connection) == (
        ExplicitPreferenceProfile.from_candidate_profile(original)
    )


def test_preference_key_is_stable_lowercase_truncated_sha256() -> None:
    key = preference_key("title", "backend engineer")
    assert key == preference_key("title", "backend engineer")
    assert len(key) == 32
    assert key == key.lower()
    assert set(key) <= set("0123456789abcdef")
    assert key != preference_key("company", "backend engineer")


def test_persisted_preferences_do_not_change_ranking(tmp_path) -> None:
    connection = _connection(tmp_path)
    facts = MatchFacts(titles=("Backend Engineer",), description="Python")
    run_profile = CandidateProfile(desired_titles=("Backend Engineer",))
    before = score_facts(facts, run_profile)

    replace_explicit_profile(
        connection,
        CandidateProfile(desired_titles=("Completely Different",)),
    )

    after = score_facts(facts, run_profile)
    assert after == before
    assert all(component.kind != "learned_preference" for component in after.components)


def _learned_regroup_db(tmp_path, *, with_decisions: bool = True):
    connection = _connection(tmp_path)
    for source_job_id, location in (("one", "Berlin"), ("two", "Munich")):
        connection.execute(
            """INSERT INTO jobs(
                   source, source_job_id, title, company, location, collected_at
               ) VALUES ('test', ?, 'Backend Engineer', 'Acme', ?, 'now')""",
            (source_job_id, location),
        )
    ensure_grouped(connection)
    refresh_opportunity_authority(connection)
    connection.commit()
    if with_decisions:
        set_decision(connection, job_id=1, decision="shortlist")
        set_decision(connection, job_id=2, decision="shortlist")
    return connection


def test_regroup_refreshes_learned_authority_when_effective_member_changes(
    tmp_path,
) -> None:
    connection = _learned_regroup_db(tmp_path)
    before = get_authority(connection)
    assert before.learned_fingerprint != "0" * 64
    connection.execute("UPDATE jobs SET location='Berlin' WHERE id=2")
    connection.commit()

    assert run_group_regroup(tmp_path / "preferences.sqlite3") == 0
    after = get_authority(connection)

    assert after.learned_revision == before.learned_revision + 1
    assert after.learned_fingerprint != before.learned_fingerprint
    assert after.opportunity_revision > before.opportunity_revision
    rebuild_once(connection)
    assert fully_prepared(connection)
    learned_generation = connection.execute(
        """SELECT g.learned_fingerprint
           FROM review_ranking_current AS c
           JOIN review_ranking_generations AS g ON g.id=c.generation_id
           WHERE c.learned_mode=1"""
    ).fetchone()
    assert learned_generation[0] == after.learned_fingerprint
    snapshot = build_today(connection, use_learned_preferences=True)
    assert snapshot.review_next.use_learned_preferences is True
    connection.close()


def test_unchanged_regroup_does_not_churn_learned_authority(tmp_path) -> None:
    connection = _learned_regroup_db(tmp_path, with_decisions=False)
    before = get_authority(connection)
    connection.execute("UPDATE jobs SET location='Berlin' WHERE id=2")
    connection.commit()

    assert run_group_regroup(tmp_path / "preferences.sqlite3") == 0
    after = get_authority(connection)

    assert after.learned_revision == before.learned_revision
    assert after.learned_fingerprint == before.learned_fingerprint
    assert after.opportunity_revision > before.opportunity_revision
    connection.close()
