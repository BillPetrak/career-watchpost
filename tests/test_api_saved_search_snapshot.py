"""Focused contract tests for saved-search browser snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search
from tests.support import TestClient

_MAIN_PROFILE = {
    "desired_titles": ["Engineer"],
    "skills": ["Python"],
    "preferred_locations": ["Berlin"],
    "remote_preference": "remote",
}


def _seed_initialized_database(path: Path) -> None:
    with connect(path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                CollectedJob(
                    source="arbeitnow",
                    source_job_id="a-1",
                    title="Python Engineer",
                    company="Acme GmbH",
                    location="Berlin",
                    remote_status="remote",
                    description="Python role",
                    url="https://example.com/a-1",
                    employment_type=None,
                    published_at="2026-08-01T00:00:00Z",
                    collected_at="2026-08-01T00:00:00+00:00",
                )
            ],
        )
        create_search(
            connection,
            fields=SavedSearchFields(
                name="python-engineer",
                query="python",
                mode="rank",
                profile_path="/private/profile.yaml",
                remote="remote",
                location="Berlin",
                result_limit=10,
            ),
        )


def _use_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _set_result_limit(path: Path, value: int) -> None:
    with connect(path) as connection:
        connection.execute("UPDATE saved_searches SET result_limit = ?", (value,))
        connection.commit()


def test_snapshot_embeds_definition_and_returns_complete_result_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _use_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches/python-engineer/execute?snapshot=true",
            json={"profile": _MAIN_PROFILE},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["offset"] == 0
    assert body["limit"] == 10
    assert body["total"] == len(body["items"])
    assert body["saved_search"]["name"] == "python-engineer"
    assert body["saved_search"]["result_limit"] == 10
    assert "profile_path" not in body["saved_search"]


def test_ordinary_execution_shape_remains_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _use_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches/python-engineer/execute",
            json={"profile": _MAIN_PROFILE},
        )

    assert response.status_code == 200
    assert list(response.json()) == [
        "mode",
        "grouped",
        "items",
        "total",
        "offset",
        "limit",
    ]
    assert "employment_type" in response.json()["items"][0]["job"]
    assert response.json()["items"][0]["job"]["employment_type"] is None


@pytest.mark.parametrize("result_limit", [1, 20, 100, 500])
def test_snapshot_accepts_limits_through_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_limit: int
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_result_limit(path, result_limit)
    _use_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches/python-engineer/execute?snapshot=true",
            json={"profile": _MAIN_PROFILE},
        )

    assert response.status_code == 200
    assert response.json()["limit"] == result_limit


@pytest.mark.parametrize("result_limit", [501, 10_000_000])
def test_snapshot_rejects_over_ceiling_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_limit: int,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_result_limit(path, result_limit)
    _use_database(monkeypatch, path)

    def fail_if_executed(*args: object, **kwargs: object) -> None:
        raise AssertionError("saved-search execution should not be called")

    monkeypatch.setattr("careeros.api.main.execute_rank", fail_if_executed)
    with TestClient(app) as client:
        response = client.post(
            "/saved-searches/python-engineer/execute?snapshot=true",
            json={"profile": _MAIN_PROFILE},
        )

    assert response.status_code == 422
    assert "500" in response.json()["detail"]


@pytest.mark.parametrize("query", ["offset=1", "limit=21"])
def test_snapshot_rejects_pagination_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _use_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            f"/saved-searches/python-engineer/execute?snapshot=true&{query}",
            json={"profile": _MAIN_PROFILE},
        )

    assert response.status_code == 422
