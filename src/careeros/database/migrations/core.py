"""Ordered, fail-closed schema inspection and migrations."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from careeros.database.schema import SCHEMA_VERSION

MIGRATIONS: dict[int, object] = {}


def inspect_schema(connection: sqlite3.Connection) -> str:
    """Classify a database without creating or changing anything."""
    row = (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        if _table_exists(connection, "schema_metadata")
        else None
    )
    if row is None:
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        return "empty" if not tables else "malformed"
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        return "malformed"
    if version == SCHEMA_VERSION:
        return (
            "current" if _valid_structure(connection, SCHEMA_VERSION) else "malformed"
        )
    if version < 8:
        return "unsupported"
    if version > SCHEMA_VERSION:
        return "future"
    if version in (8, 9):
        if not _valid_structure(connection, version):
            return "malformed"
    return "upgrade_required"


def inspect_database(path: Path) -> str:
    """Inspect a database path without creating it or mutating its schema."""
    if not path.exists():
        return "absent"
    if path.stat().st_size == 0:
        return "empty"
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
            return inspect_schema(connection)
    except sqlite3.DatabaseError:
        return "malformed"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _valid_structure(connection: sqlite3.Connection, version: int) -> bool:
    required: dict[str, set[str]] = {
        "jobs": {"id", "source", "title", "collected_at"},
        "applications": {"id", "job_id", "current_status", "created_at", "updated_at"},
        "application_events": {
            "id",
            "application_id",
            "old_status",
            "new_status",
            "created_at",
        },
        "reminders": {"id", "job_id", "kind", "due_at"},
        "review_decisions": {"id", "job_id", "decision", "created_at", "updated_at"},
    }
    if version in (9, 10):
        required["application_events"].add("note_redacted_at")
        required["application_preparations"] = {
            "application_id",
            "revision",
            "requirements_reviewed_revision",
            "requirements_reviewed_at",
            "ready_confirmed_revision",
            "ready_confirmed_at",
            "created_at",
            "updated_at",
        }
        required["application_requirement_items"] = {
            "id",
            "application_id",
            "kind",
            "label",
            "detail",
            "required",
            "fulfillment_state",
            "deadline_at",
            "sort_order",
            "origin",
            "capture_method",
            "confirmed",
            "created_at",
            "updated_at",
        }
    if version == 10:
        required.update(
            {
                "material_store": {"singleton", "store_id", "created_at"},
                "materials": {
                    "id",
                    "name",
                    "category",
                    "revision",
                    "status",
                    "retired_at",
                    "created_at",
                    "updated_at",
                },
                "material_versions": {
                    "id",
                    "material_id",
                    "version_number",
                    "display_filename",
                    "declared_media_type",
                    "byte_size",
                    "sha256",
                    "storage_key",
                    "created_at",
                },
            }
        )
    for table, columns in required.items():
        if not _table_exists(connection, table):
            return False
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns <= actual:
            return False
    if version in (9, 10):
        if not _valid_v9_columns(connection) or not _valid_v9_sql(connection):
            return False
        if not _valid_v9_foreign_keys(connection):
            return False
        if not _valid_v9_indexes(connection):
            return False
    if version == 10:
        if not _valid_v10_tables(connection):
            return False
        if not _valid_v10_indexes(connection):
            return False
    return True


def _valid_v10_indexes(connection: sqlite3.Connection) -> bool:
    expected = {
        ("materials", "idx_materials_status_updated"): ["status", "updated_at", "id"],
        ("material_versions", "idx_material_versions_number"): [
            "material_id",
            "version_number",
        ],
        ("material_versions", "idx_material_versions_content"): [
            "material_id",
            "sha256",
        ],
        ("material_versions", "idx_material_versions_storage_key"): ["storage_key"],
        ("material_versions", "idx_material_versions_material"): [
            "material_id",
            "version_number",
        ],
    }
    unique = {
        "idx_material_versions_number": True,
        "idx_material_versions_content": True,
        "idx_material_versions_storage_key": True,
    }
    directions = {
        "idx_materials_status_updated": [0, 1, 0],
        "idx_material_versions_number": [0, 0],
        "idx_material_versions_content": [0, 0],
        "idx_material_versions_storage_key": [0],
        "idx_material_versions_material": [0, 1],
    }
    for (table, name), columns in expected.items():
        rows = list(connection.execute(f"PRAGMA index_list({table})"))
        row = next((item for item in rows if item[1] == name), None)
        if (
            row is None
            or int(row[2]) != int(unique.get(name, False))
            or row[3] != "c"
            or int(row[4]) != 0
        ):
            return False
        if [
            item[2] for item in connection.execute(f"PRAGMA index_info({name})")
        ] != columns:
            return False
        xinfo = list(connection.execute(f"PRAGMA index_xinfo({name})"))
        keyed = [item for item in xinfo if int(item[5]) == 1]
        if [int(item[3]) for item in keyed] != directions[name]:
            return False
        if [str(item[4]).upper() for item in keyed] != ["BINARY"] * len(columns):
            return False
    named = {
        row[1]
        for table in ("material_store", "materials", "material_versions")
        for row in connection.execute(f"PRAGMA index_list({table})")
        if not str(row[1]).startswith("sqlite_autoindex_")
    }
    if named != {name for _, name in expected}:
        return False
    fk = list(connection.execute("PRAGMA foreign_key_list(material_versions)"))
    return (
        len(fk) == 1
        and fk[0][2] == "materials"
        and fk[0][3] == "material_id"
        and fk[0][4] == "id"
        and fk[0][5].upper() == "NO ACTION"
        and fk[0][6].upper() == "RESTRICT"
    )


def _valid_v10_tables(connection: sqlite3.Connection) -> bool:
    def canonicalize(value: str) -> str:
        return "".join(value.lower().split())

    # Keep this comparison exact after whitespace-only normalization.  It is
    # deliberately stricter than checking required fragments: additive clauses,
    # collations, and altered operators must invalidate the schema.
    expected_sql = {
        "material_store": "createtablematerial_store(singletonintegerprimarykeycheck(singleton=1),store_idtextnotnulluniquecheck(length(store_id)=32andstore_id=lower(store_id)andstore_idnotglob'*[^0-9a-f]*'),created_attextnotnullcheck(length(created_at)>0))",  # noqa: E501
        "materials": "createtablematerials(idtextprimarykeycheck(length(id)=32andid=lower(id)andidnotglob'*[^0-9a-f]*'),nametextnotnullcheck(length(name)between1and160andname=trim(name)),categorytextnotnullcheck(categoryin('resume','cover_letter_base','portfolio','supporting_document','other')),revisionintegernotnulldefault1check(revision>=1),statustextnotnulldefault'active'check(statusin('active','retired')),retired_attext,created_attextnotnullcheck(length(created_at)>0),updated_attextnotnullcheck(length(updated_at)>0),check((status='active'andretired_atisnull)or(status='retired'andretired_atisnotnull)))",  # noqa: E501
        "material_versions": "createtablematerial_versions(idtextprimarykeycheck(length(id)=32andid=lower(id)andidnotglob'*[^0-9a-f]*'),material_idtextnotnullreferencesmaterials(id)ondeleterestrict,version_numberintegernotnullcheck(version_numberbetween1and100),display_filenametextnotnullcheck(length(display_filename)between1and255),declared_media_typetextnotnullcheck(length(declared_media_type)between3and127),byte_sizeintegernotnullcheck(byte_sizebetween1and26214400),sha256textnotnullcheck(length(sha256)=64andsha256=lower(sha256)andsha256notglob'*[^0-9a-f]*'),storage_keytextnotnullcheck(length(storage_key)=64andstorage_key=lower(storage_key)andstorage_keynotglob'*[^0-9a-f]*'),created_attextnotnullcheck(length(created_at)>0))",  # noqa: E501
    }
    if any(
        canonicalize(_normalized_sql(connection, "table", table) or "") != sql
        for table, sql in expected_sql.items()
    ):
        return False

    expected = {
        "material_store": [
            ("singleton", "INTEGER", 0, None, 1),
            ("store_id", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        ],
        "materials": [
            ("id", "TEXT", 0, None, 1),
            ("name", "TEXT", 1, None, 0),
            ("category", "TEXT", 1, None, 0),
            ("revision", "INTEGER", 1, "1", 0),
            ("status", "TEXT", 1, "'active'", 0),
            ("retired_at", "TEXT", 0, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
        ],
        "material_versions": [
            ("id", "TEXT", 0, None, 1),
            ("material_id", "TEXT", 1, None, 0),
            ("version_number", "INTEGER", 1, None, 0),
            ("display_filename", "TEXT", 1, None, 0),
            ("declared_media_type", "TEXT", 1, None, 0),
            ("byte_size", "INTEGER", 1, None, 0),
            ("sha256", "TEXT", 1, None, 0),
            ("storage_key", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        ],
    }
    for table, wanted in expected.items():
        actual = [
            (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        if actual != wanted:
            return False
    sql_fragments = {
        "material_store": (
            "singleton integer primary key check (singleton=1)",
            "store_id text not null unique check (length(store_id)=32",
            "store_id=lower(store_id)",
            "store_id not glob '*[^0-9a-f]*'",
            "created_at text not null check (length(created_at)>0)",
        ),
        "materials": (
            "id text primary key check (length(id)=32",
            "id=lower(id)",
            "id not glob '*[^0-9a-f]*'",
            "name text not null check (length(name) between 1 and 160",
            "name=trim(name)",
            "category text not null check (category in ("
            "'resume','cover_letter_base','portfolio','supporting_document','other'))",
            "revision integer not null default 1 check (revision>=1)",
            "status text not null default 'active' check (status in ("
            "'active','retired'))",
            "created_at text not null check (length(created_at)>0)",
            "updated_at text not null check (length(updated_at)>0)",
            "check ((status='active' and retired_at is null) or "
            "(status='retired' and retired_at is not null))",
        ),
        "material_versions": (
            "id text primary key check (length(id)=32",
            "id=lower(id)",
            "id not glob '*[^0-9a-f]*'",
            "material_id text not null references materials(id) on delete restrict",
            "version_number integer not null check (version_number between 1 and 100)",
            "display_filename text not null check (length(display_filename) "
            "between 1 and 255)",
            "declared_media_type text not null check (length(declared_media_type) "
            "between 3 and 127)",
            "byte_size integer not null check (byte_size between 1 and 26214400)",
            "sha256 text not null check (length(sha256)=64",
            "sha256=lower(sha256)",
            "sha256 not glob '*[^0-9a-f]*'",
            "storage_key text not null check (length(storage_key)=64",
            "storage_key=lower(storage_key)",
            "storage_key not glob '*[^0-9a-f]*'",
            "created_at text not null check (length(created_at)>0)",
        ),
    }

    def compact(value: str) -> str:
        value = re.sub(r"\s+", " ", value.lower().strip())
        value = re.sub(r"\s*([(),])\s*", r"\1", value)
        value = re.sub(r"\s*=\s*", "=", value)
        return re.sub(r"\s*(>=|<=|>|<)\s*", r"\1", value)

    # Constraint fragments above establish the approved clauses.  Also reject
    # additive boolean terms: substring checks alone would otherwise accept a
    # weakened definition such as ``...length(storage_key)=64 OR 1=1``.
    approved_boolean_counts = {
        "material_store": (2, 0),
        "materials": (6, 1),
        "material_versions": (10, 0),
    }
    for table, (and_count, or_count) in approved_boolean_counts.items():
        sql = compact(_normalized_sql(connection, "table", table) or "")
        if (
            len(re.findall(r"(?<![a-z])and(?![a-z])", sql)) != and_count
            or len(re.findall(r"(?<![a-z])or(?![a-z])", sql)) != or_count
        ):
            return False

    return all(
        all(
            compact(fragment)
            in compact(_normalized_sql(connection, "table", table) or "")
            for fragment in fragments
        )
        for table, fragments in sql_fragments.items()
    )


def _valid_v9_columns(connection: sqlite3.Connection) -> bool:
    """Validate column affinity and constraint flags for the preparation tables."""
    expected = {
        "application_preparations": {
            "application_id": ("INTEGER", 0, 1),
            "revision": ("INTEGER", 1, 0),
            "requirements_reviewed_revision": ("INTEGER", 0, 0),
            "requirements_reviewed_at": ("TEXT", 0, 0),
            "ready_confirmed_revision": ("INTEGER", 0, 0),
            "ready_confirmed_at": ("TEXT", 0, 0),
            "created_at": ("TEXT", 1, 0),
            "updated_at": ("TEXT", 1, 0),
        },
        "application_requirement_items": {
            "id": ("TEXT", 0, 1),
            "application_id": ("INTEGER", 1, 0),
            "kind": ("TEXT", 1, 0),
            "label": ("TEXT", 1, 0),
            "detail": ("TEXT", 0, 0),
            "required": ("INTEGER", 1, 0),
            "fulfillment_state": ("TEXT", 1, 0),
            "deadline_at": ("TEXT", 0, 0),
            "sort_order": ("INTEGER", 1, 0),
            "origin": ("TEXT", 1, 0),
            "capture_method": ("TEXT", 1, 0),
            "confirmed": ("INTEGER", 1, 0),
            "created_at": ("TEXT", 1, 0),
            "updated_at": ("TEXT", 1, 0),
        },
    }
    for table, table_columns in expected.items():
        actual = {
            row[1]: (str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if any(
            actual.get(column) != properties
            for column, properties in table_columns.items()
        ):
            return False

    event_column = next(
        (
            row
            for row in connection.execute("PRAGMA table_info(application_events)")
            if row[1] == "note_redacted_at"
        ),
        None,
    )
    return (
        event_column is not None
        and str(event_column[2]).upper() == "TEXT"
        and not event_column[3]
        and not event_column[5]
    )


def _normalized_sql(
    connection: sqlite3.Connection, object_type: str, name: str
) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    normalized = re.sub(r"\s+", " ", str(row[0]).strip()).lower()
    normalized = re.sub(r"\s*,\s*", ",", normalized)
    return re.sub(r"\s*=\s*", "=", normalized)


def _valid_v9_sql(connection: sqlite3.Connection) -> bool:
    preparation_sql = _normalized_sql(connection, "table", "application_preparations")
    items_sql = _normalized_sql(connection, "table", "application_requirement_items")
    if preparation_sql is None or items_sql is None:
        return False
    preparation_fragments = (
        "application_id integer primary key references applications(id) "
        "on delete cascade",
        "revision integer not null default 0 check (revision >=0)",
    )
    item_fragments = (
        "application_id integer not null references applications(id) on delete cascade",  # noqa: E501
        "kind text not null check (kind in "
        "('material','link','question','task','deadline','other'))",
        "label text not null check (length(label) between 1 and 160)",
        "detail text check (detail is null or length(detail) between 1 and 1000)",
        "required integer not null check (required in (0,1))",
        "fulfillment_state text not null check (fulfillment_state in "
        "('open','satisfied','not_applicable'))",
        "sort_order integer not null check (sort_order >=0)",
        "origin text not null check (origin='manual')",
        "capture_method text not null check (capture_method='manual')",
        "confirmed integer not null check (confirmed in (0,1))",
        "check (kind !='deadline' or fulfillment_state !='satisfied')",
        "unique(application_id,sort_order)",
    )
    return all(
        fragment in preparation_sql for fragment in preparation_fragments
    ) and all(fragment in items_sql for fragment in item_fragments)


def _valid_v9_foreign_keys(connection: sqlite3.Connection) -> bool:
    for table, source_column in (
        ("application_preparations", "application_id"),
        ("application_requirement_items", "application_id"),
    ):
        matches = [
            row
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            if row[2] == "applications" and row[3] == source_column and row[4] == "id"
        ]
        if len(matches) != 1 or matches[0][6].upper() != "CASCADE":
            return False
    return True


def _valid_v9_indexes(connection: sqlite3.Connection) -> bool:
    indexes = {
        row[1]: row
        for row in connection.execute(
            "PRAGMA index_list(application_requirement_items)"
        )
    }
    index = indexes.get("idx_requirement_items_application")
    if index is None or int(index[2]) != 0 or index[3] != "c" or int(index[4]) != 0:
        return False
    columns = [
        row[2]
        for row in connection.execute(
            "PRAGMA index_info(idx_requirement_items_application)"
        )
    ]
    if columns != ["application_id", "sort_order"]:
        return False
    index_sql = _normalized_sql(
        connection, "index", "idx_requirement_items_application"
    )
    if index_sql != (
        "create index idx_requirement_items_application on "
        "application_requirement_items(application_id,sort_order)"
    ):
        return False

    unique_indexes = []
    for row in connection.execute("PRAGMA index_list(application_requirement_items)"):
        if int(row[2]) == 1:
            unique_indexes.append(
                [info[2] for info in connection.execute(f"PRAGMA index_info({row[1]})")]
            )
    return ["application_id", "sort_order"] in unique_indexes


def _apply_8_to_9(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(application_events)")
    }
    if "note_redacted_at" not in columns:
        connection.execute(
            "ALTER TABLE application_events ADD COLUMN note_redacted_at TEXT"
        )
    statements = [
        """CREATE TABLE application_preparations (
                application_id INTEGER PRIMARY KEY REFERENCES applications(id)
                    ON DELETE CASCADE,
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
                requirements_reviewed_revision INTEGER,
                requirements_reviewed_at TEXT,
                ready_confirmed_revision INTEGER,
                ready_confirmed_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
        """CREATE TABLE application_requirement_items (
                id TEXT PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id)
                    ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN
                    ('material','link','question','task','deadline','other')),
                label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 160),
                detail TEXT CHECK (detail IS NULL OR length(detail) BETWEEN 1 AND 1000),
                required INTEGER NOT NULL CHECK (required IN (0,1)),
                fulfillment_state TEXT NOT NULL CHECK (fulfillment_state IN
                    ('open','satisfied','not_applicable')),
                deadline_at TEXT, sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
                origin TEXT NOT NULL CHECK (origin='manual'), capture_method TEXT
                    NOT NULL
                    CHECK (capture_method='manual'),
                confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK (kind != 'deadline' OR fulfillment_state != 'satisfied'),
                UNIQUE(application_id, sort_order)
            )""",
        "CREATE INDEX idx_requirement_items_application ON "
        "application_requirement_items(application_id, sort_order)",
    ]
    for statement in statements:
        connection.execute(statement)


