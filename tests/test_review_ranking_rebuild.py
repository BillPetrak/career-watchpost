from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.api.main import create_app
from careeros.cli.main import main
from careeros.database.connection import connect
from careeros.database.review_ranking import (
    MODE_WITH_LEARNED,
    MODE_WITHOUT_LEARNED,
    REVIEW_RANKING_SCORER_VERSION,
    RankingDerivedStateError,
    claim,
    fully_prepared,
    get_authority,
    heartbeat,
    list_generation_items,
    reconcile_authority,
    update_learned_authority,
    update_opportunity_authority,
    validate_current_generations,
)
from careeros.database.schema import initialize_schema
from careeros.preferences.models import (
    ExplicitPreferenceProfile,
    explicit_ranking_authority_fingerprint,
)
from careeros.preferences.service import (
    effective_learned_authority_fingerprint,
    replace_explicit_profile,
    resolve_explicit_profile,
)
from careeros.ranking.materialization import (
    materialize_generation,
    opportunity_corpus_fingerprint,
    snapshot_opportunities,
)
from careeros.ranking.rebuild import (
    rebuild_database,
    rebuild_once,
    recover_and_rebuild,
)
from tests.support import TestClient


def _seed_database(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    initialize_schema(connection)
    connection.execute(
        """INSERT INTO jobs(
               source,source_job_id,title,company,location,description,url,
               employment_type,remote_status,published_at,collected_at,
               created_at,updated_at
           ) VALUES
               ('test','older','Python Engineer','Acme','Athens',
                'Python APIs','https://example.test/older','Full-time','remote',
                '2025-01-01T00:00:00+00:00','n','n','n'),
               ('test','newer','Rust Engineer','Beta','Berlin',
                'Rust systems','https://example.test/newer','Full-time','onsite',
                '2026-01-01T00:00:00+00:00','n','n','n')"""
    )
    connection.commit()
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(
            desired_titles=("Python Engineer",),
            skills=("Python",),
        ),
    )
    return connection


def _current_generation_ids(connection: sqlite3.Connection) -> dict[int, int]:
    return {
        int(row["learned_mode"]): int(row["generation_id"])
        for row in connection.execute(
            """SELECT learned_mode,generation_id
               FROM review_ranking_current ORDER BY learned_mode"""
        )
    }


_PRODUCT_ONE_AUTHORITY_QUERIES = {
    "jobs": "SELECT * FROM jobs ORDER BY id",
    "job_groups": "SELECT * FROM job_groups ORDER BY id",
    "job_group_members": ("SELECT * FROM job_group_members ORDER BY group_id, job_id"),
    "preference_state": (
        "SELECT singleton, explicit_configured, explicit_updated_at, "
        "learning_reset_at, learning_reset_review_event_id, created_at "
        "FROM preference_state ORDER BY singleton"
    ),
    "explicit_preferences": (
        "SELECT * FROM explicit_preferences ORDER BY dimension, position"
    ),
    "preference_evidence": (
        "SELECT * FROM preference_evidence ORDER BY job_id, dimension, normalized_value"
    ),
    "learned_preference_suppressions": (
        "SELECT * FROM learned_preference_suppressions ORDER BY preference_key"
    ),
    "review_decisions": "SELECT * FROM review_decisions ORDER BY job_id",
    "review_events": "SELECT * FROM review_events ORDER BY id",
    "applications": "SELECT * FROM applications ORDER BY id",
    "application_events": "SELECT * FROM application_events ORDER BY id",
}


def _product_one_authority_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        table: tuple(tuple(row) for row in connection.execute(query))
        for table, query in _PRODUCT_ONE_AUTHORITY_QUERIES.items()
    }


def _assert_preference_ranking_metadata_coherent(
    connection: sqlite3.Connection,
) -> None:
    state = connection.execute(
        "SELECT typeof(explicit_ranking_revision), explicit_ranking_revision, "
        "explicit_ranking_fingerprint FROM preference_state WHERE singleton=1"
    ).fetchone()
    assert state is not None
    authority = get_authority(connection)
    explicit = resolve_explicit_profile(connection).explicit_profile
    fingerprint = explicit_ranking_authority_fingerprint(explicit)
    assert tuple(state) == (
        "integer",
        authority.explicit_revision,
        fingerprint,
    )
    assert authority.explicit_fingerprint == fingerprint


