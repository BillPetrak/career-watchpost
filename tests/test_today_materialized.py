from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careeros.api.main import create_app
from careeros.applications.service import set_application_status
from careeros.collecting import refresh_opportunity_authority
from careeros.database.groups import ensure_grouped
from careeros.database.schema import initialize_schema
from careeros.preferences.models import ExplicitPreferenceProfile
from careeros.preferences.service import replace_explicit_profile
from careeros.ranking.rebuild import rebuild_once
from careeros.review.service import set_decision
from careeros.today import service as today_service
from careeros.today.service import build_today
from tests.support import TestClient


def _ready_db(path: Path, *, count: int = 1) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    for item in range(1, count + 1):
        connection.execute(
            """INSERT INTO jobs(
                   source,source_job_id,title,company,description,url,
                   published_at,collected_at
               ) VALUES ('test',?,'Python Engineer',?,'Python',?,?,'n')""",
            (
                str(item),
                f"Company {item}",
                f"https://example.test/{item}",
                f"2026-09-{item:02d}T00:00:00+00:00",
            ),
        )
    ensure_grouped(connection)
    refresh_opportunity_authority(connection)
    connection.commit()
    replace_explicit_profile(
        connection,
        ExplicitPreferenceProfile(desired_titles=("Python Engineer",)),
    )
    rebuild_once(connection)
    connection.close()


def test_today_uses_ready_materialized_generation(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    snapshot = build_today(connection)
    assert snapshot.review_next.candidate_total == 1
    assert snapshot.review_next.total == 1
    assert len(snapshot.review_next.items) == 1
    connection.close()


def test_today_unready_is_privacy_safe_503(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.commit()
    connection.close()
    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}


def test_today_rejects_selected_opportunity_corruption(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE review_ranking_items SET opportunity_fingerprint=?",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()
    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE review_ranking_items SET public_match_score=public_match_score+1 "
        "WHERE rank_ordinal=(SELECT max(rank_ordinal) FROM review_ranking_items)",
        "UPDATE review_ranking_items SET raw_score=raw_score+1 "
        "WHERE rank_ordinal=(SELECT max(rank_ordinal) FROM review_ranking_items)",
    ),
)
def test_today_rejects_complete_persisted_item_corruption(
    tmp_path: Path, statement: str
) -> None:
    """Ready reads fail closed for unselected score and rank/item tampering."""
    path = tmp_path / "today.sqlite3"
    _ready_db(path, count=5)
    connection = sqlite3.connect(path)
    connection.execute(statement)
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_today_rejects_constraint_safe_ordinal_swap(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path, count=5)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE review_ranking_items SET rank_ordinal=rank_ordinal+99 "
        "WHERE rank_ordinal IN (1, 2)"
    )
    connection.execute(
        "UPDATE review_ranking_items SET rank_ordinal=2 WHERE rank_ordinal=100"
    )
    connection.execute(
        "UPDATE review_ranking_items SET rank_ordinal=1 WHERE rank_ordinal=101"
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_today_uses_live_eligibility_and_exact_totals(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path, count=5)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    set_decision(connection, job_id=1, decision="review_later")
    set_application_status(connection, job_id=2, status="interested")

    snapshot = build_today(connection)

    assert snapshot.review_next.candidate_total == 3
    assert snapshot.review_next.total == 3
    assert len(snapshot.review_next.items) == 3
    assert {item.group.representative.id for item in snapshot.review_next.items} == {
        3,
        4,
        5,
    }
    connection.close()


def test_today_never_uses_corpus_snapshot_or_full_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path, count=5)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    real_score_groups = today_service.score_groups
    scored_counts: list[int] = []

    def fail_corpus(*args: object, **kwargs: object) -> object:
        raise AssertionError("request-time corpus path used")

    def bounded_score(groups, *args, **kwargs):
        groups = list(groups)
        scored_counts.append(len(groups))
        return real_score_groups(groups, *args, **kwargs)

    monkeypatch.setattr(
        "careeros.ranking.materialization.snapshot_opportunities", fail_corpus
    )
    monkeypatch.setattr(
        "careeros.preferences.service.derive_learned_preferences", fail_corpus
    )
    monkeypatch.setattr(today_service, "score_groups", bounded_score)

    snapshot = build_today(connection)

    assert snapshot.review_next.candidate_total == 5
    assert snapshot.review_next.total == 5
    assert scored_counts == [3]
    connection.close()


def test_today_rejects_out_of_band_preference_authority_change(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
    connection.execute(
        """UPDATE explicit_preferences SET value='Corrupt authority'
           WHERE dimension='desired_titles'"""
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE review_ranking_items "
        "SET representative_job_id=999, opportunity_key='job:999'",
        "DELETE FROM review_ranking_items WHERE rank_ordinal=1",
    ),
)
def test_today_rejects_selected_identity_or_count_corruption(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(statement)
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")

    assert response.status_code == 503


def test_today_rejects_malformed_numeric_ranking_metadata(tmp_path: Path) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE review_ranking_generations SET item_count='not-an-integer'"
    )
    connection.commit()
    connection.close()
    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}


@pytest.mark.parametrize("corrupt_value", ["not-an-integer", 1.5])
def test_today_rejects_malformed_preference_ranking_revision(
    tmp_path: Path, corrupt_value: object
) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
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


def test_today_does_not_relabel_unrelated_scoring_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "today.sqlite3"
    _ready_db(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row

    def fail_scoring(*args: object, **kwargs: object) -> object:
        raise ValueError("unrelated scorer failure")

    monkeypatch.setattr(today_service, "score_groups", fail_scoring)
    with pytest.raises(ValueError, match="unrelated scorer failure"):
        build_today(connection)
    connection.close()
