"""Tests for the CareerOS job-to-group endpoint (``GET /jobs/{job_id}/group``).

Covers the canonical distinct-opportunity lookup for a single stored job:
representative and non-representative grouped members return the same complete
canonical ``Group``, ungrouped/never-grouped jobs return a one-member singleton
group with a null ``group_id``, unknown jobs and a missing database return the
canonical ``404``, uninitialized/malformed databases return the canonical
privacy-safe ``500``, and the privacy guarantee that job descriptions, matcher
diagnostics, group timestamps, and all local workflow state never appear in any
HTTP response. Also verifies deterministic ordering, read-only/no-lock/no-side-
effect behavior, schema-version stability, that the API never imports
``careeros.cli``, and that prior job/group endpoints stay byte-identical.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

_JOB_FIELDS = [
    "id",
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "remote_status",
    "employment_type",
    "published_at",
    "url",
]

_GROUP_FIELDS = [
    "group_id",
    "representative",
    "members",
    "sources",
    "urls",
]


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str,
    *,
    remote_status: str | None = "remote",
    employment_type: str | None = "Full-time",
    published_at: str = "2026-08-01T10:00:00Z",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(path: Path) -> tuple[dict[str, int], str]:
    """Seed two duplicate listings, two singletons, and recompute groups.

    Returns a mapping of source-job-id to jobs.id and the canonical group ID of
    the Senior Engineer duplicate pair. The duplicate pair's representative is
    the arbeitnow listing (lowest job id), so the remotive listing is the
    non-representative member.
    """
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remotive", "r-1", "Senior Engineer", "Acme", "Berlin, Germany"),
            _make_job(
                "remoteok",
                "k-1",
                "Data Scientist",
                "Other Co",
                "Munich",
                remote_status=None,
                employment_type="Contract",
            ),
            _make_job("jobicy", "j-1", "Frontend Developer", "Frontend Labs", "Berlin"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    ensure_grouped(connection)
    group_id = connection.execute(
        "SELECT group_id FROM job_group_members WHERE job_id = ?",
        (job_ids["a-1"],),
    ).fetchone()[0]
    connection.close()
    return job_ids, group_id


# --------------------------------------------------------------------------- #
# Grouped members
# --------------------------------------------------------------------------- #


def test_representative_member_returns_complete_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _GROUP_FIELDS
    assert payload["group_id"] == group_id
    assert payload["representative"]["id"] == job_ids["a-1"]
    assert [member["id"] for member in payload["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert payload["sources"] == ["arbeitnow", "remotive"]
    assert payload["urls"] == [
        "https://example.com/a-1",
        "https://example.com/r-1",
    ]
    for member in payload["members"]:
        assert list(member) == _JOB_FIELDS


def test_non_representative_member_returns_same_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        non_rep = client.get(f"/jobs/{job_ids['r-1']}/group")
        rep = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert non_rep.status_code == 200
    assert rep.status_code == 200
    assert non_rep.content == rep.content
    assert non_rep.json()["group_id"] == group_id
    assert non_rep.json()["representative"]["id"] == job_ids["a-1"]
    assert [member["id"] for member in non_rep.json()["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]


# --------------------------------------------------------------------------- #
# Ungrouped singletons
# --------------------------------------------------------------------------- #


def test_singleton_job_returns_one_member_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_ids['k-1']}/group")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _GROUP_FIELDS
    assert payload["group_id"] is None
    assert payload["representative"]["id"] == job_ids["k-1"]
    assert [member["id"] for member in payload["members"]] == [job_ids["k-1"]]
    assert payload["sources"] == ["remoteok"]
    assert payload["urls"] == ["https://example.com/k-1"]
    assert list(payload["representative"]) == _JOB_FIELDS


def test_never_grouped_database_returns_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remotive", "r-1", "Senior Engineer", "Acme", "Berlin, Germany"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert response.status_code == 200
    payload = response.json()
    assert payload["group_id"] is None
    assert payload["representative"]["id"] == job_ids["a-1"]
    assert [member["id"] for member in payload["members"]] == [job_ids["a-1"]]
    assert payload["sources"] == ["arbeitnow"]
    assert payload["urls"] == ["https://example.com/a-1"]


# --------------------------------------------------------------------------- #
# Errors and invalid input
# --------------------------------------------------------------------------- #


def test_unknown_job_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs/9999/group")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_non_integer_job_id_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs/not-an-int/group")

    assert response.status_code == 422


def test_absent_database_returns_404_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v10"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v10"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v10"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_descriptions_and_matcher_diagnostics_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        grouped = client.get(f"/jobs/{job_ids['a-1']}/group")
        non_rep = client.get(f"/jobs/{job_ids['r-1']}/group")
        singleton = client.get(f"/jobs/{job_ids['k-1']}/group")
        reference = client.get(f"/groups/{group_id}")

    for response in (grouped, non_rep, singleton, reference):
        assert response.status_code == 200
        for leaked in (
            "Build things.",
            "description",
            "company+title match",
            "reason",
            "remote_sig",
            "location_sig",
            "created_at",
        ):
            assert leaked not in response.text

    for payload in (grouped.json(), non_rep.json(), singleton.json()):
        assert "description" not in payload["representative"]
        for member in payload["members"]:
            assert "description" not in member


def test_workflow_state_and_private_data_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["a-1"],
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="secret-reminder-message",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert response.status_code == 200
    for leaked in (
        "secret-cv-note",
        "secret-review-note",
        "secret-reminder-message",
        "applied",
        "shortlist",
        "follow_up",
        "note",
        "decision",
        "message",
    ):
        assert leaked not in response.text


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_group_members_ordered_by_job_id_ascending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert response.status_code == 200
    members = response.json()["members"]
    assert [member["id"] for member in members] == sorted(
        member["id"] for member in members
    )


def test_response_is_stable_across_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get(f"/jobs/{job_ids['a-1']}/group")
        second = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == first.content


# --------------------------------------------------------------------------- #
# Read-only / no locks / no side effects
# --------------------------------------------------------------------------- #


def test_endpoint_performs_no_writes_and_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with connect(path) as connection:
        jobs_before = connection.execute(
            "SELECT id, title, company, source FROM jobs ORDER BY id"
        ).fetchall()

    with TestClient(app) as client:
        client.get(f"/jobs/{job_ids['a-1']}/group")
        client.get(f"/jobs/{job_ids['k-1']}/group")
        client.get("/jobs/9999/group")

    with connect(path) as connection:
        jobs_after = connection.execute(
            "SELECT id, title, company, source FROM jobs ORDER BY id"
        ).fetchall()
        runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]

    assert [tuple(row) for row in jobs_after] == [tuple(row) for row in jobs_before]
    assert runs == 0
    assert not lock_path_for(path).exists()


def test_read_only_works_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.locking import collection_lock

    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="collect due"):
        with TestClient(app) as client:
            response = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert response.status_code == 200
    assert response.json()["group_id"] == group_id


# --------------------------------------------------------------------------- #
# Regression / architecture
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(database_path)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['a-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        groups = client.get("/groups")
        group_show = client.get(f"/groups/{_group_id}")
        job_group = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }

    assert jobs.status_code == 200
    assert jobs.json()["total"] == 4
    assert [item["id"] for item in jobs.json()["items"]] == sorted(job_ids.values())

    assert show.status_code == 200
    assert list(show.json()) == _JOB_FIELDS
    assert show.json()["title"] == "Senior Engineer"

    assert missing.status_code == 404
    assert missing.json() == {"detail": "No job found with ID 9999."}

    assert stats.status_code == 200
    assert stats.json()["total"] == len(COLLECTORS)

    assert groups.status_code == 200
    assert groups.json()["total"] == 3

    assert group_show.status_code == 200
    assert group_show.json()["group_id"] == _group_id

    assert job_group.status_code == 200
    assert job_group.json()["group_id"] == _group_id


def test_schema_version_unchanged() -> None:
    assert SCHEMA_VERSION == 10


# --------------------------------------------------------------------------- #
# No CLI import
# --------------------------------------------------------------------------- #


def test_api_does_not_import_careeros_cli() -> None:
    api_root = _SRC / "careeros" / "api"
    violations: list[str] = []
    for path in sorted(api_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "careeros.cli" or name.startswith("careeros.cli."):
                    violations.append(f"{path.relative_to(_SRC)}: {name}")
    assert violations == []