def _assert_generation_target_modes_unique(connection: sqlite3.Connection) -> None:
    duplicates = connection.execute(
        """SELECT target_revision,learned_mode,count(*)
           FROM review_ranking_generations
           GROUP BY target_revision,learned_mode
           HAVING count(*) > 1"""
    ).fetchall()
    assert duplicates == []


def test_rebuild_scores_both_modes_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ranking.sqlite3"
    connection = _seed_database(path)

    result = rebuild_once(connection)

    assert result is not None
    assert result.mode_mask == 3
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    current = _current_generation_ids(connection)
    assert set(current) == {MODE_WITHOUT_LEARNED, MODE_WITH_LEARNED}
    for generation_id in current.values():
        items = list_generation_items(connection, generation_id)
        assert [item.rank_ordinal for item in items] == [1, 2]
        assert items[0].representative_job_id == 1

    assert rebuild_once(connection) is None
    assert _current_generation_ids(connection) == current


def test_learned_only_rebuild_reuses_mode_zero(tmp_path: Path) -> None:
    connection = _seed_database(tmp_path / "learned.sqlite3")
    assert rebuild_once(connection) is not None
    original = _current_generation_ids(connection)

    assert update_learned_authority(connection, "1" * 64)
    connection.commit()
    result = rebuild_once(connection)

    assert result is not None
    assert result.mode_mask == 2
    current = _current_generation_ids(connection)
    assert current[MODE_WITHOUT_LEARNED] == original[MODE_WITHOUT_LEARNED]
    assert current[MODE_WITH_LEARNED] != original[MODE_WITH_LEARNED]
    assert fully_prepared(connection)
    assert validate_current_generations(connection)


def test_recovery_replaces_protected_same_target_generations_for_all_modes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-target-all.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    target = get_authority(connection).target_revision
    original = _current_generation_ids(connection)
    authoritative_before = _product_one_authority_snapshot(connection)
    connection.execute(
        "UPDATE review_ranking_authority "
        "SET state='rebuild_required', required_mode_mask=3"
    )
    connection.commit()
    assert get_authority(connection).target_revision == target
    assert _current_generation_ids(connection) == original

    result = recover_and_rebuild(connection)

    assert result is not None
    assert result.target_revision == target
    assert result.mode_mask == 3
    authority = get_authority(connection)
    assert authority.state == "ready"
    assert authority.required_mode_mask == 0
    current = _current_generation_ids(connection)
    assert set(current) == {MODE_WITHOUT_LEARNED, MODE_WITH_LEARNED}
    assert current[MODE_WITHOUT_LEARNED] != original[MODE_WITHOUT_LEARNED]
    assert current[MODE_WITH_LEARNED] != original[MODE_WITH_LEARNED]
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    _assert_generation_target_modes_unique(connection)
    assert _product_one_authority_snapshot(connection) == authoritative_before
    assert TestClient(create_app(database=path)).get("/today").status_code == 200


def test_learned_only_recovery_preserves_protected_same_target_mode_zero(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "same-target-learned.sqlite3")
    assert rebuild_once(connection) is not None
    target = get_authority(connection).target_revision
    original = _current_generation_ids(connection)
    connection.execute(
        "UPDATE review_ranking_authority "
        "SET state='rebuild_required', required_mode_mask=2"
    )
    connection.commit()

    result = recover_and_rebuild(connection)

    assert result is not None
    assert result.target_revision == target
    assert result.mode_mask == 2
    current = _current_generation_ids(connection)
    assert current[MODE_WITHOUT_LEARNED] == original[MODE_WITHOUT_LEARNED]
    assert current[MODE_WITH_LEARNED] != original[MODE_WITH_LEARNED]
    assert (
        connection.execute(
            "SELECT 1 FROM review_ranking_generations WHERE id=?",
            (original[MODE_WITH_LEARNED],),
        ).fetchone()
        is None
    )
    authority = get_authority(connection)
    assert authority.state == "ready"
    assert authority.required_mode_mask == 0
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    _assert_generation_target_modes_unique(connection)


