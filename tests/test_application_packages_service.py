from __future__ import annotations

# SQL fixtures intentionally mirror the frozen schema columns.
# ruff: noqa: E501
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import careeros.application_packages.service as package_service
from careeros.application_packages.service import (
    PackageConflict,
    PackageError,
    completeness,
    confirm_answer,
    delete_version,
    finalize,
    open_draft,
    package_summary,
    rebase,
    set_answer,
    set_approval,
    set_material_selection,
    version_detail,
)
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.materials.models import StorageError
from careeros.materials.service import create_material
from careeros.materials.storage import prepare_root, stage_bytes


def fixture_db(tmp_path):
    db = connect(tmp_path / "career.sqlite3")
    initialize_schema(db)
    db.execute(
        "INSERT INTO jobs(source,title,description,collected_at,updated_at) VALUES(?,?,?,?,?)",
        ("fixture", "Fictional Engineer", "fictional description", "t0", "t0"),
    )
    job_id = db.execute("SELECT id FROM jobs").fetchone()[0]
    db.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) VALUES(?,?,?,?)",
        (job_id, "applied", "t0", "t0"),
    )
    db.commit()
    return db, job_id


def add_requirement(
    db: sqlite3.Connection,
    job_id: int,
    rid: str,
    *,
    label: str = "Question",
    sort: int = 0,
):
    db.execute(
        "INSERT INTO application_requirement_items(id,application_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid,
            1,
            "question",
            label,
            "detail",
            1,
            "open",
            None,
            sort,
            "manual",
            "manual",
            1,
            "t0",
            "t0",
        ),
    )
    db.commit()


def test_open_is_one_package_one_draft_and_snapshots(tmp_path):
    db, job = fixture_db(tmp_path)
    add_requirement(db, job, "req-1")
    first, created = open_draft(db, job)
    second, created_again = open_draft(db, job)
    assert created and not created_again
    assert first["id"] == second["id"]
    assert db.execute("SELECT count(*) FROM application_packages").fetchone()[0] == 1
    assert (
        db.execute(
            "SELECT count(*) FROM application_package_requirement_snapshots"
        ).fetchone()[0]
        == 1
    )


def test_rebase_reconciles_insert_update_delete_and_reorder_once(tmp_path):
    db, job = fixture_db(tmp_path)
    add_requirement(db, job, "req-1")
    draft, _ = open_draft(db, job)
    db.execute(
        "INSERT INTO application_preparations(application_id,revision,created_at,updated_at) VALUES(?,?,?,?)",
        (1, 1, "t0", "t0"),
    )
    db.execute("UPDATE application_requirement_items SET sort_order=1 WHERE id='req-1'")
    db.execute(
        "INSERT INTO application_requirement_items(id,application_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "req-2",
            1,
            "question",
            "Added",
            "detail",
            1,
            "open",
            None,
            0,
            "manual",
            "manual",
            1,
            "t0",
            "t0",
        ),
    )
    db.execute(
        "UPDATE application_requirement_items SET label='Changed',sort_order=1 WHERE id='req-1'"
    )
    db.commit()
    result = rebase(db, draft["id"], package_revision=0, preparation_revision=1)
    assert result["revision"] == 1
    rows = db.execute(
        "SELECT requirement_id,label,sort_order FROM application_package_requirement_snapshots ORDER BY sort_order"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("req-2", "Added", 0),
        ("req-1", "Changed", 1),
    ]
    updated = db.execute(
        "SELECT updated_at FROM application_package_versions WHERE id=?", (draft["id"],)
    ).fetchone()[0]
    assert (
        rebase(db, draft["id"], package_revision=1, preparation_revision=1)[
            "updated_at"
        ]
        == updated
    )


def test_rebase_package_conflict_precedes_preparation(tmp_path):
    db, job = fixture_db(tmp_path)
    draft, _ = open_draft(db, job)
    with pytest.raises(PackageConflict) as error:
        rebase(db, draft["id"], package_revision=7, preparation_revision=99)
    assert error.value.code == "package_revision_conflict"


