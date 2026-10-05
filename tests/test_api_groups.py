"""Tests for the CareerOS job-group endpoints (sixth slice).

Covers ``GET /groups``, ``GET /groups/summary``, and ``GET /groups/{group_id}``:
envelope and item field order, deterministic representative/member ordering,
pre-grouping filters, pagination, database-availability behavior, the summary
values, and the privacy guarantee that job descriptions, matcher diagnostics
(``reason``/``remote_sig``/``location_sig``), group ``created_at``, and all
local workflow state never appear in any HTTP response.
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
    the Senior Engineer duplicate pair.
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
# List: shape and ordering
# --------------------------------------------------------------------------- #


def test_list_returns_distinct_opportunities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["group_id"] for item in payload["items"]] == [
        group_id,
        None,
        None,
    ]
    group = payload["items"][0]
    assert [member["id"] for member in group["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]


def test_list_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["items", "total", "offset", "limit"]
    for item in payload["items"]:
        assert list(item) == _GROUP_FIELDS
        assert list(item["representative"]) == _JOB_FIELDS
        for member in item["members"]:
            assert list(member) == _JOB_FIELDS


def test_list_orders_by_representative_id_ascending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["representative"]["id"] for item in items] == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
    ]
    assert [item["group_id"] for item in items] == [group_id, None, None]


def test_list_members_ordered_by_job_id_ascending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups")

    assert response.status_code == 200
    group = next(
        item for item in response.json()["items"] if item["group_id"] == group_id
    )
    assert [member["id"] for member in group["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert [member["id"] for member in group["members"]] == sorted(
        member["id"] for member in group["members"]
    )


def test_list_singletons_carry_null_group_id_and_ordered_distinct_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups")

    assert response.status_code == 200
    items = response.json()["items"]
    singletons = [item for item in items if item["group_id"] is None]
    assert len(singletons) == 2
    assert all(len(item["members"]) == 1 for item in singletons)
    assert all(
        item["members"][0]["id"] == item["representative"]["id"] for item in singletons
    )


def test_list_without_regroup_lists_every_job_as_singleton(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("jobicy", "j-1", "Frontend Developer", "Frontend Labs", "Berlin"),
        ],
    )
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert all(item["group_id"] is None for item in payload["items"])


# --------------------------------------------------------------------------- #
# List: filters applied before grouping
# --------------------------------------------------------------------------- #


def test_list_source_filter_applies_before_grouping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups", params={"source": "arbeitnow"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["group_id"] == group_id
    assert [member["id"] for member in item["members"]] == [job_ids["a-1"]]
    assert item["sources"] == ["arbeitnow"]


def test_list_remote_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups", params={"remote": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["representative"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["j-1"],
    ]
    assert payload["items"][0]["group_id"] == group_id


def test_list_location_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups", params={"location": "Berlin"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["representative"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["j-1"],
    ]
    assert payload["items"][0]["group_id"] == group_id


def test_list_employment_type_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        contract = client.get("/groups", params={"employment_type": "Contract"})
        full_time = client.get("/groups", params={"employment_type": "Full-time"})

    assert contract.status_code == 200
    assert contract.json()["total"] == 1
    assert contract.json()["items"][0]["representative"]["id"] == job_ids["k-1"]

    assert full_time.status_code == 200
    assert full_time.json()["total"] == 2


def test_list_multiple_simultaneous_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/groups",
            params={"source": "arbeitnow", "remote": "true", "location": "Berlin"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["group_id"] == group_id
    assert [member["id"] for member in item["members"]] == [job_ids["a-1"]]


def test_list_unknown_source_and_employment_type_are_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        by_source = client.get("/groups", params={"source": "nonexistent"})
        by_type = client.get("/groups", params={"employment_type": "Internship"})

    for response in (by_source, by_type):
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# List: pagination
# --------------------------------------------------------------------------- #


def test_list_total_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups", params={"limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 2


def test_list_filters_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/groups", params={"location": "Berlin", "offset": 1, "limit": 1}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert [item["representative"]["id"] for item in payload["items"]] == [
        job_ids["j-1"]
    ]


def test_list_out_of_range_offset_is_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups", params={"offset": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["items"] == []


def test_list_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = client.get("/groups", params=params)
            assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def test_show_group_returns_complete_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/groups/{group_id}")

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


def test_show_unknown_group_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups/0000000000000000")

    assert response.status_code == 404
    assert response.json() == {"detail": "No group found with ID 0000000000000000."}


def test_show_arbitrary_string_group_id_is_404_not_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups/not-a-group")

    assert response.status_code == 404
    assert response.json() == {"detail": "No group found with ID not-a-group."}


def test_show_summary_route_not_captured_by_group_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups/summary")

    assert response.status_code == 200
    assert response.json() == {
        "total_jobs": 4,
        "groups": 1,
        "grouped_jobs": 2,
        "distinct_opportunities": 3,
    }


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #


def test_summary_exact_field_order_and_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups/summary")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == [
        "total_jobs",
        "groups",
        "grouped_jobs",
        "distinct_opportunities",
    ]
    assert payload == {
        "total_jobs": 4,
        "groups": 1,
        "grouped_jobs": 2,
        "distinct_opportunities": 3,
    }


def test_summary_empty_database_returns_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/groups/summary")

    assert response.status_code == 200
    assert response.json() == {
        "total_jobs": 0,
        "groups": 0,
        "grouped_jobs": 0,
        "distinct_opportunities": 0,
    }


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_list_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_summary_absent_database_returns_zeros_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_show_absent_database_returns_404_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_uninitialized_database_is_500_for_all_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_malformed_database_is_500_for_all_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
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
    _job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/groups")
        summary = client.get("/groups/summary")
        detail = client.get(f"/groups/{group_id}")

    for response in (listing, summary, detail):
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

    for item in listing.json()["items"]:
        assert "description" not in item
        assert "reason" not in item
        assert "remote_sig" not in item
        assert "location_sig" not in item
        assert "created_at" not in item
        assert "description" not in item["representative"]
        for member in item["members"]:
            assert "description" not in member


def test_workflow_state_and_private_data_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
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
        listing = client.get("/groups")
        summary = client.get("/groups/summary")
        detail = client.get(f"/groups/{group_id}")

    for response in (listing, summary, detail):
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
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(database_path)
    with connect(database_path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH,
        )
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['a-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        applications = client.get("/applications")
        application_show = client.get(f"/applications/{job_ids['a-1']}")
        reminders = client.get("/reminders")
        reminder_show = client.get("/reminders/9999")
        groups = client.get("/groups")

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
    assert show.json()["title"] == "Senior Engineer"

    assert missing.status_code == 404
    assert missing.json() == {"detail": "No job found with ID 9999."}

    assert stats.status_code == 200
    assert stats.json()["total"] == len(COLLECTORS)

    assert applications.status_code == 200
    assert applications.json()["total"] == 1

    assert application_show.status_code == 200
    assert application_show.json()["current_status"] == "applied"

    assert reminders.status_code == 200
    assert reminders.json()["total"] == 0

    assert reminder_show.status_code == 404
    assert reminder_show.json() == {"detail": "No reminder found with ID 9999."}

    assert groups.status_code == 200
    assert groups.json()["total"] == 3


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