def test_retryable_failed_same_target_recovery_preserves_backoff_contract(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "same-target-failed.sqlite3")
    assert rebuild_once(connection) is not None
    original = _current_generation_ids(connection)
    connection.execute(
        """UPDATE review_ranking_authority
           SET state='failed', required_mode_mask=3, attempt_count=1,
               failure_code='database', last_failed_at=?, retry_not_before=?""",
        ("2026-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"),
    )
    connection.commit()

    assert recover_and_rebuild(connection) is None
    assert _current_generation_ids(connection) == original
    connection.execute(
        "UPDATE review_ranking_authority SET retry_not_before=?",
        ("2020-01-01T00:00:00+00:00",),
    )
    connection.commit()

    result = recover_and_rebuild(connection)

    assert result is not None
    authority = get_authority(connection)
    assert authority.state == "ready"
    assert authority.required_mode_mask == 0
    assert authority.attempt_count == 2
    assert authority.failure_code is None
    assert authority.retry_not_before is None
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    _assert_generation_target_modes_unique(connection)


def test_rebuild_failure_persists_only_allowlisted_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _seed_database(tmp_path / "failure.sqlite3")

    def fail_scoring(*args: object, **kwargs: object) -> object:
        raise ValueError("private preference /tmp/secret SQL SELECT description")

    monkeypatch.setattr("careeros.ranking.rebuild.score_groups", fail_scoring)
    with pytest.raises(ValueError, match="private preference"):
        rebuild_once(connection)

    authority = get_authority(connection)
    assert authority.state == "failed"
    assert authority.failure_code == "validation"
    assert "private" not in repr(authority)
    assert (
        connection.execute("SELECT count(*) FROM review_ranking_current").fetchone()[0]
        == 0
    )


def test_database_rebuild_requires_existing_current_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        rebuild_database(path)
    assert not path.exists()


def test_interrupted_after_generation_persistence_is_recovered_on_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _seed_database(tmp_path / "interrupted.sqlite3")
    import careeros.ranking.rebuild as rebuild_module

    original_activate = rebuild_module.activate
    monkeypatch.setattr(rebuild_module, "activate", lambda *args, **kwargs: False)
    assert rebuild_once(connection) is None
    assert not fully_prepared(connection)
    assert (
        connection.execute(
            "SELECT count(*) FROM review_ranking_generations"
        ).fetchone()[0]
        == 2
    )

    connection.execute(
        "UPDATE review_ranking_authority SET heartbeat_at=?",
        ((datetime.now(UTC) - timedelta(seconds=10)).isoformat(),),
    )
    connection.commit()
    monkeypatch.setattr(rebuild_module, "activate", original_activate)
    result = rebuild_once(connection, stale_seconds=0)
    assert result is not None
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    assert (
        connection.execute(
            "SELECT count(*) FROM review_ranking_generations"
        ).fetchone()[0]
        == 2
    )


def test_authority_change_after_heartbeat_cannot_persist_stale_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _seed_database(tmp_path / "authority-race.sqlite3")
    import careeros.ranking.rebuild as rebuild_module

    original_heartbeat = rebuild_module.heartbeat
    heartbeats = 0

    def mutate_after_mode_zero_score(*args: object, **kwargs: object) -> bool:
        nonlocal heartbeats
        heartbeats += 1
        result = original_heartbeat(*args, **kwargs)
        if heartbeats == 2:
            update_learned_authority(connection, "a" * 64)
            connection.commit()
        return result

    monkeypatch.setattr(rebuild_module, "heartbeat", mutate_after_mode_zero_score)
    with pytest.raises(ValueError, match="claim|authority"):
        rebuild_once(connection)
    assert (
        connection.execute(
            "SELECT count(*) FROM review_ranking_generations"
        ).fetchone()[0]
        == 0
    )
    assert not fully_prepared(connection)

    monkeypatch.setattr(rebuild_module, "heartbeat", original_heartbeat)
    assert rebuild_once(connection) is not None
    assert fully_prepared(connection)
    assert validate_current_generations(connection)


def test_runtime_version_reconciliation_rebuilds_and_rejects_old_claim(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "runtime-version.sqlite3")
    assert rebuild_once(connection) is not None
    old = connection.execute(
        "UPDATE review_ranking_authority SET scorer_version='obsolete'"
    )
    assert old.rowcount == 1
    connection.commit()
    assert not fully_prepared(connection)
    assert rebuild_once(connection) is not None
    authority = get_authority(connection)
    assert authority.scorer_version == REVIEW_RANKING_SCORER_VERSION
    assert fully_prepared(connection)
    assert validate_current_generations(connection)


