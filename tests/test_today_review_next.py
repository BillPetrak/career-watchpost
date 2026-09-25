"""Focused R5 M2 Review-next preview contracts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast

import pytest

from careeros.api.main import create_app
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.preferences.models import ExplicitPreferenceProfile
from careeros.preferences.service import (
    build_adaptive_preference_set,
    replace_explicit_profile,
    resolve_explicit_profile,
    suppress_learned_preference,
)
from careeros.ranking.explanations import explain_result
from careeros.review.service import build_queue, set_decision
from careeros.today import service as today_service
from careeros.today.service import build_today
from tests.support import TestClient

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection


def _job(
    connection: sqlite3.Connection,
    *,
    title: str,
    company: str,
    source: str,
    published_at: str = "2026-09-01T00:00:00+00:00",
    description: str = "",
) -> int:
    cursor = connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,description,"
        "published_at,collected_at) VALUES (?,?,?,?,?,?,?)",
        (
            source,
            f"{source}-{title}-{company}",
            title,
            company,
            description,
            published_at,
            "2026-09-17T00:00:00+00:00",
        ),
    )
    return cast(int, cursor.lastrowid)


def _group(connection: sqlite3.Connection, group_id: str, members: list[int]) -> None:
    connection.execute("INSERT INTO job_groups(id) VALUES (?)", (group_id,))
    for job_id in members:
        connection.execute(
            "INSERT INTO job_group_members(job_id,group_id,reason) VALUES (?,?,'test')",
            (job_id, group_id),
        )


def _application(connection: sqlite3.Connection, job_id: int) -> None:
    connection.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) "
        "VALUES (?,'applied',?,?)",
        (job_id, NOW.isoformat(), NOW.isoformat()),
    )


def _seed_title_learning(
    connection: sqlite3.Connection,
    *,
    title: str = "Platform Engineer",
    positive_count: int = 2,
) -> list[int]:
    job_ids = [
        _job(
            connection,
            title=title,
            company=f"Learning {index}",
            source=f"learning-{index}",
        )
        for index in range(positive_count)
    ]
    connection.commit()
    for job_id in job_ids:
        set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)
    return job_ids


def _preference_persistence_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    return tuple(
        (
            table,
            tuple(
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ),
        )
        for table in (
            "preference_state",
            "explicit_preferences",
            "preference_evidence",
            "learned_preference_suppressions",
        )
    )


def _seed_preview(connection: sqlite3.Connection) -> tuple[list[int], int]:
    first = _job(
        connection,
        title="Python Engineer",
        company="Acme",
        source="first",
        published_at="2026-09-10T00:00:00+00:00",
    )
    duplicate = _job(
        connection,
        title="Python Engineer",
        company="Acme",
        source="duplicate",
        published_at="2026-09-16T00:00:00+00:00",
    )
    _group(connection, "python-acme", [first, duplicate])
    scored = [first]
    for index, day in enumerate((15, 14, 13), start=1):
        scored.append(
            _job(
                connection,
                title="Python Engineer",
                company=f"Company {index}",
                source=f"scored-{index}",
                published_at=f"2026-09-{day:02d}T00:00:00+00:00",
            )
        )
    zero = _job(
        connection,
        title="Accountant",
        company="Ledger",
        source="zero",
        published_at="2026-09-17T00:00:00+00:00",
    )
    connection.commit()
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(desired_titles=("Python Engineer",)),
        now=NOW,
    )
    return scored, zero


def test_preview_totals_limit_grouping_and_frozen_order() -> None:
    connection = _db()
    scored, _zero = _seed_preview(connection)

    review_next = build_today(connection, now=NOW, limit=1).review_next

    assert review_next.candidate_total == 5
    assert review_next.total == 4
    assert review_next.limit == 3
    assert review_next.minimum_score == 1
    assert len(review_next.items) == 3
    assert [item.group.representative.id for item in review_next.items] == scored[:3]
    assert len(review_next.items[0].group.members) == 2
    assert review_next.personalized is True


@pytest.mark.parametrize("application_member", [0, 1])
def test_any_current_group_member_application_excludes_preview(
    application_member: int,
) -> None:
    connection = _db()
    representative = _job(
        connection, title="Python Engineer", company="Acme", source="representative"
    )
    sibling = _job(
        connection, title="Python Engineer", company="Acme", source="sibling"
    )
    singleton = _job(
        connection, title="Python Engineer", company="Solo", source="singleton"
    )
    _group(connection, "group", [representative, sibling])
    _application(connection, [representative, sibling][application_member])
    connection.commit()
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(desired_titles=("Python Engineer",)),
        now=NOW,
    )

    review_next = build_today(connection, now=NOW).review_next

    assert review_next.candidate_total == 1
    assert [item.group.representative.id for item in review_next.items] == [singleton]


def test_resolved_review_excludes_whole_group() -> None:
    connection = _db()
    representative = _job(
        connection, title="Python Engineer", company="Acme", source="representative"
    )
    sibling = _job(
        connection, title="Python Engineer", company="Acme", source="sibling"
    )
    _group(connection, "group", [representative, sibling])
    connection.commit()
    set_decision(
        connection,
        job_id=sibling,
        decision="review_later",
        now=NOW,
    )
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(desired_titles=("Python Engineer",)),
        now=NOW,
    )

    review_next = build_today(connection, now=NOW).review_next

    assert review_next.candidate_total == review_next.total == 0
    assert review_next.items == ()


@pytest.mark.parametrize(
    ("configured_empty", "expected_source"),
    [(False, "default"), (True, "stored")],
)
def test_absent_and_configured_empty_preserve_learning_without_profile_fallback(
    monkeypatch: pytest.MonkeyPatch,
    configured_empty: bool,
    expected_source: str,
) -> None:
    connection = _db()
    candidate = _job(
        connection,
        title="Platform Engineer",
        company="Candidate",
        source="candidate",
    )
    connection.commit()
    _seed_title_learning(connection)
    if configured_empty:
        replace_explicit_profile(connection, ExplicitPreferenceProfile(), now=NOW)

    context = resolve_explicit_profile(connection)
    assert context.source == expected_source
    assert context.explicit_profile == ExplicitPreferenceProfile()
    assert context.locked_dimensions == frozenset()
    before = _preference_persistence_snapshot(connection)

    def fail_profile_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("Today must not load a private candidate profile")

    monkeypatch.setattr("careeros.ranking.profile.load_profile", fail_profile_load)
    review_next = build_today(connection, now=NOW).review_next

    assert review_next.candidate_total == 1
    assert review_next.total == 1
    assert review_next.items[0].group.representative.id == candidate
    assert review_next.items[0].ranked.result.preference_adjustment == 2
    assert review_next.items[0].ranked.result.adaptive is not None
    assert review_next.personalized is True
    assert _preference_persistence_snapshot(connection) == before


def test_inactive_learned_state_is_neutral_and_not_personalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _db()
    _job(
        connection,
        title="Platform Engineer",
        company="Candidate",
        source="candidate",
    )
    connection.commit()
    _seed_title_learning(connection, positive_count=1)

    context = resolve_explicit_profile(connection)
    preferences = build_adaptive_preference_set(
        connection,
        supplied_profile=context.explicit_profile,
    )
    learned_title = next(
        item
        for item in preferences.learned
        if item.dimension == "title" and item.normalized_value == "platform engineer"
    )
    assert context.source == "default"
    assert learned_title.active is False
    assert learned_title.polarity is None
    enabled_queue = build_queue(
        connection,
        profile=context.explicit_profile.to_candidate_profile(),
        min_score=0,
        feedback=None,
        adaptive_preferences=preferences,
    )
    disabled_queue = build_queue(
        connection,
        profile=context.explicit_profile.to_candidate_profile(),
        min_score=0,
        feedback=None,
        adaptive_preferences=build_adaptive_preference_set(
            connection,
            supplied_profile=context.explicit_profile,
            enabled=False,
        ),
    )
    assert [item.group.representative.id for item in enabled_queue.items] == [
        item.group.representative.id for item in disabled_queue.items
    ]
    assert [item.ranked.result for item in enabled_queue.items] == [
        item.ranked.result for item in disabled_queue.items
    ]
    assert all(item.ranked.result.adaptive is None for item in enabled_queue.items)

    def fail_profile_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("Today must not load a private candidate profile")

    monkeypatch.setattr("careeros.ranking.profile.load_profile", fail_profile_load)
    adaptive = build_today(connection, now=NOW).review_next
    opted_out = build_today(
        connection,
        now=NOW,
        use_learned_preferences=False,
    ).review_next

    assert adaptive.candidate_total == opted_out.candidate_total == 1
    assert adaptive.total == opted_out.total == 0
    assert adaptive.items == opted_out.items == ()
    assert adaptive.personalized is False
    assert opted_out.personalized is False


def test_explicit_title_authority_locks_matching_learned_title() -> None:
    connection = _db()
    candidate = _job(
        connection,
        title="Platform Engineer",
        company="Candidate",
        source="candidate",
    )
    connection.commit()
    _seed_title_learning(connection)
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(desired_titles=("Platform Engineer",)),
        now=NOW,
    )

    context = resolve_explicit_profile(connection)
    preferences = build_adaptive_preference_set(
        connection,
        supplied_profile=context.explicit_profile,
    )
    assert context.source == "stored"
    assert preferences.locked_dimensions == {"title"}
    assert any(
        item.dimension == "title" and item.active for item in preferences.learned
    )

    snapshot = build_today(connection, now=NOW).review_next
    opted_out = build_today(
        connection,
        now=NOW,
        use_learned_preferences=False,
    ).review_next
    shared = build_queue(
        connection,
        profile=context.explicit_profile.to_candidate_profile(),
        min_score=1,
        limit=3,
        feedback=None,
        adaptive_preferences=preferences,
    )
    unlocked = build_queue(
        connection,
        profile=context.explicit_profile.to_candidate_profile(),
        min_score=1,
        limit=3,
        feedback=None,
        adaptive_preferences=type(preferences)(
            learned=preferences.learned,
            locked_dimensions=frozenset(),
        ),
    )

    assert snapshot.items[0].group.representative.id == candidate
    assert snapshot.items[0].ranked.result == shared.items[0].ranked.result
    assert snapshot.items[0].ranked.result.matched_titles == ("Platform Engineer",)
    assert snapshot.items[0].ranked.result.adaptive is None
    assert snapshot.items[0].ranked.match_score == opted_out.items[0].ranked.match_score
    assert unlocked.items[0].ranked.match_score > snapshot.items[0].ranked.match_score
    assert snapshot.personalized is True
    assert opted_out.personalized is True
    assert snapshot.use_learned_preferences is True
    assert opted_out.use_learned_preferences is False


def test_active_nonmatching_learning_has_zero_effective_adjustment() -> None:
    connection = _db()
    candidate = _job(
        connection,
        title="Systems Engineer",
        company="Candidate",
        source="candidate",
        description="Python",
    )
    connection.commit()
    _seed_title_learning(connection)
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(skills=("Python",)),
        now=NOW,
    )

    preferences = build_adaptive_preference_set(connection)
    assert any(
        item.dimension == "title" and item.active for item in preferences.learned
    )
    adaptive = build_today(connection, now=NOW).review_next
    opted_out = build_today(
        connection,
        now=NOW,
        use_learned_preferences=False,
    ).review_next

    item = adaptive.items[0]
    assert item.group.representative.id == candidate
    assert item.ranked.match_score > 0
    assert item.ranked.result.preference_adjustment == 0
    assert item.ranked.result.adaptive is None
    assert item.ranked.result == opted_out.items[0].ranked.result
    assert not any(
        component.kind == "learned_preference"
        for component in item.ranked.result.components
    )
    explanation = explain_result(item.ranked.result)
    assert explanation.score.preference_adjustment == 0
    assert not any(
        factor.criterion == "learned_preference" for factor in explanation.other_factors
    )


def test_learned_default_opt_out_and_suppression() -> None:
    connection = _db()
    candidate = _job(
        connection,
        title="Platform Engineer",
        company="Candidate",
        source="candidate",
        published_at="2026-09-01T00:00:00+00:00",
        description="Python",
    )
    newer = _job(
        connection,
        title="Systems Engineer",
        company="Newer",
        source="newer",
        published_at="2026-09-16T00:00:00+00:00",
        description="Python",
    )
    positive_ids = [
        _job(
            connection,
            title="Platform Engineer",
            company=f"Positive {index}",
            source=f"positive-{index}",
        )
        for index in range(2)
    ]
    connection.commit()
    for job_id in positive_ids:
        set_decision(connection, job_id=job_id, decision="shortlist", now=NOW)
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(skills=("Python",)),
        now=NOW,
    )

    adaptive = build_today(connection, now=NOW).review_next
    opted_out = build_today(
        connection,
        now=NOW,
        use_learned_preferences=False,
    ).review_next

    assert adaptive.candidate_total == opted_out.candidate_total == 2
    assert adaptive.total == opted_out.total == 2
    assert adaptive.items[0].group.representative.id == candidate
    assert opted_out.items[0].group.representative.id == newer
    assert adaptive.items[0].ranked.match_score > opted_out.items[1].ranked.match_score
    assert adaptive.items[0].ranked.result.feedback is None
    assert adaptive.items[0].ranked.result.adaptive is not None
    assert adaptive.personalized is True
    assert all(item.ranked.result.adaptive is None for item in opted_out.items)
    assert opted_out.personalized is True
    assert opted_out.use_learned_preferences is False

    preference = next(
        item
        for item in build_adaptive_preference_set(connection).learned
        if item.dimension == "title" and item.active
    )
    suppress_learned_preference(connection, preference.preference_key, now=NOW)
    suppressed = build_today(connection, now=NOW).review_next
    assert suppressed.total == 2
    assert suppressed.items[0].group.representative.id == newer
    assert suppressed.personalized is True


def test_hard_exclusion_remains_a_final_veto() -> None:
    connection = _db()
    _job(
        connection,
        title="Python Engineer",
        company="Acme",
        source="candidate",
        description="Python internship",
    )
    connection.commit()
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(
            desired_titles=("Python Engineer",),
            skills=("Python",),
            hard_excluded_terms=("internship",),
        ),
        now=NOW,
    )

    review_next = build_today(connection, now=NOW).review_next

    assert review_next.candidate_total == 1
    assert review_next.total == 0
    assert review_next.items == ()


def test_today_matches_shared_queue_without_legacy_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _db()
    _seed_preview(connection)
    calls: list[object] = []
    real_build_queue = today_service.build_queue

    def recording_build_queue(*args: object, **kwargs: object):
        calls.append(kwargs.get("feedback"))
        return real_build_queue(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(today_service, "build_queue", recording_build_queue)
    snapshot = build_today(connection, now=NOW)

    context = resolve_explicit_profile(connection)
    preferences = build_adaptive_preference_set(
        connection,
        supplied_profile=context.explicit_profile,
    )
    shared = build_queue(
        connection,
        profile=context.explicit_profile.to_candidate_profile(),
        min_score=1,
        limit=3,
        feedback=None,
        adaptive_preferences=preferences,
    )
    assert calls == [None]
    assert [item.ranked.match_score for item in snapshot.review_next.items] == [
        item.ranked.match_score for item in shared.items
    ]
    assert [item.ranked.result for item in snapshot.review_next.items] == [
        item.ranked.result for item in shared.items
    ]


def test_review_next_queries_do_not_scale_with_candidate_count() -> None:
    def count_for(candidate_count: int) -> int:
        connection = _db()
        for index in range(candidate_count):
            _job(
                connection,
                title="Python Engineer",
                company=f"Company {index}",
                source=f"source-{index}",
            )
        connection.commit()
        replace_explicit_profile(
            connection,
            ExplicitPreferenceProfile(desired_titles=("Python Engineer",)),
            now=NOW,
        )
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        build_today(connection, now=NOW)
        connection.set_trace_callback(None)
        assert not any("saved_search" in statement for statement in statements)
        return len(statements)

    assert count_for(1) == count_for(25)


def test_today_builds_one_preference_snapshot_and_passes_it_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _db()
    _seed_preview(connection)
    resolve_calls = 0
    adaptive_calls = 0
    built_preferences: list[object] = []
    queue_preferences: list[object] = []
    real_resolve = today_service.resolve_explicit_profile
    real_build_adaptive = today_service.build_adaptive_preference_set
    real_build_queue = today_service.build_queue

    def recording_resolve(*args: object, **kwargs: object):
        nonlocal resolve_calls
        resolve_calls += 1
        return real_resolve(*args, **kwargs)  # type: ignore[arg-type]

    def recording_build_adaptive(*args: object, **kwargs: object):
        nonlocal adaptive_calls
        adaptive_calls += 1
        preferences = real_build_adaptive(*args, **kwargs)  # type: ignore[arg-type]
        built_preferences.append(preferences)
        return preferences

    def recording_build_queue(*args: object, **kwargs: object):
        queue_preferences.append(kwargs.get("adaptive_preferences"))
        return real_build_queue(*args, **kwargs)  # type: ignore[arg-type]

    def unexpected_review_rebuild(*args: object, **kwargs: object) -> None:
        raise AssertionError("Review queue must consume Today's preference snapshot")

    monkeypatch.setattr(today_service, "resolve_explicit_profile", recording_resolve)
    monkeypatch.setattr(
        today_service,
        "build_adaptive_preference_set",
        recording_build_adaptive,
    )
    monkeypatch.setattr(today_service, "build_queue", recording_build_queue)
    monkeypatch.setattr(
        "careeros.review.service.build_adaptive_preference_set",
        unexpected_review_rebuild,
    )

    review_next = build_today(connection, now=NOW).review_next

    assert review_next.candidate_total == 5
    assert resolve_calls == 1
    assert adaptive_calls == 1
    assert len(built_preferences) == 1
    assert queue_preferences == [built_preferences[0]]


def test_review_next_does_not_execute_saved_search_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _db()
    _seed_preview(connection)

    def unexpected_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("Today Review next must remain local computation")

    monkeypatch.setattr(
        "careeros.saved_searches.execution.execute_search", unexpected_call
    )
    monkeypatch.setattr(
        "careeros.saved_searches.execution.execute_rank", unexpected_call
    )
    monkeypatch.setattr("careeros.collectors.http.urlopen", unexpected_call)

    review_next = build_today(connection, now=NOW).review_next

    assert review_next.total == 4


def test_api_exact_public_projection_and_adaptive_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "today-review-next.sqlite3"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    _job(connection, title="Python Engineer", company="Acme", source="candidate")
    connection.commit()
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(desired_titles=("Python Engineer",)),
        now=NOW,
    )
    connection.close()
    monkeypatch.setenv("CAREEROS_DATABASE", str(database))

    response = TestClient(create_app()).get(
        "/today?limit=1&use_learned_preferences=false"
    )
    assert response.status_code == 200
    review_next = response.json()["review"]["next"]
    assert set(review_next) == {
        "candidate_total",
        "total",
        "limit",
        "minimum_score",
        "personalized",
        "use_learned_preferences",
        "items",
    }
    assert review_next["limit"] == 3
    assert review_next["minimum_score"] == 1
    assert review_next["use_learned_preferences"] is False
    item = review_next["items"][0]
    assert set(item) == {
        "group",
        "match_score",
        "score",
        "review_decision",
        "application_status",
        "explanation",
    }
    assert item["review_decision"] is None
    assert item["application_status"] is None
    serialized = str(review_next).lower()
    for private_key in (
        "note",
        "profile_path",
        "score_components",
        "preference_key",
        "positive_examples",
        "negative_examples",
    ):
        assert private_key not in serialized
    assert SCHEMA_VERSION == 13