def migrate_8_to_9(connection: sqlite3.Connection) -> None:
    """Upgrade a structurally valid v8 database, transactionally."""
    if inspect_schema(connection) != "upgrade_required":
        raise ValueError("database is not a structurally valid v8 database")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _apply_8_to_9(connection)
        connection.execute(
            "UPDATE schema_metadata SET value='9' WHERE key='schema_version'"
        )
        if inspect_schema(connection) != "upgrade_required":
            raise ValueError("migration produced invalid v9 schema")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


MIGRATIONS[8] = migrate_8_to_9


def _apply_9_to_10(connection: sqlite3.Connection) -> None:
    """Add the append-only private materials tables to a v9 database."""
    material_tables = ("material_store", "materials", "material_versions")
    present = [_table_exists(connection, table) for table in material_tables]
    if any(present):
        raise ValueError("database contains pre-existing v10 material objects")
    statements = [
        """CREATE TABLE material_store (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            store_id TEXT NOT NULL UNIQUE CHECK (
                length(store_id)=32 AND store_id=lower(store_id)
                AND store_id NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL CHECK (length(created_at)>0)
        )""",
        """CREATE TABLE materials (
            id TEXT PRIMARY KEY CHECK (
                length(id)=32 AND id=lower(id)
                AND id NOT GLOB '*[^0-9a-f]*'
            ),
            name TEXT NOT NULL CHECK (
                length(name) BETWEEN 1 AND 160 AND name=trim(name)
            ),
            category TEXT NOT NULL CHECK (category IN (
                'resume','cover_letter_base','portfolio',
                'supporting_document','other'
            )),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
            status TEXT NOT NULL DEFAULT 'active' CHECK (
                status IN ('active','retired')
            ),
            retired_at TEXT, created_at TEXT NOT NULL CHECK (length(created_at)>0),
            updated_at TEXT NOT NULL CHECK (length(updated_at)>0),
            CHECK ((status='active' AND retired_at IS NULL)
                OR (status='retired' AND retired_at IS NOT NULL))
        )""",
        """CREATE INDEX idx_materials_status_updated ON materials(
            status, updated_at DESC, id
        )""",
        """CREATE TABLE material_versions (
            id TEXT PRIMARY KEY CHECK (
                length(id)=32 AND id=lower(id)
                AND id NOT GLOB '*[^0-9a-f]*'
            ),
            material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
            version_number INTEGER NOT NULL CHECK (version_number BETWEEN 1 AND 100),
            display_filename TEXT NOT NULL CHECK (
                length(display_filename) BETWEEN 1 AND 255
            ),
            declared_media_type TEXT NOT NULL CHECK (
                length(declared_media_type) BETWEEN 3 AND 127
            ),
            byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 26214400),
            sha256 TEXT NOT NULL CHECK (
                length(sha256)=64 AND sha256=lower(sha256)
                AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            storage_key TEXT NOT NULL CHECK (
                length(storage_key)=64 AND storage_key=lower(storage_key)
                AND storage_key NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL CHECK (length(created_at)>0)
        )""",
        "CREATE UNIQUE INDEX idx_material_versions_number ON "
        "material_versions(material_id, version_number)",
        "CREATE UNIQUE INDEX idx_material_versions_content ON "
        "material_versions(material_id, sha256)",
        "CREATE UNIQUE INDEX idx_material_versions_storage_key ON "
        "material_versions(storage_key)",
        "CREATE INDEX idx_material_versions_material ON "
        "material_versions(material_id, version_number DESC)",
    ]
    for statement in statements:
        connection.execute(statement)