@pytest.mark.parametrize(
    "column",
    ("scorer_version", "normalizer_version", "generation_format_version"),
)
def test_runtime_version_recovery_advances_target_once(
    tmp_path: Path, column: str
) -> None:
    connection = _seed_database(tmp_path / f"runtime-{column}.sqlite3")
    assert rebuild_once(connection) is not None
    before = get_authority(connection).target_revision
    connection.execute(
        "UPDATE review_ranking_authority SET required_mode_mask=3, "
        "state='rebuild_required'"
    )
    connection.commit()
    obsolete_claim = claim(connection)
    assert obsolete_claim is not None
    assert column in {
        "scorer_version",
        "normalizer_version",
        "generation_format_version",
    }
    connection.execute(f"UPDATE review_ranking_authority SET {column}=?", ("obsolete",))
    connection.commit()
    result = recover_and_rebuild(connection)
    assert not heartbeat(connection, obsolete_claim)
    assert result is not None
    authority = get_authority(connection)
    assert authority.target_revision == before + 1
    assert authority.state == "ready"
    assert fully_prepared(connection)
    assert validate_current_generations(connection)


def test_admin_recovery_repairs_corrupt_ready_item(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _seed_database(tmp_path / "admin-corrupt.sqlite3")
    assert rebuild_once(connection) is not None
    connection.execute("UPDATE review_ranking_items SET raw_score=raw_score+1")
    connection.commit()
    assert not validate_current_generations(connection)
    connection.close()
    response = TestClient(create_app(database=tmp_path / "admin-corrupt.sqlite3")).get(
        "/today"
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert (
        main(
            [
                "database",
                "rebuild-ranking",
                "--database",
                str(tmp_path / "admin-corrupt.sqlite3"),
            ]
        )
        == 0
    )
    assert "already ready" not in capsys.readouterr().out.lower()
    with connect(tmp_path / "admin-corrupt.sqlite3") as repaired:
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)
    assert (
        TestClient(create_app(database=tmp_path / "admin-corrupt.sqlite3"))
        .get("/today")
        .status_code
        == 200
    )


def test_missing_authority_is_privacy_safe_and_reconstructible(tmp_path: Path) -> None:
    path = tmp_path / "missing-authority.sqlite3"
    connection = _seed_database(path)
    review_event_id = connection.execute(
        "INSERT INTO review_events(job_id,old_decision,new_decision,note,created_at) "
        "VALUES (1,NULL,'shortlist','private review note','n')"
    ).lastrowid
    assert review_event_id is not None
    connection.execute(
        "INSERT INTO review_decisions(job_id,decision,created_at,updated_at) "
        "VALUES (1,'shortlist','n','n')"
    )
    connection.execute(
        "INSERT INTO preference_evidence(job_id,review_event_id,dimension,value,"
        "normalized_value,signal,observed_at) "
        "VALUES (1,?,'company','Acme','acme',1,'n')",
        (review_event_id,),
    )
    application_id = connection.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) "
        "VALUES (2,'applied','n','n')"
    ).lastrowid
    assert application_id is not None
    connection.execute(
        "INSERT INTO application_events(application_id,old_status,new_status,note,"
        "created_at) VALUES (?,NULL,'applied','private application note','n')",
        (application_id,),
    )
    connection.commit()
    assert rebuild_once(connection) is not None
    authoritative_tables = (
        "jobs",
        "preference_state",
        "explicit_preferences",
        "preference_evidence",
        "learned_preference_suppressions",
        "review_decisions",
        "review_events",
        "applications",
        "application_events",
    )
    before = {
        table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
        for table in authoritative_tables
    }
    connection.execute("DELETE FROM review_ranking_authority")
    connection.commit()
    connection.close()
    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}
    assert rebuild_database(path) is not None
    with connect(path) as repaired:
        after = {
            table: tuple(
                tuple(row) for row in repaired.execute(f"SELECT * FROM {table}")
            )
            for table in authoritative_tables
        }
        assert after == before
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)


def test_malformed_authority_numeric_state_is_reconstructible(tmp_path: Path) -> None:
    path = tmp_path / "malformed-authority.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE review_ranking_authority SET target_revision=1.5")
    connection.commit()
    connection.close()
    assert rebuild_database(path) is not None
    with connect(path) as repaired:
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)


def test_negative_authority_revision_is_reconstructible(tmp_path: Path) -> None:
    path = tmp_path / "negative-authority.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE review_ranking_authority SET target_revision=-2")
    connection.commit()
    connection.close()

    assert rebuild_database(path) is not None
    with connect(path) as repaired:
        assert get_authority(repaired).target_revision >= 0
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)


