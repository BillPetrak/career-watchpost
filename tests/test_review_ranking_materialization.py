import sqlite3

import pytest

from careeros.database.review_ranking import (
    EMPTY_LEARNED_FINGERPRINT,
    activate,
    claim,
    fully_prepared,
    get_authority,
    update_learned_authority,
    update_opportunity_authority,
    validate_current_generations,
    validate_generation,
)
from careeros.database.schema import initialize_schema
from careeros.ranking.materialization import (
    MaterializedScore,
    materialize_generation,
    opportunity_corpus_fingerprint,
    snapshot_opportunities,
)


def _materialize(connection, assertion, *, learned_mode, scores):
    return materialize_generation(
        connection,
        learned_mode=learned_mode,
        scores=scores,
        claim=assertion,
        authority_snapshot=get_authority(connection),
    )


def test_empty_two_mode_generation_uses_one_based_contract() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assertion = claim(connection)
    assert assertion is not None
    assert snapshot_opportunities(connection) == ()
    without = _materialize(connection, assertion, learned_mode=0, scores=[])
    with_learned = _materialize(connection, assertion, learned_mode=1, scores=[])
    assert activate(connection, assertion, None, {0: without.id, 1: with_learned.id})
    assert fully_prepared(connection)


def test_mode0_disabled_identity() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "UPDATE review_ranking_authority SET learned_revision=7, learned_fingerprint=?",
        ("1" * 64,),
    )
    connection.commit()
    assertion = claim(connection)
    assert assertion
    authority = get_authority(connection)
    generation = _materialize(connection, assertion, learned_mode=0, scores=[])
    assert generation.learned_revision == 0
    assert generation.learned_fingerprint == EMPTY_LEARNED_FINGERPRINT
    assert authority.learned_revision == 7
    assert authority.learned_fingerprint == "1" * 64


def test_mode1_current_identity() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "UPDATE review_ranking_authority SET learned_fingerprint=?, learned_revision=3",
        ("1" * 64,),
    )
    connection.commit()
    assertion = claim(connection)
    assert assertion
    generation = _materialize(connection, assertion, learned_mode=1, scores=[])
    assert generation.learned_fingerprint == "1" * 64
    assert generation.learned_revision == 3


def test_atomic_two_mode_rejection() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assert claim(connection, mode_mask=1) is None


def test_learned_only_replacement_reuses_mode0() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    first = claim(connection)
    assert first
    first_authority = get_authority(connection)
    zero = materialize_generation(
        connection,
        learned_mode=0,
        scores=[],
        claim=first,
        authority_snapshot=first_authority,
    )
    one = materialize_generation(
        connection,
        learned_mode=1,
        scores=[],
        claim=first,
        authority_snapshot=first_authority,
    )
    assert activate(connection, first, None, {0: zero.id, 1: one.id})
    original_target = get_authority(connection).target_revision
    update_learned_authority(connection, "1" * 64)
    connection.commit()
    assert get_authority(connection).target_revision == original_target + 1
    assert not fully_prepared(connection)
    second = claim(connection)
    assert second
    replacement = _materialize(connection, second, learned_mode=1, scores=[])
    assert activate(connection, second, None, {1: replacement.id})
    rows = connection.execute(
        """SELECT learned_mode,generation_id,bound_target_revision
           FROM review_ranking_current ORDER BY learned_mode"""
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        (0, zero.id),
        (1, replacement.id),
    ]
    assert replacement.id != one.id
    assert {int(row[2]) for row in rows} == {original_target + 1}
    assert fully_prepared(connection)
    assert validate_current_generations(connection)


