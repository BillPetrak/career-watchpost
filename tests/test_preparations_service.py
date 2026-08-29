from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros.applications.service import set_application_status
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.preparations.service import (
    PreparationError,
    RevisionConflict,
    add_requirement,
    clear_ready,
    clear_review,
    confirm_ready,
    confirm_requirement,
    delete_requirement,
    get_preparation,
    mark_reviewed,
    readiness,
    reorder_requirements,
    update_requirement,
)


def db(tmp_path: Path):
    path = tmp_path / "prep.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,title,collected_at) VALUES "
        "('x','Job','2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    set_application_status(connection, job_id=1, status="interested")
    return connection


def test_logical_revision_zero_and_lazy_creation(tmp_path: Path):
    connection = db(tmp_path)
    assert get_preparation(connection, 1)["revision"] == 0
    assert (
        connection.execute("SELECT count(*) FROM application_preparations").fetchone()[
            0
        ]
        == 0
    )
    add_requirement(connection, 1, expected_revision=0, kind="task", label="Apply")
    assert get_preparation(connection, 1)["revision"] == 1


def test_crud_dense_order_and_cas(tmp_path: Path):
    connection = db(tmp_path)
    add_requirement(connection, 1, expected_revision=0, kind="task", label="A")
    second = add_requirement(connection, 1, expected_revision=1, kind="link", label="B")
    ids = [i["id"] for i in second["items"]]
    reordered = reorder_requirements(connection, 1, ids[::-1], expected_revision=2)
    assert [i["label"] for i in reordered["items"]] == ["B", "A"]
    with pytest.raises(RevisionConflict):
        update_requirement(connection, 1, ids[0], expected_revision=2, label="stale")
    deleted = delete_requirement(connection, 1, ids[0], expected_revision=3)
    assert [i["sort_order"] for i in deleted["items"]] == [0]


def test_bounds_deadline_and_timestamp_rules(tmp_path: Path):
    connection = db(tmp_path)
    with pytest.raises(PreparationError):
        add_requirement(connection, 1, expected_revision=0, kind="task", label=" ")
    with pytest.raises(PreparationError):
        add_requirement(
            connection, 1, expected_revision=0, kind="task", label="x" * 161
        )
    with pytest.raises(PreparationError):
        add_requirement(
            connection,
            1,
            expected_revision=0,
            kind="deadline",
            label="D",
            fulfillment_state="satisfied",
        )
    item = add_requirement(
        connection,
        1,
        expected_revision=0,
        kind="deadline",
        label="D",
        deadline_at="2026-01-02T03:00:00+03:00",
    )
    assert item["items"][0]["deadline_at"] == "2026-01-02T00:00:00+00:00"


def test_review_ready_gate_and_stale_metadata(tmp_path: Path):
    connection = db(tmp_path)
    add_requirement(connection, 1, expected_revision=0, kind="task", label="A")
    with pytest.raises(PreparationError, match="unconfirmed"):
        mark_reviewed(connection, 1, 1)
    item = get_preparation(connection, 1)["items"][0]
    confirm_requirement(connection, 1, item["id"], expected_revision=1)
    update_requirement(
        connection, 1, item["id"], expected_revision=2, fulfillment_state="satisfied"
    )
    mark_reviewed(connection, 1, 3)
    assert readiness(connection, 1)["category"] == "requirements_satisfied"
    confirm_ready(connection, 1, 3)
    assert readiness(connection, 1)["category"] == "user_confirmed_ready"
    update_requirement(connection, 1, item["id"], expected_revision=3, label="Changed")
    assert readiness(connection, 1)["category"] == "unknown"
    assert get_preparation(connection, 1)["ready_confirmed_revision"] == 3
    clear_review(connection, 1)
    assert get_preparation(connection, 1)["requirements_reviewed_revision"] is None
    clear_ready(connection, 1)


def test_readiness_reason_order_and_deadline_boundary(tmp_path: Path):
    connection = db(tmp_path)
    add_requirement(
        connection,
        1,
        expected_revision=0,
        kind="deadline",
        label="D",
        deadline_at="2026-01-01T00:00:00+00:00",
    )
    result = readiness(connection, 1, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert [r["code"] for r in result["reasons"]] == [
        "job_information_unavailable",
        "requirements_not_reviewed",
        "requirement_unconfirmed",
        "required_deadline_passed",
    ]


def test_empty_review_is_allowed_and_idempotent(tmp_path: Path):
    connection = db(tmp_path)
    mark_reviewed(connection, 1, 0)
    before = get_preparation(connection, 1)["requirements_reviewed_at"]
    mark_reviewed(connection, 1, 0)
    assert get_preparation(connection, 1)["requirements_reviewed_at"] == before
