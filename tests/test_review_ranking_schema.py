import sqlite3
from typing import cast

import pytest

from careeros.database.migrations.core import inspect_schema
from careeros.database.preferences import get_preference_state
from careeros.database.review_ranking import (
    EMPTY_LEARNED_FINGERPRINT,
    RankingDerivedStateError,
    decode_ranking_integer,
    fully_prepared,
    get_authority,
    get_generation,
    list_generation_items,
    reset_authority,
)
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.preferences.models import (
    ExplicitPreferenceProfile,
    explicit_ranking_authority_fingerprint,
)
from careeros.ranking.rebuild import rebuild_once


def test_schema_14_seeds_ranking_authority_without_private_values() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assert SCHEMA_VERSION == 14
    authority = get_authority(connection)
    assert authority.required_mode_mask == 3
    assert authority.state == "rebuild_required"
    assert authority.learned_fingerprint == EMPTY_LEARNED_FINGERPRINT
    expected_explicit = explicit_ranking_authority_fingerprint(
        ExplicitPreferenceProfile()
    )
    assert authority.explicit_fingerprint == expected_explicit
    assert get_preference_state(connection).explicit_ranking_fingerprint == (
        expected_explicit
    )
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name='review_ranking_items'"
    ).fetchone()


def test_structural_rejection_missing_ranking_index() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute("DROP INDEX idx_review_ranking_items_order")
    connection.commit()
    assert inspect_schema(connection) == "malformed"


def test_ranking_numeric_metadata_requires_integer_storage() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE review_ranking_authority SET target_revision='not-an-integer'"
        )


def test_preference_ranking_revision_requires_integer_storage() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE preference_state SET explicit_ranking_revision='not-an-integer'"
        )


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("target_revision", -1),
        ("explicit_revision", -1),
        ("opportunity_revision", -1),
        ("opportunity_count", -1),
        ("learned_revision", -1),
        ("required_mode_mask", 4),
        ("claim_target_revision", -1),
        ("claim_mode_mask", 4),
        ("attempt_count", -1),
    ),
)
def test_authority_decoder_rejects_schema_domain_violations(
    column: str, value: int
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        f"UPDATE review_ranking_authority SET {column}=? WHERE singleton=1",
        (value,),
    )
    connection.commit()

    with pytest.raises(RankingDerivedStateError):
        get_authority(connection)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("target_revision", -1),
        ("learned_mode", 2),
        ("explicit_revision", -1),
        ("opportunity_revision", -1),
        ("opportunity_count", -1),
        ("learned_revision", -1),
        ("item_count", -1),
    ),
)
def test_generation_decoder_rejects_schema_domain_violations(
    column: str, value: int
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assert rebuild_once(connection) is not None
    generation_id = int(
        connection.execute("SELECT min(id) FROM review_ranking_generations").fetchone()[
            0
        ]
    )
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        f"UPDATE review_ranking_generations SET {column}=? WHERE id=?",
        (value, generation_id),
    )
    connection.commit()

    with pytest.raises(RankingDerivedStateError):
        get_generation(connection, generation_id)


@pytest.mark.parametrize(
    ("column", "value"),
    (("rank_ordinal", 0), ("public_match_score", 101)),
)
def test_item_decoder_rejects_schema_domain_violations(column: str, value: int) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,collected_at,"
        "created_at,updated_at) VALUES ('x','1','T','C','n','n','n')"
    )
    connection.commit()
    assert rebuild_once(connection) is not None
    generation_id = int(
        connection.execute("SELECT min(id) FROM review_ranking_generations").fetchone()[
            0
        ]
    )
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        f"UPDATE review_ranking_items SET {column}=? WHERE generation_id=?",
        (value, generation_id),
    )
    connection.commit()

    with pytest.raises(RankingDerivedStateError):
        list_generation_items(connection, generation_id)


