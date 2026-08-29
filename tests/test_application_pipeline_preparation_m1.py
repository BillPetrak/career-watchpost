from pathlib import Path

from careeros.api.main import app
from careeros.applications.service import (
    get_application_pipeline,
    set_application_status,
)
from careeros.database.applications import preparation_summary_for_applications
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.preparations.service import (
    add_requirement,
    confirm_requirement,
    mark_reviewed,
    update_requirement,
)
from tests.support import TestClient


def setup_db(path: Path, count: int = 20):
    with connect(path) as c:
        initialize_schema(c)
        for index in range(count):
            c.execute(
                "INSERT INTO jobs(source,title,description,collected_at) "
                "VALUES ('x',?,?,?)",
                (f"Job {index}", "Synthetic description", "2026-01-01T00:00:00+00:00"),
            )
        c.commit()
        for job_id in range(1, count + 1):
            set_application_status(c, job_id=job_id, status="interested")
            add_requirement(
                c, job_id, expected_revision=0, kind="task", label="Private label"
            )
            item = c.execute(
                "SELECT id FROM application_requirement_items WHERE application_id=?",
                (job_id,),
            ).fetchone()[0]
            confirm_requirement(c, job_id, item, expected_revision=1)
            update_requirement(
                c, job_id, item, expected_revision=2, fulfillment_state="satisfied"
            )
            mark_reviewed(c, job_id, 3)


def test_pipeline_compact_summary_has_no_private_graph(tmp_path: Path, monkeypatch):
    path = tmp_path / "pipeline.sqlite3"
    setup_db(path, 20)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    response = TestClient(app).get("/application-pipeline?limit=20")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 20
    for item in body["items"]:
        assert set(item["preparation"]) == {"category", "blocking_count"}
        assert item["preparation"]["category"] == "requirements_satisfied"
        serialized = str(item)
        assert "Private label" not in serialized
        assert "items" not in item["preparation"]


def test_pipeline_pagination_and_application_recency_unchanged(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "pagination.sqlite3"
    setup_db(path, 5)
    before = (
        connect(path)
        .execute("SELECT updated_at FROM applications WHERE job_id=1")
        .fetchone()[0]
    )
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    body = TestClient(app).get("/application-pipeline?limit=2&offset=1").json()
    assert body["total"] == 5 and body["offset"] == 1 and body["limit"] == 2
    after = (
        connect(path)
        .execute("SELECT updated_at FROM applications WHERE job_id=1")
        .fetchone()[0]
    )
    assert after == before


def test_preparation_aggregate_is_one_query_and_indexed(tmp_path: Path):
    path = tmp_path / "trace.sqlite3"
    setup_db(path, 20)
    with connect(path) as connection:
        page = get_application_pipeline(connection, view="all", limit=20)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        preparation_summary_for_applications(
            connection,
            [item.application.id for item in page.items],
            now="2026-01-01T00:00:00+00:00",
        )
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 1
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT p.application_id "
            "FROM application_preparations p "
            "LEFT JOIN application_requirement_items i "
            "ON i.application_id=p.application_id "
            "WHERE p.application_id IN (?,?,?,?,?) GROUP BY p.application_id",
            (1, 2, 3, 4, 5),
        ).fetchall()
        assert any("idx_requirement_items_application" in row[3] for row in plan)


def test_aggregate_query_count_is_constant(tmp_path: Path):
    path = tmp_path / "distribution.sqlite3"
    setup_db(path, 20)
    with connect(path) as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM applications")]
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        preparation_summary_for_applications(
            connection, ids, now="2026-01-01T00:00:00+00:00"
        )
        assert (
            len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 1
        )