def test_rebase_does_not_mutate_m1(tmp_path):
    db, job = fixture_db(tmp_path)
    add_requirement(db, job, "req-1")
    before = db.execute("SELECT * FROM application_requirement_items").fetchall()
    draft, _ = open_draft(db, job)
    db.execute(
        "INSERT INTO application_preparations(application_id,revision,created_at,updated_at) VALUES(?,?,?,?)",
        (1, 1, "t0", "t0"),
    )
    db.commit()
    rebase(db, draft["id"], package_revision=0, preparation_revision=1)
    after = db.execute("SELECT * FROM application_requirement_items").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def finalizable_material_draft(tmp_path: Path, *, requirements: int = 1):
    db, job_id = fixture_db(tmp_path)
    timestamp = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO application_preparations(application_id,revision,requirements_reviewed_revision,requirements_reviewed_at,ready_confirmed_revision,ready_confirmed_at,created_at,updated_at) VALUES(1,0,0,?,0,?,?,?)",
        (timestamp, timestamp, timestamp, timestamp),
    )
    for index in range(requirements):
        db.execute(
            "INSERT INTO application_requirement_items(id,application_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"material-{index}",
                1,
                "material",
                f"Document {index}",
                None,
                1,
                "satisfied",
                None,
                index,
                "manual",
                "manual",
                1,
                timestamp,
                timestamp,
            ),
        )
    db.commit()
    root = tmp_path / "materials"
    stage, size, digest = stage_bytes(root, [b"fictional package material"])
    material = create_material(
        db,
        root,
        name="Fictional Resume",
        category="resume",
        filename="fictional.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=digest,
    )
    draft, _ = open_draft(db, job_id, preparation_revision=0)
    revision = 0
    for index in range(requirements):
        result = set_material_selection(
            db,
            draft["id"],
            f"material-{index}",
            material["current_version"]["version_id"],
            package_revision=revision,
            preparation_revision=0,
        )
        revision = result["revision"]
    set_approval(
        db,
        draft["id"],
        approved=True,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )
    return db, draft["id"], revision, root


def test_finalization_hashes_before_collection_lock(tmp_path, monkeypatch):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    lock_held = False
    real_open = package_service._open_verified_material_metadata

    @contextmanager
    def observed_lock(*_args, **_kwargs):
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def observed_open(*args, **kwargs):
        assert not lock_held
        return real_open(*args, **kwargs)

    monkeypatch.setattr(package_service, "collection_lock", observed_lock)
    monkeypatch.setattr(
        package_service, "_open_verified_material_metadata", observed_open
    )
    result = finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
        database_path=tmp_path / "career.sqlite3",
    )
    assert result["state"] == "finalized"


def test_finalization_hashes_duplicate_selection_once(tmp_path, monkeypatch):
    db, version_id, revision, root = finalizable_material_draft(
        tmp_path, requirements=2
    )
    calls = 0
    real_open = package_service._open_verified_material_metadata

    def counted_open(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(
        package_service, "_open_verified_material_metadata", counted_open
    )
    finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )
    assert calls == 1


def test_finalization_rejects_non_private_blob_mode(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    key = db.execute("SELECT storage_key FROM material_versions").fetchone()[0]
    blob = root / "objects" / key[:2] / key[2:4] / f"{key}.blob"
    blob.chmod(0o644)
    with pytest.raises(PackageError, match="selected_material_unavailable"):
        finalize(
            db,
            version_id,
            package_revision=revision,
            preparation_revision=0,
            root=root,
        )


def test_storage_failure_is_private_unavailable_result(tmp_path, monkeypatch):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise StorageError("private path detail")

    monkeypatch.setattr(package_service, "open_bound_object_with_root", unavailable)
    with pytest.raises(PackageError) as error:
        finalize(
            db,
            version_id,
            package_revision=revision,
            preparation_revision=0,
            root=root,
        )
    assert str(error.value) == "selected_material_unavailable"


def test_finalized_retry_does_not_run_preflight_hook(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    first = finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )

    def forbidden_hook():
        raise AssertionError("idempotent retry reran preflight hook")

    second = finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
        _after_preflight=forbidden_hook,
    )
    assert second["version_id"] == first["version_id"]
    assert second["finalized_at"] == first["finalized_at"]


