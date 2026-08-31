from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import stat
import sys
from pathlib import Path

from careeros.database.admin import _check
from careeros.database.connection import connect_current_rw, connect_read_only
from careeros.database.materials import list_material_rows
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.materials.audit import audit_database
from careeros.materials.models import MaterialError
from careeros.materials.service import (
    add_version,
    create_material,
    get_material,
    preflight_binding,
    set_retirement,
    update_material,
    validate_metadata,
)
from careeros.materials.storage import (
    open_bound_object,
    remove_stage,
    stage_source,
)
from careeros.settings import materials_root


def run_material_command(args: argparse.Namespace) -> int:
    root = (
        Path(args.materials_root)
        if getattr(args, "materials_root", None)
        else materials_root()
    )
    db = args.database
    try:
        if not db.is_file():
            raise ValueError("database is absent or uninitialized")
        if args.material_command == "list":
            if _check(db) != "current":
                raise ValueError("database schema is not current")
            with connect_read_only(db) as c:
                rows = list_material_rows(
                    c, include_retired=args.include_retired, limit=args.limit
                )
                for row in rows:
                    print(f"{row['id']} {row['name']} ({row['category']})")
            return 0
        if args.material_command == "show":
            if _check(db) != "current":
                raise ValueError("database schema is not current")
            with connect_read_only(db) as c:
                print(get_material(c, args.material_id))
            return 0
        if args.material_command == "audit":
            code, message, counts = audit_database(db, root)
            if message:
                print(message, file=sys.stderr)
            elif code in (0, 1):
                print(
                    "Material audit: "
                    + ("coherent" if code == 0 else "inconsistent")
                    + ". "
                    + ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
                )
                if code == 1:
                    print(
                        "Review the managed Materials store and retry.", file=sys.stderr
                    )
            return int(code)
        if args.material_command == "export-version":
            # Keep the read-only current connection alive for the complete
            # metadata/binding admission and byte export operation.
            with connect_read_only(db) as c:
                row = c.execute(
                    "SELECT byte_size,sha256,storage_key "
                    "FROM material_versions WHERE id=?",
                    (args.version_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("material version not found")
                store = c.execute(
                    "SELECT store_id FROM material_store WHERE singleton=1"
                ).fetchone()
                if store is None:
                    raise ValueError("material content unavailable")
                sfd = open_bound_object(root, str(store[0]), str(row["storage_key"]))
                try:
                    st = os.fstat(sfd)
                    if (
                        not stat.S_ISREG(st.st_mode)
                        or st.st_uid != os.getuid()
                        or stat.S_IMODE(st.st_mode) != 0o600
                        or st.st_nlink != 1
                        or st.st_size != row["byte_size"]
                    ):
                        raise ValueError("material content unavailable")
                    destination_name = args.destination.name
                    if destination_name in ("", ".", ".."):
                        raise ValueError("invalid export destination")
                    pfd = os.open(
                        args.destination.parent,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    )
                    dfd = -1
                    created = False
                    completed = False
                    try:
                        dfd = os.open(
                            destination_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=pfd,
                        )
                        created = True
                        try:
                            export_digest = hashlib.sha256()
                            while chunk := os.read(sfd, 1024 * 1024):
                                view = memoryview(chunk)
                                while view:
                                    written = os.write(dfd, view)
                                    view = view[written:]
                                export_digest.update(chunk)
                            post = os.fstat(sfd)
                            if export_digest.hexdigest() != row["sha256"] or (
                                st.st_dev,
                                st.st_ino,
                                st.st_size,
                                st.st_mode,
                                st.st_uid,
                                st.st_nlink,
                                st.st_mtime_ns,
                                st.st_ctime_ns,
                            ) != (
                                post.st_dev,
                                post.st_ino,
                                post.st_size,
                                post.st_mode,
                                post.st_uid,
                                post.st_nlink,
                                post.st_mtime_ns,
                                post.st_ctime_ns,
                            ):
                                raise ValueError("material content unavailable")
                            os.fchmod(dfd, 0o600)
                            final = os.fstat(dfd)
                            if (
                                not stat.S_ISREG(final.st_mode)
                                or stat.S_IMODE(final.st_mode) != 0o600
                                or final.st_nlink != 1
                                or final.st_size != row["byte_size"]
                            ):
                                raise ValueError("material content unavailable")
                            os.fsync(dfd)
                        except (OSError, ValueError):
                            raise
                        os.close(dfd)
                        dfd = -1
                        os.fsync(pfd)
                        completed = True
                    finally:
                        if dfd >= 0:
                            os.close(dfd)
                        if created and not completed:
                            try:
                                os.unlink(destination_name, dir_fd=pfd)
                            except FileNotFoundError:
                                pass
                            except OSError:
                                # Cleanup is best-effort and must never hide
                                # the export failure that caused it.
                                pass
                            else:
                                try:
                                    os.fsync(pfd)
                                except OSError:
                                    # The destination is already unlinked;
                                    # preserve the original operation error.
                                    pass
                        os.close(pfd)
                finally:
                    os.close(sfd)
                print(f"Material version exported: {args.destination}")
                return 0
        if args.material_command in ("import", "add-version"):
            if args.material_command == "import":
                validate_metadata(
                    args.name,
                    args.category,
                    args.display_filename or args.source.name,
                    args.media_type,
                )
            else:
                validate_metadata(
                    "x",
                    "resume",
                    args.display_filename or args.source.name,
                    args.media_type,
                )
            # Admission must not create a root or staging file.  This also
            # ensures a bound database fails closed when its root is absent.
            with connect_read_only(db) as preflight:
                preflight_binding(preflight, root)
            # The frozen upload order stages and hashes outside the collection
            # lock. Finalization alone is serialized and revalidates the
            # current database/root binding under that lock.
            stage, size, digest = stage_source(root, args.source)
            try:
                with collection_lock(db, command="material"):
                    with connect_current_rw(db) as c:
                        if args.material_command == "import":
                            result = create_material(
                                c,
                                root,
                                name=args.name,
                                category=args.category,
                                filename=args.display_filename or args.source.name,
                                media_type=args.media_type,
                                stage=stage,
                                size=size,
                                sha256=digest,
                            )
                        else:
                            result, status = add_version(
                                c,
                                root,
                                args.material_id,
                                expected_revision=args.expected_revision,
                                filename=args.display_filename or args.source.name,
                                media_type=args.media_type,
                                stage=stage,
                                size=size,
                                sha256=digest,
                            )
            except BaseException:
                try:
                    remove_stage(stage)
                except (OSError, MaterialError):
                    pass
                raise
            print(result)
            return 0
        with collection_lock(db, command="material"):
            c = connect_current_rw(db)
            try:
                if args.material_command == "update":
                    if args.name is None and args.category is None:
                        raise ValueError("material update requires a field")
                    result = update_material(
                        c,
                        args.material_id,
                        expected_revision=args.expected_revision,
                        name=args.name,
                        category=args.category,
                    )
                else:
                    result = set_retirement(
                        c,
                        args.material_id,
                        expected_revision=args.expected_revision,
                        retired=args.material_command == "retire",
                    )
            finally:
                c.close()
        print(result)
        return 0
    except CollectionLockConflict:
        return 75
    except (OSError, sqlite3.Error, ValueError, MaterialError):
        print("Material failed: operation could not be completed", file=sys.stderr)
        return 1
