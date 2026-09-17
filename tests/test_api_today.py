from pathlib import Path

import pytest

from careeros.api.main import create_app
from tests.support import TestClient


def test_today_missing_database_is_empty_and_does_not_create_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    response = TestClient(create_app()).get("/today")
    assert response.status_code == 200
    assert response.json()["reminders"]["overdue"] == {"total": 0, "items": []}
    assert response.json()["applications"] == {
        "overdue": {"total": 0, "items": []},
        "due_today": {"total": 0, "items": []},
        "work": {"total": 0, "items": []},
    }
    assert not path.exists()


def test_today_limit_validation_and_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "today.sqlite3"
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    assert TestClient(create_app()).get("/today?limit=0").status_code == 422
    assert TestClient(create_app()).get("/today?limit=21").status_code == 422


def test_today_response_is_publicly_pruned_and_uses_server_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "today.sqlite3"
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    response = TestClient(create_app()).get("/today?limit=20")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "as_of",
        "timezone",
        "today_ends_at",
        "limit",
        "reminders",
        "applications",
        "shortlist",
    }
    assert payload["timezone"] == "UTC"
    assert set(payload["reminders"]) == {"overdue", "due_today"}
    assert set(payload["applications"]) == {"overdue", "due_today", "work"}
    assert set(payload["shortlist"]) == {"unstarted"}