def migrate_9_to_10(connection: sqlite3.Connection) -> None:
    """Upgrade a structurally valid v9 database, transactionally."""
    if inspect_schema(connection) != "upgrade_required":
        raise ValueError("database is not a structurally valid v9 database")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _apply_9_to_10(connection)
        connection.execute(
            "UPDATE schema_metadata SET value='10' WHERE key='schema_version'"
        )
        if inspect_schema(connection) != "current":
            raise ValueError("migration produced invalid v10 schema")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


MIGRATIONS[9] = migrate_9_to_10


def migrate(connection: sqlite3.Connection) -> str:
    state = inspect_schema(connection)
    if state == "upgrade_required":
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = int(
                connection.execute(
                    "SELECT value FROM schema_metadata WHERE key='schema_version'"
                ).fetchone()[0]
            )
            while version < SCHEMA_VERSION:
                if version not in MIGRATIONS:
                    raise ValueError(f"unsupported database schema (v{version})")
                if version == 8:
                    _apply_8_to_9(connection)
                else:
                    _apply_9_to_10(connection)
                version += 1
                if version == SCHEMA_VERSION:
                    connection.execute(
                        "UPDATE schema_metadata SET value=? WHERE key='schema_version'",
                        (str(version),),
                    )
            if inspect_schema(connection) != "current":
                raise ValueError("migration produced invalid schema")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return "current"
    if state in {"future", "malformed", "unsupported"}:
        raise ValueError(f"unsupported database schema ({state})")
    return state
