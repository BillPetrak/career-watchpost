"""Stable, privacy-safe managed-material audit."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from careeros.locking import CollectionLockConflict, collection_lock
from careeros.materials.storage import MARKER, _inside_git_checkout

STAGING_RE = re.compile(r"^[0-9a-f]{64}\.part$")
OBJECT_RE = re.compile(r"([0-9a-f]{64})\.blob")
MARKER_RE = re.compile(rb"careeros-material-store-v1:([0-9a-f]{32})\n")
FIXED_MESSAGE = "Material audit could not certify a stable snapshot; retry later."

ZERO_COUNTS = {
    "missing": 0,
    "unexpected": 0,
    "invalid": 0,
    "stale_staging": 0,
}

StatTuple = tuple[int, int, int, int, int, int, int, int]
IdentityTuple = tuple[int, int, int, int, int]
Inventory = tuple[tuple[str, StatTuple | None], ...]


class _SnapshotChanged(Exception):
    """A path or marker changed while one snapshot was being captured."""


class _InvalidSchema(Exception):
    """The database is not an exact current-v12 database."""


def _value(value: Any) -> bytes:
    if value is None:
        return b"\0"
    if isinstance(value, int):
        return b"\1" + struct.pack(">q", value)
    if isinstance(value, str):
        raw = value.encode()
        return b"\2" + struct.pack(">Q", len(raw)) + raw
    raise TypeError("unsupported fingerprint value")


def fingerprint(connection: sqlite3.Connection) -> str:
    """Return the frozen canonical fingerprint without exposing its inputs."""
    digest = hashlib.sha256()
    digest.update(b"careeros-material-audit-v1\0")
    schema_version = int(
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
    )
    digest.update(_value(schema_version))
    specs = (
        ("material_store", "singleton", ("singleton", "store_id", "created_at")),
        (
            "materials",
            "id COLLATE BINARY",
            (
                "id",
                "name",
                "category",
                "revision",
                "status",
                "retired_at",
                "created_at",
                "updated_at",
            ),
        ),
        (
            "material_versions",
            "material_id COLLATE BINARY,version_number,id COLLATE BINARY",
            (
                "id",
                "material_id",
                "version_number",
                "display_filename",
                "declared_media_type",
                "byte_size",
                "sha256",
                "storage_key",
                "created_at",
            ),
        ),
    )
    for table, order, columns in specs:
        rows = connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"
        ).fetchall()
        raw = table.encode()
        digest.update(struct.pack(">I", len(raw)) + raw + struct.pack(">Q", len(rows)))
        for row in rows:
            for value in row:
                digest.update(_value(value))
    return digest.hexdigest()


@dataclass(frozen=True)
class MarkerSnapshot:
    state: Literal["absent", "valid", "invalid"]
    identity: StatTuple | None = None
    store_id: str | None = None


@dataclass(frozen=True)
class RootSnapshot:
    state: Literal["absent", "safe", "unsafe"]
    # Every existing configured-path component participates. Parent directory
    # mtimes do not: unrelated files beside the root must not destabilize audit.
    path_identity: tuple[IdentityTuple, ...]
    missing_depth: int | None = None
    marker: MarkerSnapshot = MarkerSnapshot("absent")


@dataclass
class _RootHandle:
    snapshot: RootSnapshot
    fd: int | None


@dataclass(frozen=True)
class _AuthoritySnapshot:
    fingerprint: str
    row_counts: tuple[int, int, int]
    root: RootSnapshot


@dataclass(frozen=True)
class AuditAttempt:
    unstable: bool
    coherent: bool
    counts: dict[str, int]


def _tuple(st: os.stat_result) -> StatTuple:
    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mode,
        st.st_uid,
        st.st_nlink,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _identity(st: os.stat_result) -> IdentityTuple:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink)


def _marker_snapshot(rfd: int) -> MarkerSnapshot:
    """Distinguish an absent marker from every unsafe or malformed marker."""
    try:
        lst = os.stat(MARKER, dir_fd=rfd, follow_symlinks=False)
    except FileNotFoundError:
        return MarkerSnapshot("absent")
    except OSError:
        return MarkerSnapshot("invalid")
    before = _tuple(lst)
    if not stat.S_ISREG(lst.st_mode):
        return MarkerSnapshot("invalid", before)
    try:
        fd = os.open(
            MARKER,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=rfd,
        )
    except OSError:
        return MarkerSnapshot("invalid", before)
    try:
        opened = os.fstat(fd)
        if _tuple(opened) != before:
            raise _SnapshotChanged
        raw = b""
        while chunk := os.read(fd, 4096):
            raw += chunk
            if len(raw) > 128:
                break
        after = os.fstat(fd)
        if _tuple(opened) != _tuple(after):
            raise _SnapshotChanged
        match = MARKER_RE.fullmatch(raw)
        if (
            match is None
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            return MarkerSnapshot("invalid", _tuple(after))
        return MarkerSnapshot("valid", _tuple(after), match.group(1).decode())
    finally:
        os.close(fd)


def _open_configured_root(root: Path) -> _RootHandle:
    """Descriptor-walk the absolute configured path without following links."""
    absolute = Path(os.path.abspath(root))
    inside_git = _inside_git_checkout(absolute)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identities: list[IdentityTuple] = [_identity(os.fstat(fd))]
    components = absolute.parts[1:]
    try:
        for depth, component in enumerate(components, start=1):
            try:
                lst = os.stat(component, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.close(fd)
                return _RootHandle(
                    RootSnapshot(
                        "unsafe" if inside_git else "absent",
                        tuple(identities),
                        depth,
                    ),
                    None,
                )
            except OSError:
                os.close(fd)
                return _RootHandle(
                    RootSnapshot("unsafe", tuple(identities), depth), None
                )
            identities.append(_identity(lst))
            if not stat.S_ISDIR(lst.st_mode):
                os.close(fd)
                return _RootHandle(RootSnapshot("unsafe", tuple(identities)), None)
            try:
                nxt = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except OSError:
                os.close(fd)
                return _RootHandle(RootSnapshot("unsafe", tuple(identities)), None)
            if _identity(os.fstat(nxt)) != _identity(lst):
                os.close(nxt)
                raise _SnapshotChanged
            os.close(fd)
            fd = nxt
        root_stat = os.fstat(fd)
        safe = (
            stat.S_ISDIR(root_stat.st_mode)
            and root_stat.st_uid == os.getuid()
            and stat.S_IMODE(root_stat.st_mode) == 0o700
            and not inside_git
        )
        marker = _marker_snapshot(fd)
        return _RootHandle(
            RootSnapshot(
                "safe" if safe else "unsafe", tuple(identities), marker=marker
            ),
            fd,
        )
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _inventory(rfd: int) -> Inventory:
    """Capture the complete managed entry/type/metadata inventory."""
    entries: list[tuple[str, StatTuple | None]] = []
    try:
        root_names = os.listdir(rfd)
    except OSError:
        return (("<root>", None),)
    for name in root_names:
        try:
            entries.append(
                (name, _tuple(os.stat(name, dir_fd=rfd, follow_symlinks=False)))
            )
        except OSError:
            entries.append((name, None))
    for top in ("objects", ".staging"):
        try:
            top_fd = os.open(
                top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rfd
            )
        except OSError:
            continue
        stack = [(top, top_fd)]
        try:
            while stack:
                prefix, current = stack.pop()
                try:
                    names = os.listdir(current)
                except OSError:
                    entries.append((f"{prefix}/<unreadable>", None))
                    os.close(current)
                    continue
                for name in names:
                    path = f"{prefix}/{name}"
                    try:
                        item = os.stat(name, dir_fd=current, follow_symlinks=False)
                    except OSError:
                        entries.append((path, None))
                        continue
                    entries.append((path, _tuple(item)))
                    if stat.S_ISDIR(item.st_mode):
                        try:
                            child = os.open(
                                name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=current,
                            )
                        except OSError:
                            continue
                        stack.append((path, child))
                os.close(current)
        except BaseException:
            for _, current in stack:
                try:
                    os.close(current)
                except OSError:
                    pass
            raise
    return tuple(sorted(entries))


def _expected(connection: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    return {
        str(row["storage_key"]): (int(row["byte_size"]), str(row["sha256"]))
        for row in connection.execute(
            "SELECT storage_key,byte_size,sha256 FROM material_versions"
        )
    }


def _scan_staging(rfd: int, as_of: float) -> tuple[int, int]:
    invalid = 0
    stale = 0
    try:
        sfd = os.open(
            ".staging", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rfd
        )
    except FileNotFoundError:
        return invalid, stale
    except OSError:
        return 1, stale
    try:
        for name in os.listdir(sfd):
            try:
                item = os.stat(name, dir_fd=sfd, follow_symlinks=False)
            except OSError:
                invalid += 1
                continue
            valid = (
                bool(STAGING_RE.fullmatch(name))
                and stat.S_ISREG(item.st_mode)
                and item.st_uid == os.getuid()
                and stat.S_IMODE(item.st_mode) == 0o600
                and item.st_nlink == 1
            )
            if not valid:
                invalid += 1
            elif as_of - item.st_mtime > 86400:
                stale += 1
    finally:
        os.close(sfd)
    return invalid, stale


def _open_expected_object(rfd: int, key: str) -> tuple[int | None, StatTuple | None]:
    descriptors: list[int] = []
    current = rfd
    try:
        for component in ("objects", key[:2], key[2:4]):
            try:
                current = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except OSError:
                return None, None
            descriptors.append(current)
        name = f"{key}.blob"
        try:
            listed = os.stat(name, dir_fd=current, follow_symlinks=False)
        except OSError:
            return None, None
        try:
            result = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=current,
            )
        except OSError:
            return None, _tuple(listed)
        if _tuple(os.fstat(result)) != _tuple(listed):
            os.close(result)
            raise _SnapshotChanged
        return result, _tuple(listed)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _scan_objects(
    rfd: int, expected: dict[str, tuple[int, str]], counts: dict[str, int]
) -> dict[str, StatTuple]:
    seen: set[str] = set()
    final_stats: dict[str, StatTuple] = {}
    inventory = _inventory(rfd)
    for path, item in inventory:
        if not path.startswith("objects/"):
            continue
        if item is None:
            counts["invalid"] += 1
            continue
        parts = path.split("/")
        if len(parts) == 2:
            valid = bool(re.fullmatch(r"[0-9a-f]{2}", parts[1])) and stat.S_ISDIR(
                item[3]
            )
            if not valid or item[4] != os.getuid() or stat.S_IMODE(item[3]) != 0o700:
                counts["invalid"] += 1
        elif len(parts) == 3:
            valid = (
                bool(re.fullmatch(r"[0-9a-f]{2}", parts[1]))
                and bool(re.fullmatch(r"[0-9a-f]{2}", parts[2]))
                and stat.S_ISDIR(item[3])
            )
            if not valid or item[4] != os.getuid() or stat.S_IMODE(item[3]) != 0o700:
                counts["invalid"] += 1
        elif len(parts) == 4:
            match = OBJECT_RE.fullmatch(parts[3])
            if (
                match is None
                or match.group(1)[:2] != parts[1]
                or match.group(1)[2:4] != parts[2]
            ):
                counts["invalid"] += 1
                continue
            key = match.group(1)
            seen.add(key)
            if key not in expected:
                counts["unexpected"] += 1
                continue
            fd, before = _open_expected_object(rfd, key)
            if fd is None:
                if before is None:
                    counts["missing"] += 1
                else:
                    counts["invalid"] += 1
                    final_stats[key] = before
                continue
            try:
                opened = os.fstat(fd)
                invalid_object = not (
                    stat.S_ISREG(opened.st_mode)
                    and opened.st_uid == os.getuid()
                    and stat.S_IMODE(opened.st_mode) == 0o600
                    and opened.st_nlink == 1
                    and opened.st_size == expected[key][0]
                )
                # Hash every safely opened regular expected object even when
                # metadata is already invalid; audit is the complete byte scan.
                if stat.S_ISREG(opened.st_mode):
                    invalid_object = _hash_fd(fd) != expected[key][1] or invalid_object
                if invalid_object:
                    counts["invalid"] += 1
                after = os.fstat(fd)
                if _tuple(opened) != _tuple(after):
                    raise _SnapshotChanged
                final_stats[key] = _tuple(after)
            finally:
                os.close(fd)
        else:
            counts["invalid"] += 1
    for _key in set(expected) - seen:
        counts["missing"] += 1
    return final_stats


def _restat_expected(rfd: int, key: str) -> StatTuple | None:
    fd, observed = _open_expected_object(rfd, key)
    if fd is None:
        return observed
    try:
        return _tuple(os.fstat(fd))
    finally:
        os.close(fd)


def audit_attempt(
    connection: sqlite3.Connection,
    root: Path,
    *,
    as_of: float | None = None,
    root_fd: int | None = None,
    root_snapshot: RootSnapshot | None = None,
) -> AuditAttempt:
    """Perform the unlocked byte scan and metadata-only stability sweep."""
    as_of = datetime.now(UTC).timestamp() if as_of is None else as_of
    counts = ZERO_COUNTS.copy()
    expected = _expected(connection)
    owned = root_fd is None
    handle: _RootHandle | None = None
    if root_fd is None:
        handle = _open_configured_root(root)
        root_fd = handle.fd
        root_snapshot = handle.snapshot
    if root_snapshot is None:
        raise ValueError("root snapshot required")
    try:
        if root_snapshot.state == "absent":
            counts["missing"] = len(expected)
            store = connection.execute(
                "SELECT store_id FROM material_store WHERE singleton=1"
            ).fetchone()
            if store is not None:
                counts["invalid"] += 1
            return AuditAttempt(False, not any(counts.values()), counts)
        if root_snapshot.state == "unsafe" or root_fd is None:
            counts["invalid"] += 1
            return AuditAttempt(False, False, counts)

        before_inventory = _inventory(root_fd)
        root_names = {path for path, _ in before_inventory if "/" not in path}
        extras = root_names - {MARKER, "objects", ".staging"}
        counts["invalid"] += len(extras)
        store = connection.execute(
            "SELECT store_id FROM material_store WHERE singleton=1"
        ).fetchone()
        marker = root_snapshot.marker
        if marker.state == "invalid":
            counts["invalid"] += 1
        elif store is None:
            # Marker-only state B, an absent marker, and a safely empty root are
            # all coherent while no committed object exists.
            pass
        elif marker.state != "valid" or marker.store_id != str(store["store_id"]):
            counts["invalid"] += 1

        for required in ("objects", ".staging"):
            required_items = [
                item for path, item in before_inventory if path == required
            ]
            required_item = required_items[0] if required_items else None
            if required_item is None:
                if store is not None:
                    counts["invalid"] += 1
            elif (
                not stat.S_ISDIR(required_item[3])
                or required_item[4] != os.getuid()
                or stat.S_IMODE(required_item[3]) != 0o700
            ):
                counts["invalid"] += 1

        final_stats = _scan_objects(root_fd, expected, counts)
        stage_counts = _scan_staging(root_fd, as_of)
        counts["invalid"] += stage_counts[0]
        counts["stale_staging"] += stage_counts[1]

        # Frozen unlocked metadata-only stability sweep.
        after_inventory = _inventory(root_fd)
        if before_inventory != after_inventory:
            return AuditAttempt(True, False, ZERO_COUNTS.copy())
        if _scan_staging(root_fd, as_of) != stage_counts:
            return AuditAttempt(True, False, ZERO_COUNTS.copy())
        for key, expected_stat in final_stats.items():
            if _restat_expected(root_fd, key) != expected_stat:
                return AuditAttempt(True, False, ZERO_COUNTS.copy())

        if store is None and any(
            path.count("/") == 3 and OBJECT_RE.fullmatch(path.rsplit("/", 1)[1])
            for path, _ in before_inventory
            if path.startswith("objects/")
        ):
            # State C is independently inconsistent even when every blob is
            # already counted as an orphan.
            counts["invalid"] += 1
        return AuditAttempt(False, not any(counts.values()), counts)
    except _SnapshotChanged:
        return AuditAttempt(True, False, ZERO_COUNTS.copy())
    finally:
        if owned and handle is not None and handle.fd is not None:
            os.close(handle.fd)


def audit(connection: sqlite3.Connection, root: Path) -> tuple[bool, dict[str, int]]:
    result = audit_attempt(connection, root)
    return result.coherent, result.counts


def _counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    values = tuple(
        int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in ("material_store", "materials", "material_versions")
    )
    return values[0], values[1], values[2]


def _capture_authority(
    database: Path, root: Path
) -> tuple[_AuthoritySnapshot, int | None]:
    from careeros.database.migrations.core import inspect_schema

    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if inspect_schema(connection) != "current":
            raise _InvalidSchema
        db_fingerprint = fingerprint(connection)
        row_counts = _counts(connection)
    handle = _open_configured_root(root)
    return (
        _AuthoritySnapshot(db_fingerprint, row_counts, handle.snapshot),
        handle.fd,
    )


def audit_database(
    database: Path, root: Path
) -> tuple[int, str | None, dict[str, int]]:
    """Certify one stable snapshot using exactly two short lock windows."""
    for _attempt in range(2):
        root_fd: int | None = None
        try:
            try:
                with collection_lock(database, command="material audit"):
                    before, root_fd = _capture_authority(database, root)
            except CollectionLockConflict:
                return 75, None, ZERO_COUNTS.copy()
            except _InvalidSchema:
                return 1, None, ZERO_COUNTS.copy()
            except _SnapshotChanged:
                continue
            except (OSError, sqlite3.Error, ValueError):
                return 1, None, ZERO_COUNTS.copy()

            try:
                try:
                    with sqlite3.connect(
                        f"file:{database.resolve()}?mode=ro", uri=True
                    ) as scan:
                        scan.row_factory = sqlite3.Row
                        result = audit_attempt(
                            scan,
                            root,
                            root_fd=root_fd,
                            root_snapshot=before.root,
                        )
                except (OSError, sqlite3.Error, ValueError):
                    result = AuditAttempt(True, False, ZERO_COUNTS.copy())
                try:
                    with collection_lock(database, command="material audit"):
                        after, after_fd = _capture_authority(database, root)
                        if after_fd is not None:
                            os.close(after_fd)
                except CollectionLockConflict:
                    return 75, None, ZERO_COUNTS.copy()
                except (_InvalidSchema, _SnapshotChanged):
                    continue
                except (OSError, sqlite3.Error, ValueError):
                    return 1, None, ZERO_COUNTS.copy()
                if result.unstable or before != after:
                    continue
                return (0 if result.coherent else 1), None, result.counts
            finally:
                if root_fd is not None:
                    os.close(root_fd)
        finally:
            # Kept explicit so every loop/return path is visibly descriptor-safe.
            pass
    return 75, FIXED_MESSAGE, ZERO_COUNTS.copy()
