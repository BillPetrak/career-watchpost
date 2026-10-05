from pathlib import Path

import pytest

from careeros.api.main import create_app
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from tests.support import TestClient, prepare_today_ranking


def test_today_missing_database_fails_closed_and_does_not_create_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    response = TestClient(create_app()).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}
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
    with connect(path) as connection:
        initialize_schema(connection)
        prepare_today_ranking(connection)
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
        "review",
    }
    assert payload["timezone"] == "UTC"
    assert set(payload["reminders"]) == {"overdue", "due_today"}
    assert set(payload["applications"]) == {"overdue", "due_today", "work"}
    assert set(payload["shortlist"]) == {"unstarted"}
    assert set(payload["review"]) == {"next"}