@pytest.mark.parametrize("corrupt_value", [2.5, "not-an-integer"])
def test_admin_recovers_malformed_preference_ranking_revision_without_user_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    corrupt_value: object,
) -> None:
    path = tmp_path / f"malformed-preference-{type(corrupt_value).__name__}.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    before = _product_one_authority_snapshot(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE preference_state SET explicit_ranking_revision=?", (corrupt_value,)
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}

    assert main(["database", "rebuild-ranking", "--database", str(path)]) == 0
    assert "failed" not in capsys.readouterr().err.lower()
    with connect(path) as repaired:
        _assert_preference_ranking_metadata_coherent(repaired)
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)
        assert _product_one_authority_snapshot(repaired) == before
    assert TestClient(create_app(database=path)).get("/today").status_code == 200


def test_admin_reconstructs_malformed_preference_revision_and_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed-preference-and-authority.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    before = _product_one_authority_snapshot(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE preference_state SET explicit_ranking_revision='not-an-integer'"
    )
    connection.execute("DELETE FROM review_ranking_authority")
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}
    assert main(["database", "rebuild-ranking", "--database", str(path)]) == 0

    with connect(path) as repaired:
        _assert_preference_ranking_metadata_coherent(repaired)
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)
        assert _product_one_authority_snapshot(repaired) == before
    assert TestClient(create_app(database=path)).get("/today").status_code == 200


def test_admin_reconstructs_negative_preference_revision_and_missing_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "negative-preference-and-missing-authority.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    before = _product_one_authority_snapshot(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE preference_state SET explicit_ranking_revision=-1")
    connection.execute("DELETE FROM review_ranking_authority")
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}
    assert rebuild_database(path) is not None

    with connect(path) as repaired:
        _assert_preference_ranking_metadata_coherent(repaired)
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)
        assert _product_one_authority_snapshot(repaired) == before
    assert TestClient(create_app(database=path)).get("/today").status_code == 200


def test_reconciliation_retries_after_concurrent_explicit_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "reconciliation-race.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    writer = connect(path)
    replacement = ExplicitPreferenceProfile(
        desired_titles=("Rust Engineer",),
        skills=("Rust",),
    )
    replacement_fingerprint = explicit_ranking_authority_fingerprint(replacement)

    import careeros.ranking.rebuild as rebuild_module

    original_reconcile = rebuild_module.reconcile_authority
    interleaved = False

    def reconcile_after_real_writer(*args: object, **kwargs: object) -> object:
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            replace_explicit_profile(writer, replacement)
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(
        rebuild_module,
        "reconcile_authority",
        reconcile_after_real_writer,
    )

    result = rebuild_once(connection)

    assert interleaved
    assert result is not None
    authority = get_authority(connection)
    state = connection.execute(
        "SELECT explicit_ranking_revision, explicit_ranking_fingerprint "
        "FROM preference_state WHERE singleton=1"
    ).fetchone()
    assert state is not None
    identities_agree = (
        authority.explicit_fingerprint == replacement_fingerprint
        and str(state["explicit_ranking_fingerprint"]) == replacement_fingerprint
        and int(state["explicit_ranking_revision"]) == authority.explicit_revision
    )
    assert not fully_prepared(connection) or identities_agree
    assert identities_agree
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    connection.close()
    writer.close()
    assert TestClient(create_app(database=path)).get("/today").status_code == 200