def test_retained_descriptor_rejects_store_marker_replacement(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)

    def replace_marker():
        marker = root / ".careeros-store"
        marker.unlink()
        marker.write_text(f"careeros-material-store-v1:{'f' * 32}\n")
        marker.chmod(0o600)

    with pytest.raises(PackageError, match="selected_material_unavailable"):
        finalize(
            db,
            version_id,
            package_revision=revision,
            preparation_revision=0,
            root=root,
            database_path=tmp_path / "career.sqlite3",
            _after_preflight=replace_marker,
        )
    assert (
        db.execute(
            "SELECT state FROM application_package_versions WHERE id=?", (version_id,)
        ).fetchone()[0]
        == "draft"
    )
    assert (
        db.execute("SELECT COUNT(*) FROM package_completeness_reasons").fetchone()[0]
        == 0
    )


def test_retained_descriptor_rejects_root_replacement(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)

    def replace_root():
        root.rename(tmp_path / "original-materials")
        prepare_root(root)

    with pytest.raises(PackageError, match="selected_material_unavailable"):
        finalize(
            db,
            version_id,
            package_revision=revision,
            preparation_revision=0,
            root=root,
            database_path=tmp_path / "career.sqlite3",
            _after_preflight=replace_root,
        )
    state = db.execute(
        "SELECT state FROM application_package_versions WHERE id=?", (version_id,)
    ).fetchone()[0]
    assert state == "draft"


def test_frozen_query_budgets(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)

    def select_count(operation):
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            operation()
        finally:
            db.set_trace_callback(None)
        return sum(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )

    job_id = int(db.execute("SELECT job_id FROM applications").fetchone()[0])
    assert select_count(lambda: package_summary(db, job_id)) <= 8
    assert select_count(lambda: version_detail(db, version_id, root=root)) <= 5
    assert (
        select_count(
            lambda: finalize(
                db,
                version_id,
                package_revision=revision,
                preparation_revision=0,
                root=root,
            )
        )
        <= 10
    )


def test_finalization_lock_contention_preserves_draft(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    database_path = tmp_path / "career.sqlite3"
    with collection_lock(database_path, command="test holder"):
        with pytest.raises(CollectionLockConflict):
            finalize(
                db,
                version_id,
                package_revision=revision,
                preparation_revision=0,
                root=root,
                database_path=database_path,
            )
    assert (
        db.execute(
            "SELECT state FROM application_package_versions WHERE id=?", (version_id,)
        ).fetchone()[0]
        == "draft"
    )


def test_answer_normalization_confirmation_and_noops(tmp_path):
    db, job_id = fixture_db(tmp_path)
    timestamp = "2026-01-01T00:00:00+00:00"
    add_requirement(db, job_id, "question-1")
    db.execute(
        "INSERT INTO application_preparations(application_id,revision,created_at,updated_at) VALUES(1,0,?,?)",
        (timestamp, timestamp),
    )
    db.commit()
    draft, _ = open_draft(db, job_id)
    saved = set_answer(
        db,
        draft["id"],
        "question-1",
        "Line one\r\nLine two",
        package_revision=0,
        preparation_revision=0,
    )
    assert saved["revision"] == 1
    answer = db.execute("SELECT * FROM package_answers").fetchone()
    assert answer["answer_text"] == "Line one\nLine two"
    unchanged_at = answer["updated_at"]
    no_op = set_answer(
        db,
        draft["id"],
        "question-1",
        "Line one\rLine two",
        package_revision=1,
        preparation_revision=0,
    )
    assert no_op["revision"] == 1
    assert (
        db.execute("SELECT updated_at FROM package_answers").fetchone()[0]
        == unchanged_at
    )
    confirmed = confirm_answer(
        db,
        draft["id"],
        "question-1",
        confirmed=True,
        package_revision=1,
        preparation_revision=0,
    )
    assert confirmed["revision"] == 2
    with pytest.raises(PackageError, match="invalid_package_answer"):
        set_answer(
            db,
            draft["id"],
            "question-1",
            "bad\x07answer",
            package_revision=2,
            preparation_revision=0,
        )


def test_completeness_reason_vocabulary_is_globally_ordered(tmp_path):
    db, job_id = fixture_db(tmp_path)
    add_requirement(db, job_id, "question-b", sort=1)
    add_requirement(db, job_id, "question-a", sort=0)
    db.execute(
        "UPDATE application_requirement_items SET confirmed=0 WHERE id='question-a'"
    )
    db.commit()
    draft, _ = open_draft(db, job_id)
    result = completeness(db, draft["id"], root=None)
    codes = [reason["code"] for reason in result["reasons"]]
    assert codes == [
        "requirements_not_reviewed",
        "requirement_unconfirmed",
        "required_requirement_open",
        "required_requirement_open",
        "ready_confirmation_missing",
        "answer_missing",
        "answer_missing",
    ]
    assert [reason["position"] for reason in result["reasons"]] == list(
        range(len(codes))
    )


def test_finalization_authority_race_rolls_back_freeze(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)

    def mutate_job_after_preflight():
        db.execute("UPDATE jobs SET description='Changed fictional job'")
        db.commit()

    with pytest.raises(PackageError, match="package_job_stale"):
        finalize(
            db,
            version_id,
            package_revision=revision,
            preparation_revision=0,
            root=root,
            _after_preflight=mutate_job_after_preflight,
        )
    row = db.execute(
        "SELECT state,finalized_at FROM application_package_versions WHERE id=?",
        (version_id,),
    ).fetchone()
    assert tuple(row) == ("draft", None)
    assert (
        db.execute("SELECT COUNT(*) FROM package_completeness_reasons").fetchone()[0]
        == 0
    )


def test_concurrent_create_returns_one_logical_package_and_draft(tmp_path):
    db, job_id = fixture_db(tmp_path)
    path = tmp_path / "career.sqlite3"
    db.close()
    barrier = threading.Barrier(2)

    def create() -> tuple[str, bool]:
        connection = connect(path)
        try:
            barrier.wait()
            draft, created = open_draft(connection, job_id, preparation_revision=0)
            return str(draft["id"]), created
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: create(), range(2)))
    check = connect(path)
    assert {version_id for version_id, _created in results} == {results[0][0]}
    assert sorted(created for _version_id, created in results) == [False, True]
    assert check.execute("SELECT COUNT(*) FROM application_packages").fetchone()[0] == 1
    assert (
        check.execute("SELECT COUNT(*) FROM application_package_versions").fetchone()[0]
        == 1
    )


