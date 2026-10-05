from __future__ import annotations

import sqlite3

import pytest

from careeros.database import review_ranking
from careeros.database.migrations import core
from careeros.database.migrations.core import inspect_schema, migrate
from careeros.database.review_ranking import get_authority
from careeros.database.schema import initialize_schema
from careeros.preferences.models import (
    ExplicitPreferenceProfile,
    explicit_ranking_authority_fingerprint,
)
from careeros.preferences.service import _learned_authority_fingerprint
from careeros.ranking.materialization import (
    opportunity_corpus_fingerprint,
    snapshot_opportunities,
)

_RANKING_TABLES = (
    "review_ranking_current",
    "review_ranking_items",
    "review_ranking_generations",
    "review_ranking_authority",
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection


def _downgrade_to_exact_v13(connection: sqlite3.Connection) -> None:
    for table in _RANKING_TABLES:
        connection.execute(f"DROP TABLE {table}")
    connection.execute(
        "ALTER TABLE preference_state DROP COLUMN explicit_ranking_fingerprint"
    )
    connection.execute(
        "ALTER TABLE preference_state DROP COLUMN explicit_ranking_revision"
    )
    connection.execute(
        "UPDATE schema_metadata SET value='13' WHERE key='schema_version'"
    )
    connection.commit()
    assert {
        str(row[1]) for row in connection.execute("PRAGMA table_info(preference_state)")
    }.isdisjoint({"explicit_ranking_revision", "explicit_ranking_fingerprint"})
    assert inspect_schema(connection) == "upgrade_required"


def _schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


def _row_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    return {
        table: tuple(
            tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')
        )
        for table in tables
    }


def test_real_v13_migration_adds_schema_14_authority() -> None:
    connection = _connection()
    _downgrade_to_exact_v13(connection)

    assert migrate(connection) == "current"
    authority = get_authority(connection)
    assert authority.required_mode_mask == 3
    assert authority.state == "rebuild_required"
    assert {
        str(row[1]) for row in connection.execute("PRAGMA table_info(preference_state)")
    } >= {"explicit_ranking_revision", "explicit_ranking_fingerprint"}


def test_v13_migration_seeds_running_ranking_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    _downgrade_to_exact_v13(connection)
    monkeypatch.setattr(review_ranking, "REVIEW_RANKING_SCORER_VERSION", "test-scorer")
    monkeypatch.setattr(
        review_ranking, "REVIEW_RANKING_NORMALIZER_VERSION", "test-normalizer"
    )
    monkeypatch.setattr(
        review_ranking,
        "REVIEW_RANKING_GENERATION_FORMAT_VERSION",
        "test-generation",
    )
    assert migrate(connection) == "current"
    authority = get_authority(connection)
    assert authority.scorer_version == "test-scorer"
    assert authority.normalizer_version == "test-normalizer"
    assert authority.generation_format_version == "test-generation"


def test_v13_migration_rolls_back_schema_and_rows_after_applied_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    _downgrade_to_exact_v13(connection)
    connection.execute(
        """INSERT INTO jobs(
               source,source_job_id,title,company,collected_at,created_at,updated_at
           ) VALUES ('test','rollback','Engineer','Acme','n','n','n')"""
    )
    connection.commit()
    before_schema = _schema_signature(connection)
    before_rows = _row_snapshot(connection)
    real_apply = core.MIGRATIONS[13]

    def fail_after_apply(candidate: sqlite3.Connection) -> None:
        real_apply(candidate)
        raise RuntimeError("injected schema-14 failure")

    monkeypatch.setitem(core.MIGRATIONS, 13, fail_after_apply)
    with pytest.raises(RuntimeError, match="injected schema-14 failure"):
        migrate(connection)

    assert _schema_signature(connection) == before_schema
    assert _row_snapshot(connection) == before_rows
    assert inspect_schema(connection) == "upgrade_required"


def test_v13_partial_ranking_objects_are_rejected_without_mutation() -> None:
    connection = _connection()
    _downgrade_to_exact_v13(connection)
    connection.execute("CREATE TABLE review_ranking_authority(bad INTEGER)")
    connection.commit()
    before = _schema_signature(connection)

    with pytest.raises(ValueError, match="pre-existing ranking objects"):
        migrate(connection)

    assert _schema_signature(connection) == before
    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "13"
    )


def test_populated_v13_seeds_exact_authorities_without_private_snapshots() -> None:
    connection = _connection()
    _downgrade_to_exact_v13(connection)
    job_ids: list[int] = []
    for suffix in ("a", "b"):
        job_ids.append(
            int(
                connection.execute(
                    """INSERT INTO jobs(
                           source,source_job_id,title,company,description,url,
                           collected_at,created_at,updated_at
                       ) VALUES ('test',?,'Engineer','Acme','private description',?,
                                 'n','n','n')""",
                    (suffix, f"https://example.test/{suffix}"),
                ).lastrowid
            )
        )
    connection.execute(
        "INSERT INTO explicit_preferences VALUES ('desired_titles',0,'Engineer')"
    )
    connection.execute(
        "UPDATE preference_state SET explicit_configured=1, explicit_updated_at='n'"
    )
    for job_id in job_ids:
        event_id = int(
            connection.execute(
                """INSERT INTO review_events(
                       job_id,old_decision,new_decision,note,created_at
                   ) VALUES (?,NULL,'shortlist','private note','n')""",
                (job_id,),
            ).lastrowid
        )
        connection.execute(
            """INSERT INTO review_decisions(job_id,decision,created_at,updated_at)
               VALUES (?,'shortlist','n','n')""",
            (job_id,),
        )
        connection.execute(
            """INSERT INTO preference_evidence(
                   job_id,review_event_id,dimension,value,normalized_value,signal,
                   observed_at
               ) VALUES (?,?,'company','Acme','acme',1,'n')""",
            (job_id, event_id),
        )
    connection.commit()

    assert migrate(connection) == "current"
    authority = get_authority(connection)
    opportunities = snapshot_opportunities(connection)
    assert authority.explicit_fingerprint == explicit_ranking_authority_fingerprint(
        ExplicitPreferenceProfile(desired_titles=("Engineer",))
    )
    assert authority.learned_fingerprint == _learned_authority_fingerprint(connection)
    assert authority.opportunity_fingerprint == opportunity_corpus_fingerprint(
        opportunities
    )
    assert authority.opportunity_count == len(opportunities) == 2

    prohibited = {
        "explicit_profile_json",
        "learned_preferences_json",
        "description",
        "prepared_text",
        "today_response",
    }
    ranking_columns = {
        str(row[1])
        for table in _RANKING_TABLES
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    assert ranking_columns.isdisjoint(prohibited)
    ranking_sql = " ".join(
        str(row[0] or "")
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name LIKE 'review_ranking_%'"
        )
    )
    assert "private description" not in ranking_sql
    assert "private note" not in ranking_sql
