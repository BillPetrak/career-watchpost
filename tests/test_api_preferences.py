"""R4 M3 preference HTTP inspection and reversal contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from careeros.api import app
from careeros.api import deps as api_deps
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.preferences import list_preference_evidence
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict
from careeros.preferences.service import inspect_preferences, replace_explicit_profile
from careeros.ranking.scorer import CandidateProfile
from careeros.review.service import set_decision
from tests.support import TestClient

EXPLICIT_FIELDS = {
    "desired_titles",
    "skills",
    "required_skills",
    "preferred_locations",
    "remote_preference",
    "excluded_terms",
    "hard_excluded_terms",
    "preferred_employment_types",
    "preferred_seniority",
}
LEARNED_FIELDS = {
    "key",
    "dimension",
    "display_value",
    "normalized_value",
    "polarity",
    "positive_count",
    "negative_count",
    "net_evidence",
    "strength_level",
    "active",
    "activation_reason",
    "suppressed",
    "positive_examples",
    "negative_examples",
}


def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "api-preferences.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    return path


def _seed(path: Path, decisions: tuple[str, ...] = ("shortlist", "shortlist")) -> str:
    with connect(path) as connection:
        save_jobs(
            connection,
            [
                CollectedJob(
                    source=f"provider-{index}",
                    source_job_id=str(index),
                    title="Backend Engineer",
                    company=f"Company {index}",
                    location=f"City {index}",
                    description=f"private-description-{index}",
                    url=f"https://private.example/{index}",
                    employment_type="Full-time",
                    remote_status="remote",
                    published_at=None,
                    collected_at="2026-09-16T12:00:00+00:00",
                )
                for index in range(len(decisions))
            ],
        )
        ensure_grouped(connection)
        ids = [int(row[0]) for row in connection.execute("SELECT id FROM jobs")]
        for job_id, decision in zip(ids, decisions, strict=True):
            set_decision(
                connection,
                job_id=job_id,
                decision=decision,
                note="private-review-note",
            )
        return next(
            item.preference_key
            for item in inspect_preferences(connection).learned
            if item.dimension == "title"
        )


def test_get_preferences_exact_allow_list_and_configured_empty_distinction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    with TestClient(app) as client:
        absent = client.get("/preferences")
        empty = client.put("/preferences/explicit", json={})

    assert absent.status_code == 200
    assert set(absent.json()) == {
        "explicit",
        "learned",
        "learning_reset_at",
        "learning_reset_review_event_id",
    }
    assert absent.json()["explicit"]["configured"] is False
    assert set(absent.json()["explicit"]) == {
        "configured",
        "updated_at",
        "profile",
        "locked_dimensions",
    }
    assert set(absent.json()["explicit"]["profile"]) == EXPLICIT_FIELDS
    assert empty.status_code == 200
    assert empty.json()["explicit"]["configured"] is True
    assert empty.json()["explicit"]["profile"] == absent.json()["explicit"]["profile"]
    with connect(path) as connection:
        assert inspect_preferences(connection).explicit.configured is True


def test_explicit_replace_preserves_all_fields_order_duplicates_and_clear_is_narrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    key = _seed(path)
    payload = {
        "desired_titles": ["Backend", "Backend", "Platform"],
        "skills": ["Python", "SQL", "Python"],
        "required_skills": ["Git", "Git"],
        "preferred_locations": ["Athens", "Berlin", "Athens"],
        "remote_preference": "hybrid",
        "excluded_terms": ["crypto", "crypto"],
        "hard_excluded_terms": ["unpaid", "unpaid"],
        "preferred_employment_types": ["full-time", "contract", "full-time"],
        "preferred_seniority": ["senior", "lead", "senior"],
    }
    with TestClient(app) as client:
        saved = client.put("/preferences/explicit", json=payload)
        client.put(f"/preferences/learned/{key}", json={"suppressed": True})
        cleared = client.delete("/preferences/explicit")
    assert saved.status_code == 200
    assert saved.json()["explicit"]["profile"] == payload
    assert cleared.status_code == 200
    assert cleared.json()["explicit"]["configured"] is False
    assert any(item["suppressed"] for item in cleared.json()["learned"])
    with connect(path) as connection:
        assert len(list_preference_evidence(connection)) > 0
        assert (
            connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 2
        )


def test_learned_read_suppress_restore_shapes_counts_and_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    key = _seed(path, ("shortlist",) * 5 + ("dismiss",) * 2)
    with connect(path) as connection:
        evidence = list_preference_evidence(connection)
        events = tuple(connection.execute("SELECT * FROM review_events"))
    with TestClient(app) as client:
        learned = client.get("/preferences/learned")
        suppressed = client.put(
            f"/preferences/learned/{key}", json={"suppressed": True}
        )
        restored = client.put(f"/preferences/learned/{key}", json={"suppressed": False})
    assert learned.status_code == 200
    assert set(learned.json()) == {"items"}
    title = next(item for item in learned.json()["items"] if item["key"] == key)
    assert set(title) == LEARNED_FIELDS
    assert title["positive_count"] == 5
    assert title["negative_count"] == 2
    assert len(title["positive_examples"]) == 3
    assert set(title["positive_examples"][0]) == {"job_id", "title", "company"}
    assert (
        next(item for item in suppressed.json()["learned"] if item["key"] == key)[
            "suppressed"
        ]
        is True
    )
    restored_title = next(
        item for item in restored.json()["learned"] if item["key"] == key
    )
    assert restored_title["suppressed"] is False
    assert restored_title["active"] is True
    combined = learned.text + suppressed.text + restored.text
    for private in (
        "private-description",
        "private.example",
        "private-review-note",
        "provider-",
    ):
        assert private not in combined
    with connect(path) as connection:
        assert list_preference_evidence(connection) == evidence
        assert tuple(connection.execute("SELECT * FROM review_events")) == events


def test_reset_returns_authoritative_state_and_preserves_explicit_and_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    key = _seed(path)
    with connect(path) as connection:
        replace_explicit_profile(connection, CandidateProfile(skills=("Python",)))
        events = tuple(connection.execute("SELECT * FROM review_events"))
        decisions = tuple(connection.execute("SELECT * FROM review_decisions"))
        boundary = max(int(row["id"]) for row in events)
    with TestClient(app) as client:
        client.put(f"/preferences/learned/{key}", json={"suppressed": True})
        response = client.delete("/preferences/learned")
    assert response.status_code == 200
    payload = response.json()
    assert payload["learned"] == []
    assert payload["explicit"]["configured"] is True
    assert payload["explicit"]["profile"]["skills"] == ["Python"]
    assert payload["learning_reset_review_event_id"] == boundary
    assert payload["learning_reset_at"] is not None
    with connect(path) as connection:
        assert tuple(connection.execute("SELECT * FROM review_events")) == events
        assert tuple(connection.execute("SELECT * FROM review_decisions")) == decisions


def test_write_security_and_validation_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)
    key = _seed(path)
    with TestClient(app, auto_write=False) as client:
        assert client.put("/preferences/explicit", json={}).status_code == 403
        assert client.delete("/preferences/explicit").status_code == 403
        assert (
            client.put(
                f"/preferences/learned/{key}", json={"suppressed": True}
            ).status_code
            == 403
        )
        assert client.delete("/preferences/learned").status_code == 403
        assert (
            client.put(
                "/preferences/explicit",
                headers={"X-CareerOS-Write": "1", "Origin": "http://evil.example"},
                json={},
            ).status_code
            == 403
        )
    with TestClient(app) as client:
        assert (
            client.put(
                "/preferences/explicit", json={"profile_path": "/private"}
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/preferences/learned/{key}", json={"suppressed": "yes"}
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/preferences/learned/bad", json={"suppressed": True}
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/preferences/learned/" + "0" * 32, json={"suppressed": True}
            ).status_code
            == 404
        )


def test_preference_write_lock_contention_is_privacy_safe_and_non_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, monkeypatch)

    def blocked(*args, **kwargs):
        raise CollectionLockConflict(
            path, Path(str(path) + ".lock"), {"pid": "private"}
        )

    monkeypatch.setattr(api_deps, "collection_lock", blocked)
    with TestClient(app) as client:
        response = client.put("/preferences/explicit", json={"skills": ["Python"]})
    assert response.status_code == 503
    assert response.json() == {
        "detail": "The database is temporarily busy; retry the request later."
    }
    assert str(path) not in response.text
    assert "private" not in response.text
    with connect(path) as connection:
        assert inspect_preferences(connection).explicit.configured is False