def test_one_based_nonempty_ordinals_and_exact_canonical_order() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    for title, published in (
        ("Older", "2020-01-01T00:00:00+00:00"),
        ("Newer", "2025-01-01T00:00:00+00:00"),
    ):
        connection.execute(
            """INSERT INTO jobs(
                source,source_job_id,title,company,published_at,
                collected_at,created_at,updated_at
            ) VALUES ('x',?,?,?,?,'n','n','n')""",
            (title, title, "C", published),
        )
    opportunities = snapshot_opportunities(connection)
    update_opportunity_authority(
        connection, opportunity_corpus_fingerprint(opportunities), len(opportunities)
    )
    connection.commit()
    assertion = claim(connection)
    assert assertion
    scores = [MaterializedScore(item, 1, 50) for item in opportunities]
    generation = _materialize(connection, assertion, learned_mode=0, scores=scores)
    rows = connection.execute(
        """SELECT rank_ordinal, representative_job_id, opportunity_key
           FROM review_ranking_items WHERE generation_id=? ORDER BY rank_ordinal""",
        (generation.id,),
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert [row[2] for row in rows] == ["job:2", "job:1"]
    assert generation.item_count == generation.opportunity_count == 2


def test_short_group_id_uses_disjoint_namespaced_opportunity_key() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    for source_job_id in ("1", "2", "3"):
        connection.execute(
            """INSERT INTO jobs(
                source,source_job_id,title,company,collected_at,created_at,updated_at
            ) VALUES ('x',?,'T','C','n','n','n')""",
            (source_job_id,),
        )
    connection.execute("INSERT INTO job_groups(id) VALUES ('g1')")
    connection.executemany(
        """INSERT INTO job_group_members(
            job_id,group_id,reason,remote_sig,location_sig
        ) VALUES (?,'g1','test',NULL,'')""",
        ((1,), (2,)),
    )
    opportunities = snapshot_opportunities(connection)
    assert [item.key for item in opportunities] == ["group:g1", "job:3"]
    update_opportunity_authority(
        connection, opportunity_corpus_fingerprint(opportunities), len(opportunities)
    )
    connection.commit()
    assertion = claim(connection)
    assert assertion
    generation = _materialize(
        connection,
        assertion,
        learned_mode=0,
        scores=[MaterializedScore(item, 0, 0) for item in opportunities],
    )
    rows = connection.execute(
        """SELECT opportunity_key FROM review_ranking_items
           WHERE generation_id=? ORDER BY opportunity_key""",
        (generation.id,),
    ).fetchall()
    assert [str(row[0]) for row in rows] == ["group:g1", "job:3"]


def _valid_nonempty_generation() -> tuple[sqlite3.Connection, int]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        """INSERT INTO jobs(
            source,source_job_id,title,company,collected_at,created_at,updated_at
        ) VALUES ('x','1','T','C','n','n','n')"""
    )
    opportunities = snapshot_opportunities(connection)
    update_opportunity_authority(
        connection, opportunity_corpus_fingerprint(opportunities), 1
    )
    connection.commit()
    assertion = claim(connection)
    assert assertion
    generation = _materialize(
        connection,
        assertion,
        learned_mode=0,
        scores=[MaterializedScore(opportunities[0], 1, 50)],
    )
    return connection, generation.id


def test_full_item_fingerprint_corruption_rejected() -> None:
    connection, generation_id = _valid_nonempty_generation()
    connection.execute("UPDATE review_ranking_items SET raw_score=9")
    connection.commit()
    assert not validate_generation(connection, generation_id)


def test_header_content_corruption_rejected() -> None:
    connection, generation_id = _valid_nonempty_generation()
    connection.execute(
        "UPDATE review_ranking_generations SET content_fingerprint=?", ("f" * 64,)
    )
    connection.commit()
    assert not validate_generation(connection, generation_id)


def test_obsolete_persistence_rejected(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    original = snapshot_opportunities
    calls = 0

    def changing_snapshot(conn):
        nonlocal calls
        result = original(conn)
        calls += 1
        if calls == 1:
            conn.execute(
                "UPDATE review_ranking_authority SET target_revision=target_revision+1"
            )
            conn.commit()
        return result

    monkeypatch.setattr(
        "careeros.ranking.materialization.snapshot_opportunities", changing_snapshot
    )
    assertion = claim(connection)
    assert assertion
    with pytest.raises(ValueError):
        materialize_generation(
            connection,
            learned_mode=0,
            scores=[],
            claim=assertion,
            authority_snapshot=get_authority(connection),
        )
    assert (
        connection.execute(
            "SELECT count(*) FROM review_ranking_generations"
        ).fetchone()[0]
        == 0
    )


def test_item_count_and_ordinal_corruption_rejected() -> None:
    connection, generation_id = _valid_nonempty_generation()
    connection.execute("UPDATE review_ranking_generations SET item_count=2")
    connection.commit()
    assert not validate_generation(connection, generation_id)


@pytest.mark.parametrize(
    "statement,parameters",
    (
        ("UPDATE review_ranking_items SET public_match_score=49", ()),
        (
            "UPDATE review_ranking_items "
            "SET group_id='corrupt', opportunity_key='group:corrupt'",
            (),
        ),
        ("UPDATE review_ranking_items SET opportunity_fingerprint=?", ("f" * 64,)),
        ("UPDATE review_ranking_generations SET items_fingerprint=?", ("e" * 64,)),
        ("UPDATE review_ranking_generations SET scorer_version='corrupt'", ()),
        ("UPDATE review_ranking_generations SET normalizer_version='corrupt'", ()),
        (
            "UPDATE review_ranking_generations SET generation_format_version='corrupt'",
            (),
        ),
    ),
)
def test_complete_generation_corruption_is_rejected(
    statement: str, parameters: tuple[object, ...]
) -> None:
    connection, generation_id = _valid_nonempty_generation()
    connection.execute(statement, parameters)
    connection.commit()
    assert not validate_generation(connection, generation_id)