def test_finalization_race_returns_same_frozen_result(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    second = connect(tmp_path / "career.sqlite3")
    raced: dict[str, object] = {}

    def finalize_other_request():
        raced.update(
            finalize(
                second,
                version_id,
                package_revision=revision,
                preparation_revision=0,
                root=root,
                database_path=tmp_path / "career.sqlite3",
            )
        )

    first = finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
        database_path=tmp_path / "career.sqlite3",
        _after_preflight=finalize_other_request,
    )
    assert first == raced
    assert first["state"] == "finalized"
    assert (
        db.execute("SELECT COUNT(*) FROM package_completeness_reasons").fetchone()[0]
        == 1
    )


def test_large_fork_and_rebase_select_budgets_are_batched(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(
        tmp_path, requirements=200
    )
    finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )
    job_id = int(db.execute("SELECT job_id FROM applications").fetchone()[0])
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    fork, created = open_draft(db, job_id, preparation_revision=0)
    db.set_trace_callback(None)
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert created
    assert len(selects) == 12
    assert (
        sum(
            "FROM MATERIAL_VERSIONS V JOIN MATERIALS M" in query.upper()
            for query in selects
        )
        == 1
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM package_material_selections WHERE package_version_id=?",
            (fork["id"],),
        ).fetchone()[0]
        == 200
    )

    statements.clear()
    db.set_trace_callback(statements.append)
    rebase(db, fork["id"], package_revision=0, preparation_revision=0)
    db.set_trace_callback(None)
    assert (
        sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        == 7
    )