@pytest.mark.parametrize("mutation", ["learned", "opportunity"])
def test_reconciliation_freshness_guard_covers_every_authority_identity(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / f"reconciliation-{mutation}-freshness.sqlite3"
    connection = _seed_database(path)
    assert rebuild_once(connection) is not None
    opportunities = snapshot_opportunities(connection)
    explicit = resolve_explicit_profile(connection).explicit_profile
    expected_authority = get_authority(connection)
    writer = connect(path)

    if mutation == "learned":
        assert update_learned_authority(writer, "a" * 64)
    else:
        assert update_opportunity_authority(writer, "b" * 64, 17)
    writer.commit()

    result = reconcile_authority(
        connection,
        opportunity_fingerprint_value=opportunity_corpus_fingerprint(opportunities),
        opportunity_count=len(opportunities),
        explicit_fingerprint=explicit_ranking_authority_fingerprint(explicit),
        learned_fingerprint=expected_authority.learned_fingerprint,
        expected_authority=expected_authority,
    )

    assert result is None
    current = get_authority(connection)
    if mutation == "learned":
        assert current.learned_fingerprint == "a" * 64
    else:
        assert current.opportunity_fingerprint == "b" * 64
        assert current.opportunity_count == 17
    assert current.state == "rebuild_required"
    assert not fully_prepared(connection)
    writer.close()
    connection.close()


def test_runtime_reconciliation_rejects_inflight_old_worker(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "runtime-claim.sqlite3")
    assert rebuild_once(connection) is not None
    connection.execute(
        "UPDATE review_ranking_authority SET required_mode_mask=3, "
        "state='rebuild_required'"
    )
    connection.commit()
    old_claim = claim(connection)
    assert old_claim is not None
    old_snapshot = get_authority(connection)
    connection.execute("UPDATE review_ranking_authority SET scorer_version='obsolete'")
    connection.commit()
    opportunities = snapshot_opportunities(connection)
    explicit = resolve_explicit_profile(connection).explicit_profile
    reconcile_authority(
        connection,
        opportunity_fingerprint_value=opportunity_corpus_fingerprint(opportunities),
        opportunity_count=len(opportunities),
        explicit_fingerprint=explicit_ranking_authority_fingerprint(explicit),
        learned_fingerprint=effective_learned_authority_fingerprint(connection),
    )
    assert not heartbeat(connection, old_claim)
    with pytest.raises(ValueError, match="authority|claim"):
        materialize_generation(
            connection,
            learned_mode=0,
            scores=[],
            claim=old_claim,
            authority_snapshot=old_snapshot,
        )


def test_reconciliation_rejects_malformed_preference_revision_without_mutation(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "malformed-preference-revision.sqlite3")
    opportunities = snapshot_opportunities(connection)
    explicit = resolve_explicit_profile(connection).explicit_profile
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE preference_state SET explicit_ranking_revision=?", (2.5,)
    )
    connection.commit()
    before = connection.execute(
        "SELECT explicit_ranking_revision, explicit_ranking_fingerprint "
        "FROM preference_state"
    ).fetchone()
    with pytest.raises(RankingDerivedStateError):
        reconcile_authority(
            connection,
            opportunity_fingerprint_value=opportunity_corpus_fingerprint(opportunities),
            opportunity_count=len(opportunities),
            explicit_fingerprint=explicit_ranking_authority_fingerprint(explicit),
            learned_fingerprint=get_authority(connection).learned_fingerprint,
        )
    assert not connection.in_transaction
    after = connection.execute(
        "SELECT explicit_ranking_revision, explicit_ranking_fingerprint "
        "FROM preference_state"
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_explicit_rows_divergence_reconciles_both_modes(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "explicit-divergence.sqlite3")
    assert rebuild_once(connection) is not None
    connection.execute(
        "UPDATE explicit_preferences SET value='Changed' WHERE dimension='skills'"
    )
    connection.commit()
    assert rebuild_once(connection) is not None
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    profile = resolve_explicit_profile(connection).explicit_profile
    assert get_authority(connection).explicit_fingerprint == (
        explicit_ranking_authority_fingerprint(profile)
    )


def test_learned_only_divergence_preserves_mode_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _seed_database(tmp_path / "learned-divergence.sqlite3")
    assert rebuild_once(connection) is not None
    original = _current_generation_ids(connection)
    monkeypatch.setattr(
        "careeros.ranking.rebuild.effective_learned_authority_fingerprint",
        lambda _connection: "a" * 64,
    )
    assert rebuild_once(connection) is not None
    assert fully_prepared(connection)
    assert validate_current_generations(connection)
    current = _current_generation_ids(connection)
    assert current[MODE_WITHOUT_LEARNED] == original[MODE_WITHOUT_LEARNED]
    assert current[MODE_WITH_LEARNED] != original[MODE_WITH_LEARNED]


def test_activation_cleanup_removes_superseded_unbound_generations(
    tmp_path: Path,
) -> None:
    connection = _seed_database(tmp_path / "activation-cleanup.sqlite3")
    assert rebuild_once(connection) is not None
    assert update_learned_authority(connection, "b" * 64)
    connection.commit()
    assert rebuild_once(connection) is not None
    assert fully_prepared(connection)
    assert (
        connection.execute(
            "SELECT count(*) FROM review_ranking_generations"
        ).fetchone()[0]
        == 2
    )
