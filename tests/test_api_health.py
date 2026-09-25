"""Tests for the CareerOS API health endpoint (first slice)."""

from __future__ import annotations

from pathlib import Path

import pytest

from careeros.api import app
from careeros.database.connection import connect
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from tests.support import TestClient


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _seed(path: Path) -> None:
    connection = connect(path)
    initialize_schema(connection)
    connection.close()


def test_health_ok_with_absent_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_database(monkeypatch, tmp_path / "missing.sqlite3")

    with pytest.raises(ValueError, match="current schema v13"):
        with TestClient(app):
            pass


def test_health_ok_with_initialized_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database"] == "present"
    assert payload["schema_version"] == SCHEMA_VERSION


def test_health_schema_version_null_for_uninitialized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    _set_database(monkeypatch, path)

    with pytest.raises(ValueError, match="current schema v13"):
        with TestClient(app):
            pass