def test_semantic_job_reconfirmation_uses_strictly_newer_aware_utc_times(tmp_path):
    db, job_id = fixture_db(tmp_path)
    timestamp = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO application_preparations(application_id,revision,requirements_reviewed_revision,requirements_reviewed_at,ready_confirmed_revision,ready_confirmed_at,created_at,updated_at) VALUES(1,0,0,?,0,?,?,?)",
        (timestamp, timestamp, timestamp, timestamp),
    )
    db.commit()
    draft, _ = open_draft(db, job_id, preparation_revision=0)
    db.execute(
        "UPDATE jobs SET description='Semantically changed fictional description'"
    )
    db.commit()
    rebased = rebase(db, draft["id"], package_revision=0, preparation_revision=0)
    required_at = datetime.fromisoformat(str(rebased["job_reconfirmation_required_at"]))
    newer = (
        (required_at + timedelta(seconds=1))
        .astimezone(timezone(timedelta(hours=-5)))
        .isoformat()
    )
    db.execute(
        "UPDATE application_preparations SET requirements_reviewed_at=?,ready_confirmed_at=? WHERE application_id=1",
        (newer, newer),
    )
    db.commit()
    result = completeness(db, draft["id"], root=None, now=datetime.now(UTC))
    assert "job_reconfirmation_required" not in {
        reason["code"] for reason in result["reasons"]
    }


def test_finalization_leaves_full_m1_preparation_row_byte_identical(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    before = tuple(db.execute("SELECT * FROM application_preparations").fetchone())
    finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )
    after = tuple(db.execute("SELECT * FROM application_preparations").fetchone())
    assert after == before


def test_deadline_equality_is_blocking(tmp_path):
    db, job_id = fixture_db(tmp_path)
    instant = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    db.execute(
        "INSERT INTO application_preparations(application_id,revision,requirements_reviewed_revision,requirements_reviewed_at,ready_confirmed_revision,ready_confirmed_at,created_at,updated_at) VALUES(1,0,0,?,0,?,?,?)",
        (
            instant.isoformat(),
            instant.isoformat(),
            instant.isoformat(),
            instant.isoformat(),
        ),
    )
    db.execute(
        "INSERT INTO application_requirement_items(id,application_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES('deadline-1',1,'deadline','Exact deadline',NULL,1,'open',?,0,'manual','manual',1,?,?)",
        (instant.isoformat(), instant.isoformat(), instant.isoformat()),
    )
    db.commit()
    draft, _ = open_draft(db, job_id)
    result = completeness(db, draft["id"], root=None, now=instant)
    assert "required_deadline_passed" in {
        reason["code"] for reason in result["reasons"]
    }


def test_fork_preserves_retired_material_and_restore_recovers_authority(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    finalize(
        db, version_id, package_revision=revision, preparation_revision=0, root=root
    )
    db.execute(
        "UPDATE materials SET status='retired',retired_at='t1',revision=revision+1"
    )
    db.commit()
    job_id = int(db.execute("SELECT job_id FROM applications").fetchone()[0])
    fork, _ = open_draft(db, job_id, preparation_revision=0)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM package_material_selections WHERE package_version_id=?",
            (fork["id"],),
        ).fetchone()[0]
        == 1
    )
    assert "selected_material_retired" in {
        reason["code"] for reason in completeness(db, fork["id"], root=root)["reasons"]
    }
    delete_version(db, str(fork["id"]), package_revision=0)
    db.execute(
        "UPDATE materials SET status='active',retired_at=NULL,revision=revision+1"
    )
    db.commit()
    restored_fork, created = open_draft(db, job_id, preparation_revision=0)
    assert created
    assert (
        db.execute(
            "SELECT COUNT(*) FROM package_material_selections WHERE package_version_id=?",
            (restored_fork["id"],),
        ).fetchone()[0]
        == 1
    )
    assert "selected_material_retired" not in {
        reason["code"]
        for reason in completeness(db, restored_fork["id"], root=root)["reasons"]
    }


def test_fork_preserves_unavailable_exact_material_reference(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    finalize(
        db, version_id, package_revision=revision, preparation_revision=0, root=root
    )
    key = str(db.execute("SELECT storage_key FROM material_versions").fetchone()[0])
    (root / "objects" / key[:2] / key[2:4] / f"{key}.blob").unlink()
    job_id = int(db.execute("SELECT job_id FROM applications").fetchone()[0])
    fork, _ = open_draft(db, job_id, preparation_revision=0)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM package_material_selections WHERE package_version_id=?",
            (fork["id"],),
        ).fetchone()[0]
        == 1
    )
    assert "selected_material_unavailable" in {
        reason["code"] for reason in completeness(db, fork["id"], root=root)["reasons"]
    }


