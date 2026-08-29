"""Regression coverage for the inline matching-profile HTTP contract.

M3 deliberately reuses this computation-only API.  These tests pin the
complete nine-field payload, preserve the intentionally open remote/seniority
vocabulary, and guard the privacy/no-feedback boundaries needed by the
browser's memory-only profile coordinator.
"""

from __future__ import annotations

from pathlib import Path

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from tests.support import TestClient


def _seed(path: Path) -> None:
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            CollectedJob(
                source="fixture",
                source_job_id="m3-1",
                title="Principal Platform Engineer",
                company="Example GmbH",
                location="Anywhere",
                description="Python and distributed systems.",
                url="https://example.test/m3-1",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-08-01T10:00:00Z",
                collected_at="2026-08-02T08:00:00+00:00",
            )
        ],
    )
    connection.close()


def test_ranked_jobs_accepts_complete_profile_without_reinterpreting_terms(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    profile = {
        "desired_titles": [" Platform Engineer ", "Platform Engineer"],
        "required_skills": ["Python", "Python"],
        "skills": ["distributed systems"],
        "preferred_locations": ["Anywhere"],
        "remote_preference": "  unusual-remote-vocabulary  ",
        "preferred_employment_types": ["Full-time"],
        "preferred_seniority": ["Principal-ish", "Principal-ish"],
        "excluded_terms": ["legacy"],
        "hard_excluded_terms": ["secret-hostile-term"],
    }

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": profile, "untracked": True},
            params={"minimum_score": 0, "grouped": False, "limit": 20},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["job"]["source_job_id"] == "m3-1"
    # Submitted profile terms are computation inputs, never response data.
    assert "unusual-remote-vocabulary" not in response.text
    assert "secret-hostile-term" not in response.text


def test_remote_and_seniority_are_not_lossily_enum_validated(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={
                "profile": {
                    "remote_preference": "provider-specific-mode",
                    "preferred_seniority": ["L7 / Staff+", "custom-level"],
                }
            },
        )

    assert response.status_code == 200


def test_ranked_jobs_profile_probe_is_inert_and_feedback_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    profile_path = str(tmp_path / "private-profile.json")

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": {}, "profile_path": profile_path},
        )

    assert response.status_code == 200
    assert profile_path not in response.text
    assert "feedback" not in response.text.lower()