@pytest.mark.parametrize(
    ("column", "value", "where"),
    (
        ("learned_mode", 2, "learned_mode=0"),
        ("bound_target_revision", -1, "learned_mode=0"),
    ),
)
def test_current_decoder_rejects_schema_domain_violations(
    column: str, value: int, where: str
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assert rebuild_once(connection) is not None
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        f"UPDATE review_ranking_current SET {column}=? WHERE {where}",
        (value,),
    )
    connection.commit()

    with pytest.raises(RankingDerivedStateError):
        fully_prepared(connection)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    (
        ("review_ranking_items", "generation_id", 1.5),
        ("review_ranking_items", "generation_id", "not-an-integer"),
        ("review_ranking_items", "representative_job_id", 1.5),
        ("review_ranking_items", "representative_job_id", "not-an-integer"),
        ("review_ranking_current", "generation_id", 1.5),
        ("review_ranking_current", "generation_id", "not-an-integer"),
    ),
)
def test_ranking_id_columns_reject_non_integer_storage(
    table: str, column: str, value: object
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,collected_at,"
        "created_at,updated_at) VALUES ('x','1','T','C','n','n','n')"
    )
    connection.execute(
        """INSERT INTO review_ranking_generations(
            content_fingerprint,target_revision,learned_mode,explicit_revision,
            explicit_fingerprint,opportunity_revision,opportunity_fingerprint,
            opportunity_count,learned_revision,learned_fingerprint,scorer_version,
            normalizer_version,generation_format_version,item_count,items_fingerprint,completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "a" * 64,
            0,
            0,
            0,
            "b" * 64,
            0,
            "c" * 64,
            1,
            0,
            "d" * 64,
            "v1",
            "v1",
            "v1",
            1,
            "e" * 64,
            "n",
        ),
    )
    generation_id = int(
        connection.execute("SELECT id FROM review_ranking_generations").fetchone()[0]
    )
    connection.execute(
        "INSERT INTO review_ranking_items VALUES (?,?,?,?,?,?,?,?)",
        (generation_id, "job:1", None, 1, "f" * 64, 1, 0, 0),
    )
    connection.execute(
        "INSERT INTO review_ranking_current VALUES (?,?,?,?)",
        (0, generation_id, 0, "n"),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"UPDATE {table} SET {column}=?", (value,))


def test_ranking_decoders_fail_closed_for_deliberate_numeric_corruption() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        """INSERT INTO jobs(
            source,source_job_id,title,company,collected_at,created_at,updated_at
        ) VALUES ('x','1','T','C','n','n','n')"""
    )
    connection.execute(
        """INSERT INTO review_ranking_generations(
            content_fingerprint,target_revision,learned_mode,explicit_revision,
            explicit_fingerprint,opportunity_revision,opportunity_fingerprint,
            opportunity_count,learned_revision,learned_fingerprint,scorer_version,
            normalizer_version,generation_format_version,item_count,items_fingerprint,
            completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "a" * 64,
            0,
            0,
            0,
            "b" * 64,
            0,
            "c" * 64,
            1,
            0,
            "d" * 64,
            "v1",
            "v1",
            "v1",
            0,
            "e" * 64,
            "n",
        ),
    )
    generation_id = int(
        connection.execute("SELECT id FROM review_ranking_generations").fetchone()[0]
    )
    connection.execute(
        """INSERT INTO review_ranking_items(
            generation_id,opportunity_key,group_id,representative_job_id,
            opportunity_fingerprint,rank_ordinal,raw_score,public_match_score
        ) VALUES (?,?,?,?,?,?,?,?)""",
        (generation_id, "job:1", None, 1, "f" * 64, 1, 0, 0),
    )
    connection.execute(
        "INSERT INTO review_ranking_current VALUES (?,?,?,?)",
        (0, generation_id, 0, "n"),
    )
    connection.commit()
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("UPDATE review_ranking_items SET representative_job_id='1.9'")
    connection.execute(
        "UPDATE review_ranking_items SET generation_id='1.9' WHERE generation_id=?",
        (generation_id,),
    )
    connection.execute(
        "UPDATE review_ranking_current SET generation_id='1.9' WHERE learned_mode=0"
    )
    connection.commit()
    with pytest.raises(RankingDerivedStateError):
        list_generation_items(connection, cast(int, "1.9"))
    current = connection.execute(
        "SELECT generation_id FROM review_ranking_current WHERE learned_mode=0"
    ).fetchone()
    assert current is not None
    with pytest.raises(RankingDerivedStateError):
        decode_ranking_integer(current[0], "generation_id")


def test_reset_authority_rebuilds_only_disposable_ranking_state() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,collected_at,"
        "created_at,updated_at) VALUES ('x','1','T','C','n','n','n')"
    )
    connection.execute("DELETE FROM review_ranking_authority")
    connection.commit()
    reset_authority(
        connection,
        explicit_revision=0,
        explicit_fingerprint="a" * 64,
        opportunity_revision=0,
        opportunity_fingerprint_value="b" * 64,
        opportunity_count=1,
        learned_revision=0,
        learned_fingerprint="c" * 64,
    )
    assert get_authority(connection).required_mode_mask == 3
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_structural_rejection_bad_ordinal_check_is_detected_by_schema_contract() -> (
    None
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    original_sql = str(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='review_ranking_items'"
        ).fetchone()[0]
    )
    current_sql = str(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='review_ranking_current'"
        ).fetchone()[0]
    )
    connection.execute("DROP TABLE review_ranking_current")
    connection.execute("DROP TABLE review_ranking_items")
    connection.execute(original_sql.replace("rank_ordinal >= 1", "rank_ordinal >= 0"))
    connection.execute(
        "CREATE INDEX idx_review_ranking_items_order "
        "ON review_ranking_items(generation_id, rank_ordinal)"
    )
    connection.execute(
        "CREATE INDEX idx_review_ranking_items_rep "
        "ON review_ranking_items(representative_job_id)"
    )
    connection.execute(current_sql)
    connection.commit()
    assert inspect_schema(connection) == "malformed"


def test_url_is_part_of_opportunity_authority() -> None:
    from careeros.database.jobs import StoredJob
    from careeros.ranking.materialization import opportunity_digest

    base = StoredJob(
        1, "x", "1", "T", "C", None, "D", "https://a", None, None, None, "c", "c", "c"
    )
    changed = StoredJob(
        1, "x", "1", "T", "C", None, "D", "https://b", None, None, None, "c", "c", "c"
    )
    assert opportunity_digest("job:1", None, 1, (base,)) != opportunity_digest(
        "job:1", None, 1, (changed,)
    )