def test_finalized_deletion_maps_future_reference_conflict(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    finalize(
        db, version_id, package_revision=revision, preparation_revision=0, root=root
    )
    db.execute(
        "CREATE TABLE future_package_refs(version_id TEXT NOT NULL REFERENCES application_package_versions(id) ON DELETE RESTRICT)"
    )
    db.execute("INSERT INTO future_package_refs VALUES(?)", (version_id,))
    db.commit()
    with pytest.raises(PackageError, match="package_version_referenced"):
        delete_version(db, version_id, package_revision=revision)
    assert (
        db.execute(
            "SELECT state FROM application_package_versions WHERE id=?", (version_id,)
        ).fetchone()[0]
        == "finalized"
    )


def test_material_display_metadata_change_does_not_block_and_fork_refreshes(tmp_path):
    db, version_id, revision, root = finalizable_material_draft(tmp_path)
    db.execute(
        "UPDATE materials SET name='Renamed Fictional Resume',category='other',revision=revision+1"
    )
    db.commit()
    finalized = finalize(
        db,
        version_id,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )
    assert finalized["state"] == "finalized"
    job_id = int(db.execute("SELECT job_id FROM applications").fetchone()[0])
    fork, created = open_draft(db, job_id, preparation_revision=0)
    assert created
    selection = db.execute(
        "SELECT material_name_snapshot,material_category_snapshot FROM package_material_selections WHERE package_version_id=?",
        (fork["id"],),
    ).fetchone()
    assert tuple(selection) == ("Renamed Fictional Resume", "other")


def test_retained_version_cap_counts_rows_and_never_reuses_ordinals(tmp_path):
    db, job_id = fixture_db(tmp_path)
    draft, _ = open_draft(db, job_id)
    db.execute(
        "UPDATE application_package_versions SET version_number=2147483647 WHERE id=?",
        (draft["id"],),
    )
    for number in range(1, 101):
        db.execute(
            """INSERT INTO application_package_versions(
                   id,package_id,version_number,state,revision,preparation_revision,
                   forked_from_version_id,forked_from_version_number,
                   job_id_snapshot,job_source,job_source_job_id,job_title,job_company,
                   job_location,job_url,job_employment_type,job_remote_status,
                   job_published_at,job_updated_at_snapshot,job_description_present,
                   job_content_fingerprint,job_reconfirmation_required_at,
                   approved_revision,approved_at,finalized_at,
                   final_completeness_category,created_at,updated_at)
               SELECT ?,package_id,?,'finalized',revision,preparation_revision,
                      forked_from_version_id,forked_from_version_number,
                      job_id_snapshot,job_source,job_source_job_id,job_title,job_company,
                      job_location,job_url,job_employment_type,job_remote_status,
                      job_published_at,job_updated_at_snapshot,job_description_present,
                      job_content_fingerprint,job_reconfirmation_required_at,
                      revision,'t','t','complete',created_at,updated_at
               FROM application_package_versions WHERE id=?""",
            (f"{number:032x}", number, draft["id"]),
        )
    db.execute("DELETE FROM application_package_versions WHERE id=?", (draft["id"],))
    db.execute("UPDATE application_packages SET next_version_number=101")
    db.commit()
    with pytest.raises(PackageError, match="package_version_limit_reached"):
        open_draft(db, job_id)
    delete_version(db, f"{1:032x}", package_revision=0)
    replacement, created = open_draft(db, job_id)
    assert created
    assert replacement["version_number"] == 101


def test_version_deletion_is_cas_guarded_and_cleans_the_empty_package(tmp_path):
    db, job_id = fixture_db(tmp_path)
    add_requirement(db, job_id, "question-1")
    draft, _ = open_draft(db, job_id)
    package_id = draft["package_id"]

    with pytest.raises(PackageConflict) as error:
        delete_version(db, draft["id"], package_revision=1)
    assert error.value.code == "package_revision_conflict"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM application_package_versions WHERE id=?",
            (draft["id"],),
        ).fetchone()[0]
        == 1
    )

    delete_version(db, draft["id"], package_revision=0)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM application_packages WHERE id=?", (package_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM application_package_requirement_snapshots WHERE package_version_id=?",
            (draft["id"],),
        ).fetchone()[0]
        == 0
    )
