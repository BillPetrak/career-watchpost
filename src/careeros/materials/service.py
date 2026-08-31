from __future__ import annotations

import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from careeros.materials.models import (
    CATEGORIES,
    MAX_VERSIONS,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from careeros.materials.storage import (
    ensure_binding,
    ensure_binding_fd,
    finalize_stage,
    install_marker_fd,
    open_root_handle,
    remove_marker_fd,
    remove_object_fd,
    remove_stage,
    root_identity,
    stage_identity,
    verify_stage,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return secrets.token_hex(16)


def validate_metadata(
    name: str, category: str, filename: str, media_type: str
) -> tuple[str, str, str, str]:
    _validate_name_category(name, category)
    filename = unicodedata.normalize("NFC", filename)
    if (
        not filename.strip()
        or not 1 <= len(filename) <= 255
        or len(filename.encode("utf-8")) > 1024
        or filename in (".", "..")
        or any(unicodedata.category(c).startswith("C") or c in "/\\" for c in filename)
    ):
        raise ValidationError("invalid material metadata")
    if media_type == "":
        media_type = "application/octet-stream"
    grammar = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,63}/[A-Za-z0-9!#$%&'*+.^_`|~-]{1,63}$"
    if not 3 <= len(media_type) <= 127 or not re.fullmatch(grammar, media_type):
        raise ValidationError("invalid material metadata")
    return name, category, filename, media_type.lower()


def _validate_name_category(name: str, category: str) -> None:
    if not 1 <= len(name) <= 160 or name != name.strip():
        raise ValidationError("invalid material metadata")
    if category not in CATEGORIES:
        raise ValidationError("invalid material metadata")


def _validate_staged(size: int, sha256: str) -> None:
    if not 1 <= size <= 25 * 1024 * 1024:
        raise ValidationError("invalid staged material")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValidationError("invalid staged material")


def _store(c: sqlite3.Connection, root: Path, root_fd: int) -> tuple[str, bool]:
    row = c.execute("SELECT store_id FROM material_store WHERE singleton=1").fetchone()
    found = ensure_binding_fd(root_fd, root, row[0] if row else None)
    if row:
        return str(row[0]), False
    material_rows = int(c.execute("SELECT count(*) FROM materials").fetchone()[0])
    version_rows = int(
        c.execute("SELECT count(*) FROM material_versions").fetchone()[0]
    )
    if material_rows or version_rows:
        raise ConflictError(
            "materials storage binding unavailable", code="material_binding_conflict"
        )
    store_id = found or secrets.token_hex(16)
    marker_created = found is None
    if marker_created:
        install_marker_fd(root_fd, store_id)
    c.execute(
        "INSERT INTO material_store(singleton,store_id,created_at) VALUES(1,?,?)",
        (store_id, now()),
    )
    return store_id, marker_created


def preflight_binding(c: sqlite3.Connection, root: Path) -> str | None:
    """Read-only validation for upload callers before creating a stage.

    An unbound database with an absent root is allowed to remain unbound; a
    bound database must already have its exact safe root and marker.  This
    deliberately uses ``allow_create=False`` and never initializes or repairs
    the managed root.
    """
    row = c.execute("SELECT store_id FROM material_store WHERE singleton=1").fetchone()
    return ensure_binding(root, row[0] if row else None, allow_create=False)


def _version(row: sqlite3.Row) -> dict[str, object]:
    return {
        "version_id": row["id"],
        "version_number": row["version_number"],
        "display_filename": row["display_filename"],
        "declared_media_type": row["declared_media_type"],
        "byte_size": row["byte_size"],
        "created_at": row["created_at"],
    }


def _detail(c: sqlite3.Connection, material_id: str) -> dict[str, object]:
    m = c.execute(
        "SELECT m.*, count(v.id) version_count FROM materials m "
        "LEFT JOIN material_versions v ON v.material_id=m.id "
        "WHERE m.id=? GROUP BY m.id",
        (material_id,),
    ).fetchone()
    if m is None:
        raise NotFoundError("material not found")
    versions = c.execute(
        "SELECT * FROM material_versions WHERE material_id=? "
        "ORDER BY version_number DESC,id",
        (material_id,),
    ).fetchall()
    return {
        "material_id": m["id"],
        "name": m["name"],
        "category": m["category"],
        "status": m["status"],
        "revision": m["revision"],
        "version_count": m["version_count"],
        "current_version": _version(versions[0]) if versions else None,
        "retired_at": m["retired_at"],
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
        "versions": [_version(v) for v in versions],
    }


def create_material(
    c: sqlite3.Connection,
    root: Path,
    *,
    name: str,
    category: str,
    filename: str,
    media_type: str,
    stage: Path,
    size: int,
    sha256: str,
) -> dict[str, object]:
    name, category, filename, media_type = validate_metadata(
        name, category, filename, media_type
    )
    _validate_staged(size, sha256)
    staged_identity = stage_identity(stage)
    final = None
    marker_created = False
    original_root_fd = -1
    object_key: str | None = None
    try:
        c.execute("BEGIN IMMEDIATE")
        count = c.execute("SELECT count(*) FROM materials").fetchone()[0]
        if int(count) >= 1000:
            raise ConflictError("material limit", code="material_library_limit_reached")
        # Do not create/adopt the store binding until all database-only
        # admission checks have passed.  A rejected import must not leave a
        # marker (or any other binding side effect) behind.
        original_root_fd = open_root_handle(root)
        store_id, marker_created = _store(c, root, original_root_fd)
        mid, vid, key, timestamp = _id(), _id(), secrets.token_hex(32), now()
        object_key = key
        identity = root_identity(root)
        verify_stage(stage, size, staged_identity)
        final = finalize_stage(
            root,
            stage,
            key,
            expected_size=size,
            expected_identity=staged_identity,
            root_fd=original_root_fd,
            expected_store_id=store_id,
        )
        if root_identity(root) != identity:
            raise ConflictError(
                "materials storage binding unavailable",
                code="material_binding_conflict",
            )
        c.execute(
            "INSERT INTO materials VALUES (?,?,?,?,?,?,?,?)",
            (mid, name, category, 1, "active", None, timestamp, timestamp),
        )
        c.execute(
            "INSERT INTO material_versions VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, mid, 1, filename, media_type, size, sha256, key, timestamp),
        )
        c.commit()
        return _detail(c, mid)
    except Exception:
        c.rollback()
        if final is not None:
            assert original_root_fd >= 0 and object_key is not None
            try:
                remove_object_fd(original_root_fd, object_key)
            except Exception:
                pass
        if marker_created:
            assert original_root_fd >= 0
            try:
                remove_marker_fd(original_root_fd)
            except Exception:
                pass
        raise
    finally:
        if original_root_fd >= 0:
            os.close(original_root_fd)


def add_version(
    c: sqlite3.Connection,
    root: Path,
    material_id: str,
    *,
    expected_revision: int,
    filename: str,
    media_type: str,
    stage: Path,
    size: int,
    sha256: str,
) -> tuple[dict[str, object], int]:
    _, _, filename, media_type = validate_metadata("x", "resume", filename, media_type)
    _validate_staged(size, sha256)
    staged_identity = stage_identity(stage)
    c.execute("BEGIN IMMEDIATE")
    final = None
    marker_created = False
    original_root_fd = -1
    object_key: str | None = None
    try:
        m = c.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if m is None:
            raise NotFoundError("material not found")
        if m["status"] != "active":
            raise ConflictError(
                "retired material",
                code="material_retired",
                current_revision=int(m["revision"]),
            )
        duplicate = c.execute(
            "SELECT * FROM material_versions WHERE material_id=? AND sha256=?",
            (material_id, sha256),
        ).fetchone()
        if duplicate:
            c.rollback()
            c.commit()
            remove_stage(stage)
            return _detail(c, material_id), 200
        if int(expected_revision) != m["revision"]:
            raise ConflictError(
                "material revision conflict",
                code="material_revision_conflict",
                current_revision=int(m["revision"]),
            )
        n = int(
            c.execute(
                "SELECT coalesce(max(version_number),0)+1 "
                "FROM material_versions WHERE material_id=?",
                (material_id,),
            ).fetchone()[0]
        )
        if n > MAX_VERSIONS:
            raise ConflictError(
                "version limit",
                code="material_version_limit_reached",
                current_revision=int(m["revision"]),
            )
        original_root_fd = open_root_handle(root)
        store_id, marker_created = _store(c, root, original_root_fd)
        key = secrets.token_hex(32)
        object_key = key
        vid = _id()
        ts = now()
        identity = root_identity(root)
        verify_stage(stage, size, staged_identity)
        final = finalize_stage(
            root,
            stage,
            key,
            expected_size=size,
            expected_identity=staged_identity,
            root_fd=original_root_fd,
            expected_store_id=store_id,
        )
        if root_identity(root) != identity:
            raise ConflictError(
                "materials storage binding unavailable",
                code="material_binding_conflict",
            )
        c.execute(
            "INSERT INTO material_versions VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, material_id, n, filename, media_type, size, sha256, key, ts),
        )
        c.execute(
            "UPDATE materials SET revision=revision+1,updated_at=? WHERE id=?",
            (ts, material_id),
        )
        c.commit()
        return _detail(c, material_id), 201
    except Exception:
        c.rollback()
        if final is not None:
            assert original_root_fd >= 0 and object_key is not None
            try:
                remove_object_fd(original_root_fd, object_key)
            except Exception:
                pass
        if marker_created:
            assert original_root_fd >= 0
            try:
                remove_marker_fd(original_root_fd)
            except Exception:
                pass
        raise
    finally:
        if original_root_fd >= 0:
            os.close(original_root_fd)


def get_material(c: sqlite3.Connection, material_id: str) -> dict[str, object]:
    return _detail(c, material_id)


def update_material(
    c: sqlite3.Connection,
    material_id: str,
    *,
    expected_revision: int,
    name: str | None = None,
    category: str | None = None,
) -> dict[str, object]:
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("material not found")
        if row["revision"] != expected_revision:
            raise ConflictError(
                "material revision conflict",
                code="material_revision_conflict",
                current_revision=int(row["revision"]),
            )
        new_name = row["name"] if name is None else name
        new_category = row["category"] if category is None else category
        _validate_name_category(new_name, new_category)
        if new_name == row["name"] and new_category == row["category"]:
            c.rollback()
            return _detail(c, material_id)
        ts = now()
        c.execute(
            "UPDATE materials SET name=?,category=?,revision=revision+1,updated_at=? "
            "WHERE id=?",
            (new_name, new_category, ts, material_id),
        )
        c.commit()
        return _detail(c, material_id)
    except Exception:
        c.rollback()
        raise


def set_retirement(
    c: sqlite3.Connection,
    material_id: str,
    *,
    expected_revision: int,
    retired: bool,
) -> dict[str, object]:
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("material not found")
        if row["revision"] != expected_revision:
            raise ConflictError(
                "material revision conflict",
                code="material_revision_conflict",
                current_revision=int(row["revision"]),
            )
        target = "retired" if retired else "active"
        if row["status"] == target:
            c.rollback()
            return _detail(c, material_id)
        ts = now()
        c.execute(
            "UPDATE materials SET status=?,retired_at=?,revision=revision+1,"
            "updated_at=? "
            "WHERE id=?",
            (target, ts if retired else None, ts, material_id),
        )
        c.commit()
        return _detail(c, material_id)
    except Exception:
        c.rollback()
        raise
