"""Ordered, fail-closed schema inspection and migrations."""

# Migration SQL is normative and intentionally mirrors the bootstrap DDL.
# ruff: noqa: E501

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from careeros.database.schema import SCHEMA_VERSION, V13_DDL, V14_DDL

MigrationStep = Callable[[sqlite3.Connection], None]
MIGRATIONS: dict[int, MigrationStep] = {}


def _valid_v14_structure(connection: sqlite3.Connection) -> bool:
    """Require the complete additive v14 layout, exactly as bootstrapped."""
    from careeros.database.schema import initialize_schema

    tables = (
        "review_ranking_authority",
        "review_ranking_generations",
        "review_ranking_items",
        "review_ranking_current",
    )
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        initialize_schema(reference)
        for table in tables:
            if not _table_exists(connection, table):
                return False
            if (
                _canonical_master_sql(connection, "table", table)
                != _canonical_master_sql(reference, "table", table)
                or _pragma_rows(connection, "table_info", table)
                != _pragma_rows(reference, "table_info", table)
                or _pragma_rows(connection, "foreign_key_list", table)
                != _pragma_rows(reference, "foreign_key_list", table)
            ):
                return False
            actual_indexes = {
                str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
                for row in connection.execute(f"PRAGMA index_list({table})")
            }
            expected_indexes = {
                str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
                for row in reference.execute(f"PRAGMA index_list({table})")
            }
            if actual_indexes != expected_indexes:
                return False
            for index in actual_indexes:
                if _canonical_master_sql(
                    connection, "index", index
                ) != _canonical_master_sql(reference, "index", index) or _pragma_rows(
                    connection, "index_xinfo", index
                ) != _pragma_rows(reference, "index_xinfo", index):
                    return False

        expected_objects = set(tables)
        expected_objects.update(
            str(row[1])
            for table in tables
            for row in reference.execute(f"PRAGMA index_list({table})")
            if not str(row[1]).startswith("sqlite_autoindex_")
        )
        actual_objects = {
            str(row[1])
            for row in connection.execute(
                "SELECT type,name,tbl_name FROM sqlite_master "
                "WHERE type IN ('table','index','trigger')"
            )
            if (str(row[1]).startswith("review_ranking_") or str(row[2]) in tables)
            and not str(row[1]).startswith("sqlite_autoindex_")
        }
        return actual_objects == expected_objects
    finally:
        reference.close()


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
    if version in (8, 9, 10, 11, 12, 13):
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
        "review_events": {
            "id",
            "job_id",
            "old_decision",
            "new_decision",
            "note",
            "created_at",
        },
    }
    if version in (9, 10, 11, 12, 13):
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
    if version in (10, 11, 12, 13, 14):
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
    if version in (9, 10, 11, 12, 13, 14):
        if not _valid_v9_columns(connection) or not _valid_v9_sql(connection):
            return False
        if not _valid_v9_foreign_keys(connection):
            return False
        if not _valid_v9_indexes(connection):
            return False
    if version in (10, 11, 12, 13, 14):
        if not _valid_v10_tables(connection):
            return False
        if not _valid_v10_indexes(connection):
            return False
    if version in (11, 12, 13, 14) and not _valid_v11_structure(connection):
        return False
    if version in (11, 12, 13, 14) and (
        not _valid_v9_columns(connection)
        or not _valid_v9_sql(connection)
        or not _valid_v9_foreign_keys(connection)
        or not _valid_v9_indexes(connection)
        or not _valid_v10_tables(connection)
        or not _valid_v10_indexes(connection)
    ):
        return False
    if version == 11 and _preference_objects(connection):
        return False
    if version in (12, 13, 14) and not _valid_v12_structure(
        connection, allow_ranking_columns=version == 14
    ):
        return False
    if version == 12 and any(
        str(row[1]).startswith(("resume_", "career_"))
        for row in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index','trigger')"
        )
    ):
        return False
    if version == 12 and any(
        str(row[0]) in V13_TABLES
        for row in connection.execute(
            "SELECT DISTINCT tbl_name FROM sqlite_master WHERE type='index'"
        )
    ):
        return False
    if version in (13, 14) and not _valid_v13_structure(connection):
        return False
    if version == 14 and not _valid_v14_structure(connection):
        return False
    return True


V13_TABLES = (
    "resume_extraction_runs",
    "resume_text_segments",
    "resume_sanitization_runs",
    "resume_sanitization_spans",
    "resume_pii_findings",
    "resume_sanitization_review_events",
    "career_fact_runs",
    "career_fact_candidates",
    "career_fact_evidence",
    "career_fact_review_events",
    "career_profile_versions",
    "career_profile_facts",
    "career_profile_fact_sources",
    "career_profile_state",
)


def _valid_v13_structure(connection: sqlite3.Connection) -> bool:
    """Require the complete additive v13 object layout, exactly as bootstrapped."""
    from careeros.database.schema import initialize_schema

    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        initialize_schema(reference)
        # The v12 preference boundary predates ranking authority columns.
        reference.execute(
            "ALTER TABLE preference_state DROP COLUMN explicit_ranking_fingerprint"
        )
        reference.execute(
            "ALTER TABLE preference_state DROP COLUMN explicit_ranking_revision"
        )
        for table in V13_TABLES:
            if not _table_exists(connection, table):
                return False
            if (
                _canonical_master_sql(connection, "table", table)
                != _canonical_master_sql(reference, "table", table)
                or _pragma_rows(connection, "table_info", table)
                != _pragma_rows(reference, "table_info", table)
                or _pragma_rows(connection, "foreign_key_list", table)
                != _pragma_rows(reference, "foreign_key_list", table)
            ):
                return False
            actual = {
                str(r[1]) for r in connection.execute(f"PRAGMA index_list({table})")
            }
            expected = {
                str(r[1]) for r in reference.execute(f"PRAGMA index_list({table})")
            }
            if actual != expected:
                return False
            for index in actual:
                if _canonical_master_sql(
                    connection, "index", index
                ) != _canonical_master_sql(reference, "index", index) or _pragma_rows(
                    connection, "index_xinfo", index
                ) != _pragma_rows(reference, "index_xinfo", index):
                    return False
        # No partially-added or unrecognized v13 objects are tolerated.
        expected_objects = set(V13_TABLES)
        expected_objects.update(
            str(r[1])
            for table in V13_TABLES
            for r in reference.execute(f"PRAGMA index_list({table})")
            if not str(r[1]).startswith("sqlite_autoindex_")
        )
        actual_objects = {
            str(row[1])
            for row in connection.execute(
                "SELECT type,name,tbl_name FROM sqlite_master "
                "WHERE type IN ('table','index','trigger')"
            )
            if (
                str(row[1]).startswith(("resume_", "career_"))
                or str(row[2]) in V13_TABLES
            )
            and not str(row[1]).startswith("sqlite_autoindex_")
        }
        return actual_objects == expected_objects
    finally:
        reference.close()


def _preference_objects(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    prefixes = (
        "preference_",
        "explicit_preference",
        "learned_preference",
        "idx_preference_",
        "idx_explicit_preference",
        "idx_learned_preference",
        "trg_preference_",
        "trg_explicit_preference",
        "trg_learned_preference",
    )
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT type,name,tbl_name FROM sqlite_master "
            "WHERE type IN ('table','index','trigger')"
        )
        if str(row[1]).startswith(prefixes) or str(row[2]).startswith(prefixes)
    ]


def _valid_v12_structure(
    connection: sqlite3.Connection, *, allow_ranking_columns: bool
) -> bool:
    """Compare every preference table and index with a normative fresh v12."""
    from careeros.database.schema import initialize_schema

    tables = (
        "preference_state",
        "explicit_preferences",
        "preference_evidence",
        "learned_preference_suppressions",
    )
    expected_indexes = {
        "idx_preference_evidence_dimension_value",
        "idx_preference_evidence_review_event",
    }
    objects = _preference_objects(connection)
    if any(
        object_type == "table" and name not in tables
        for object_type, name, _table_name in objects
    ):
        return False
    if any(
        object_type == "index"
        and not name.startswith("sqlite_autoindex_")
        and name not in expected_indexes
        for object_type, name, _table_name in objects
    ):
        return False
    if any(object_type == "trigger" for object_type, _name, _table_name in objects):
        return False

    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        initialize_schema(reference)
        actual_state_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(preference_state)")
        }
        ranking_columns = {
            "explicit_ranking_revision",
            "explicit_ranking_fingerprint",
        }
        present_ranking_columns = actual_state_columns & ranking_columns
        expected_ranking_columns = ranking_columns if allow_ranking_columns else set()
        if present_ranking_columns != expected_ranking_columns:
            return False
        if not allow_ranking_columns:
            reference.execute(
                "ALTER TABLE preference_state DROP COLUMN explicit_ranking_fingerprint"
            )
            reference.execute(
                "ALTER TABLE preference_state DROP COLUMN explicit_ranking_revision"
            )
        for table in tables:
            if (
                _canonical_master_sql(connection, "table", table)
                != _canonical_master_sql(reference, "table", table)
                or _pragma_rows(connection, "table_info", table)
                != _pragma_rows(reference, "table_info", table)
                or _pragma_rows(connection, "foreign_key_list", table)
                != _pragma_rows(reference, "foreign_key_list", table)
            ):
                return False
            actual_indexes = {
                str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
                for row in connection.execute(f"PRAGMA index_list({table})")
            }
            reference_indexes = {
                str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
                for row in reference.execute(f"PRAGMA index_list({table})")
            }
            if actual_indexes != reference_indexes:
                return False
            for index in actual_indexes:
                if _pragma_rows(connection, "index_xinfo", index) != _pragma_rows(
                    reference, "index_xinfo", index
                ) or _canonical_master_sql(
                    connection, "index", index
                ) != _canonical_master_sql(reference, "index", index):
                    return False
        named = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name IN (?,?,?,?) AND name NOT LIKE 'sqlite_autoindex_%'",
                tables,
            )
        }
        if named != expected_indexes:
            return False
        state_rows = connection.execute(
            "SELECT singleton FROM preference_state"
        ).fetchall()
        return len(state_rows) == 1 and int(state_rows[0][0]) == 1
    finally:
        reference.close()


def _valid_v11_structure_legacy(connection: sqlite3.Connection) -> bool:
    required = {
        "application_packages": {
            "id",
            "application_id",
            "next_version_number",
            "created_at",
            "updated_at",
        },
        "application_package_versions": {
            "id",
            "package_id",
            "version_number",
            "state",
            "revision",
            "preparation_revision",
            "forked_from_version_id",
            "forked_from_version_number",
            "job_id_snapshot",
            "job_source",
            "job_source_job_id",
            "job_title",
            "job_company",
            "job_location",
            "job_url",
            "job_employment_type",
            "job_remote_status",
            "job_published_at",
            "job_updated_at_snapshot",
            "job_description_present",
            "job_content_fingerprint",
            "job_reconfirmation_required_at",
            "approved_revision",
            "approved_at",
            "finalized_at",
            "final_completeness_category",
            "created_at",
            "updated_at",
        },
        "application_package_requirement_snapshots": {
            "package_version_id",
            "requirement_id",
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
        },
        "package_material_selections": {
            "package_version_id",
            "requirement_id",
            "material_version_id",
            "material_id_snapshot",
            "material_name_snapshot",
            "material_category_snapshot",
            "material_version_number_snapshot",
            "display_filename_snapshot",
            "declared_media_type_snapshot",
            "byte_size_snapshot",
            "sha256_snapshot",
            "origin",
            "capture_method",
            "created_at",
            "updated_at",
        },
        "package_answers": {
            "package_version_id",
            "requirement_id",
            "answer_text",
            "origin",
            "capture_method",
            "confirmed",
            "confirmed_at",
            "created_at",
            "updated_at",
        },
        "package_completeness_reasons": {
            "package_version_id",
            "position",
            "code",
            "blocking",
            "requirement_id",
        },
    }
    if any(not _table_exists(connection, t) for t in required):
        return False
    if any(
        cols != {r[1] for r in connection.execute(f"PRAGMA table_info({t})")}
        for t, cols in required.items()
    ):
        return False
    expected = {
        "idx_application_packages_application",
        "idx_package_versions_package_number",
        "idx_package_versions_one_draft",
        "idx_package_versions_history",
        "idx_package_requirement_snapshots_order",
        "idx_package_material_selections_material_version",
    }
    named = {
        r[1]
        for t in required
        for r in connection.execute(f"PRAGMA index_list({t})")
        if not str(r[1]).startswith("sqlite_autoindex_")
    }
    if named != expected:
        return False
    checks = {
        "application_packages": (
            "application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE RESTRICT",
        ),
        "application_package_versions": (
            "state TEXT NOT NULL CHECK",
            "finalized_at IS NULL",
            "final_completeness_category='complete'",
        ),
        "application_package_requirement_snapshots": (
            "origin TEXT NOT NULL CHECK",
            "capture_method TEXT NOT NULL CHECK",
        ),
        "package_material_selections": (
            "REFERENCES material_versions(id) ON DELETE RESTRICT",
            "FOREIGN KEY",
        ),
        "package_answers": (
            "length(answer_text) BETWEEN 1 AND 10000",
            "confirmed_at IS NULL",
        ),
        "package_completeness_reasons": ("package_complete", "blocking IN"),
    }
    return all(
        all(
            fragment.lower()
            in (_normalized_sql(connection, "table", table) or "").lower()
            for fragment in fragments
        )
        for table, fragments in checks.items()
    )


def _canonical_master_sql(
    connection: sqlite3.Connection, object_type: str, name: str
) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return "".join(str(row[0]).lower().split())


def _pragma_rows(
    connection: sqlite3.Connection, pragma: str, name: str
) -> list[tuple[object, ...]]:
    return [tuple(row) for row in connection.execute(f"PRAGMA {pragma}({name})")]


def _valid_v11_structure(connection: sqlite3.Connection) -> bool:
    """Compare every M3 table/index property with a fresh normative v11 schema."""
    from careeros.database.schema import initialize_schema

    tables = (
        "application_packages",
        "application_package_versions",
        "application_package_requirement_snapshots",
        "package_material_selections",
        "package_answers",
        "package_completeness_reasons",
    )
    expected_indexes = {
        "idx_application_packages_application",
        "idx_package_versions_package_number",
        "idx_package_versions_one_draft",
        "idx_package_versions_history",
        "idx_package_requirement_snapshots_order",
        "idx_package_material_selections_material_version",
    }
    m3_table_prefixes = ("application_package", "package_")
    m3_named_object_prefixes = (
        "application_package",
        "package_",
        "idx_application_package",
        "idx_package_",
        "trg_application_package",
        "trg_package_",
    )
    master_objects = [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT type,name,tbl_name FROM sqlite_master WHERE type IN ('table','index','trigger')"
        )
    ]
    if any(
        object_type == "table"
        and name.startswith(m3_table_prefixes)
        and name not in tables
        for object_type, name, _table_name in master_objects
    ):
        return False
    if any(
        object_type == "index"
        and not name.startswith("sqlite_autoindex_")
        and name.startswith(m3_named_object_prefixes)
        and name not in expected_indexes
        for object_type, name, _table_name in master_objects
    ):
        return False
    if any(
        object_type == "trigger"
        and (table_name in tables or name.startswith(m3_named_object_prefixes))
        for object_type, name, table_name in master_objects
    ):
        return False
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        initialize_schema(reference)
        for table in tables:
            if (
                _canonical_master_sql(connection, "table", table)
                != _canonical_master_sql(reference, "table", table)
                or _pragma_rows(connection, "table_info", table)
                != _pragma_rows(reference, "table_info", table)
                or _pragma_rows(connection, "foreign_key_list", table)
                != _pragma_rows(reference, "foreign_key_list", table)
            ):
                return False
            actual_indexes = {
                str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
                for row in connection.execute(f"PRAGMA index_list({table})")
            }
            reference_indexes = {
                str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
                for row in reference.execute(f"PRAGMA index_list({table})")
            }
            if actual_indexes != reference_indexes:
                return False
            for index in actual_indexes:
                if _pragma_rows(connection, "index_xinfo", index) != _pragma_rows(
                    reference, "index_xinfo", index
                ) or _canonical_master_sql(
                    connection, "index", index
                ) != _canonical_master_sql(reference, "index", index):
                    return False
        named = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN (?,?,?,?,?,?) AND name NOT LIKE 'sqlite_autoindex_%'",
                tables,
            )
        }
        if named != expected_indexes:
            return False
        return True
    finally:
        reference.close()


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
        if not _valid_structure(connection, 9):
            raise ValueError("migration produced invalid v9 schema")
        connection.execute(
            "UPDATE schema_metadata SET value='9' WHERE key='schema_version'"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


MIGRATIONS[8] = _apply_8_to_9


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
        if not _valid_structure(connection, 10):
            raise ValueError("migration produced invalid v10 schema")
        connection.execute(
            "UPDATE schema_metadata SET value='10' WHERE key='schema_version'"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


MIGRATIONS[9] = _apply_9_to_10


def _apply_10_to_11(connection: sqlite3.Connection) -> None:
    """Add immutable, job-specific application package persistence."""
    statements = [
        """CREATE TABLE application_packages (
            id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'),
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,
            next_version_number INTEGER NOT NULL DEFAULT 1 CHECK (next_version_number BETWEEN 1 AND 2147483647),
            created_at TEXT NOT NULL CHECK (length(created_at)>0), updated_at TEXT NOT NULL CHECK (length(updated_at)>0))""",
        "CREATE UNIQUE INDEX idx_application_packages_application ON application_packages(application_id ASC)",
        """CREATE TABLE application_package_versions (
            id TEXT PRIMARY KEY CHECK(length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'),
            package_id TEXT NOT NULL REFERENCES application_packages(id) ON DELETE CASCADE,
            version_number INTEGER NOT NULL CHECK(version_number BETWEEN 1 AND 2147483647),
            state TEXT NOT NULL CHECK(state IN ('draft','finalized')), revision INTEGER NOT NULL DEFAULT 0 CHECK(revision BETWEEN 0 AND 2147483647),
            preparation_revision INTEGER NOT NULL CHECK(preparation_revision BETWEEN 0 AND 2147483647),
            forked_from_version_id TEXT, forked_from_version_number INTEGER,
            job_id_snapshot INTEGER NOT NULL CHECK(job_id_snapshot>0), job_source TEXT NOT NULL CHECK(length(job_source)>0), job_source_job_id TEXT,
            job_title TEXT NOT NULL CHECK(length(job_title)>0), job_company TEXT, job_location TEXT, job_url TEXT, job_employment_type TEXT, job_remote_status TEXT, job_published_at TEXT,
            job_updated_at_snapshot TEXT NOT NULL CHECK(length(job_updated_at_snapshot)>0), job_description_present INTEGER NOT NULL CHECK(job_description_present IN(0,1)),
            job_content_fingerprint TEXT NOT NULL CHECK(length(job_content_fingerprint)=64 AND job_content_fingerprint=lower(job_content_fingerprint) AND job_content_fingerprint NOT GLOB '*[^0-9a-f]*'),
            job_reconfirmation_required_at TEXT, approved_revision INTEGER, approved_at TEXT, finalized_at TEXT, final_completeness_category TEXT,
            created_at TEXT NOT NULL CHECK(length(created_at)>0), updated_at TEXT NOT NULL CHECK(length(updated_at)>0),
            CHECK((forked_from_version_id IS NULL AND forked_from_version_number IS NULL) OR (forked_from_version_id IS NOT NULL AND length(forked_from_version_id)=32 AND forked_from_version_id=lower(forked_from_version_id) AND forked_from_version_id NOT GLOB '*[^0-9a-f]*' AND forked_from_version_number BETWEEN 1 AND 2147483647)),
            CHECK((approved_revision IS NULL AND approved_at IS NULL) OR (approved_revision IS NOT NULL AND approved_revision BETWEEN 0 AND revision AND approved_at IS NOT NULL AND length(approved_at)>0)),
            CHECK((state='draft' AND finalized_at IS NULL AND final_completeness_category IS NULL) OR (state='finalized' AND finalized_at IS NOT NULL AND length(finalized_at)>0 AND final_completeness_category='complete' AND approved_revision=revision AND approved_at IS NOT NULL)))""",
        "CREATE UNIQUE INDEX idx_package_versions_package_number ON application_package_versions(package_id COLLATE BINARY ASC,version_number ASC)",
        "CREATE UNIQUE INDEX idx_package_versions_one_draft ON application_package_versions(package_id COLLATE BINARY ASC) WHERE state='draft'",
        "CREATE INDEX idx_package_versions_history ON application_package_versions(package_id COLLATE BINARY ASC,version_number DESC) WHERE state='finalized'",
        """CREATE TABLE application_package_requirement_snapshots (
            package_version_id TEXT NOT NULL REFERENCES application_package_versions(id) ON DELETE CASCADE, requirement_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN('material','link','question','task','deadline','other')), label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 160), detail TEXT CHECK(detail IS NULL OR length(detail) BETWEEN 1 AND 1000), required INTEGER NOT NULL CHECK(required IN(0,1)), fulfillment_state TEXT NOT NULL CHECK(fulfillment_state IN('open','satisfied','not_applicable')), deadline_at TEXT, sort_order INTEGER NOT NULL CHECK(sort_order>=0), origin TEXT NOT NULL CHECK(origin='manual'), capture_method TEXT NOT NULL CHECK(capture_method='manual'), confirmed INTEGER NOT NULL CHECK(confirmed IN(0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(package_version_id,requirement_id), CHECK(kind!='deadline' OR fulfillment_state!='satisfied'))""",
        "CREATE UNIQUE INDEX idx_package_requirement_snapshots_order ON application_package_requirement_snapshots(package_version_id COLLATE BINARY ASC,sort_order ASC)",
        """CREATE TABLE package_material_selections (
            package_version_id TEXT NOT NULL, requirement_id TEXT NOT NULL, material_version_id TEXT NOT NULL REFERENCES material_versions(id) ON DELETE RESTRICT,
            material_id_snapshot TEXT NOT NULL CHECK(length(material_id_snapshot)=32 AND material_id_snapshot=lower(material_id_snapshot) AND material_id_snapshot NOT GLOB '*[^0-9a-f]*'), material_name_snapshot TEXT NOT NULL CHECK(length(material_name_snapshot) BETWEEN 1 AND 160 AND material_name_snapshot=trim(material_name_snapshot)), material_category_snapshot TEXT NOT NULL CHECK(material_category_snapshot IN('resume','cover_letter_base','portfolio','supporting_document','other')), material_version_number_snapshot INTEGER NOT NULL CHECK(material_version_number_snapshot BETWEEN 1 AND 100), display_filename_snapshot TEXT NOT NULL CHECK(length(display_filename_snapshot) BETWEEN 1 AND 255), declared_media_type_snapshot TEXT NOT NULL CHECK(length(declared_media_type_snapshot) BETWEEN 3 AND 127), byte_size_snapshot INTEGER NOT NULL CHECK(byte_size_snapshot BETWEEN 1 AND 26214400), sha256_snapshot TEXT NOT NULL CHECK(length(sha256_snapshot)=64 AND sha256_snapshot=lower(sha256_snapshot) AND sha256_snapshot NOT GLOB '*[^0-9a-f]*'), origin TEXT NOT NULL CHECK(origin='manual'), capture_method TEXT NOT NULL CHECK(capture_method='manual'), created_at TEXT NOT NULL CHECK(length(created_at)>0), updated_at TEXT NOT NULL CHECK(length(updated_at)>0),
            PRIMARY KEY(package_version_id,requirement_id), FOREIGN KEY(package_version_id,requirement_id) REFERENCES application_package_requirement_snapshots(package_version_id,requirement_id) ON DELETE CASCADE)""",
        "CREATE INDEX idx_package_material_selections_material_version ON package_material_selections(material_version_id COLLATE BINARY ASC)",
        """CREATE TABLE package_answers (
            package_version_id TEXT NOT NULL, requirement_id TEXT NOT NULL, answer_text TEXT NOT NULL CHECK(length(answer_text) BETWEEN 1 AND 10000), origin TEXT NOT NULL CHECK(origin='manual'), capture_method TEXT NOT NULL CHECK(capture_method='manual'), confirmed INTEGER NOT NULL CHECK(confirmed IN(0,1)), confirmed_at TEXT, created_at TEXT NOT NULL CHECK(length(created_at)>0), updated_at TEXT NOT NULL CHECK(length(updated_at)>0), PRIMARY KEY(package_version_id,requirement_id), FOREIGN KEY(package_version_id,requirement_id) REFERENCES application_package_requirement_snapshots(package_version_id,requirement_id) ON DELETE CASCADE, CHECK((confirmed=0 AND confirmed_at IS NULL) OR (confirmed=1 AND confirmed_at IS NOT NULL AND length(confirmed_at)>0)))""",
        """CREATE TABLE package_completeness_reasons (
            package_version_id TEXT NOT NULL REFERENCES application_package_versions(id) ON DELETE CASCADE, position INTEGER NOT NULL CHECK(position>=0), code TEXT NOT NULL CHECK(code IN('job_information_unavailable','preparation_revision_stale','job_snapshot_stale','job_reconfirmation_required','requirements_not_reviewed','requirements_review_stale','requirement_unconfirmed','required_deadline_passed','required_requirement_open','ready_confirmation_missing','ready_confirmation_stale','material_selection_missing','selected_material_retired','selected_material_unavailable','answer_missing','answer_unconfirmed','package_complete')), blocking INTEGER NOT NULL CHECK(blocking IN(0,1)), requirement_id TEXT, PRIMARY KEY(package_version_id,position), CHECK((code IN('job_information_unavailable','package_complete') AND blocking=0) OR (code NOT IN('job_information_unavailable','package_complete') AND blocking=1)))""",
    ]
    for statement in statements:
        connection.execute(statement)


MIGRATIONS[10] = _apply_10_to_11


def migrate_10_to_11(connection: sqlite3.Connection) -> None:
    """Apply the v10 boundary as one independently usable transaction."""
    if not _valid_structure(connection, 10):
        raise ValueError("invalid v10 schema")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _apply_10_to_11(connection)
        if not _valid_structure(connection, 11):
            raise ValueError("migration produced invalid v11 schema")
        connection.execute(
            "UPDATE schema_metadata SET value='11' WHERE key='schema_version'"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _apply_11_to_12(connection: sqlite3.Connection) -> None:
    """Add persistent explicit and learned preference authority storage."""
    statements = [
        """CREATE TABLE preference_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            explicit_configured INTEGER NOT NULL DEFAULT 0
                CHECK (explicit_configured IN (0,1)),
            explicit_updated_at TEXT,
            learning_reset_at TEXT,
            learning_reset_review_event_id INTEGER NOT NULL DEFAULT 0
                CHECK (learning_reset_review_event_id >= 0),
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE explicit_preferences (
            dimension TEXT NOT NULL CHECK (dimension IN (
                'desired_titles','skills','required_skills',
                'preferred_locations','remote_preference','excluded_terms',
                'hard_excluded_terms','preferred_employment_types',
                'preferred_seniority'
            )),
            position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 99),
            value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 100),
            PRIMARY KEY (dimension, position)
        )""",
        """CREATE TABLE preference_evidence (
            job_id INTEGER NOT NULL
                REFERENCES jobs(id) ON DELETE RESTRICT,
            review_event_id INTEGER NOT NULL
                REFERENCES review_events(id) ON DELETE RESTRICT,
            dimension TEXT NOT NULL CHECK (dimension IN (
                'title','company','location','remote','employment_type','seniority'
            )),
            value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            signal INTEGER NOT NULL CHECK (signal IN (-1,1)),
            observed_at TEXT NOT NULL,
            PRIMARY KEY (job_id, dimension, normalized_value),
            UNIQUE (review_event_id, dimension, normalized_value)
        )""",
        "CREATE INDEX idx_preference_evidence_dimension_value "
        "ON preference_evidence(dimension, normalized_value)",
        "CREATE INDEX idx_preference_evidence_review_event "
        "ON preference_evidence(review_event_id)",
        """CREATE TABLE learned_preference_suppressions (
            preference_key TEXT PRIMARY KEY CHECK (
                length(preference_key) = 32
                AND preference_key = lower(preference_key)
                AND preference_key NOT GLOB '*[^0-9a-f]*'
            ),
            dimension TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            suppressed_at TEXT NOT NULL,
            UNIQUE (dimension, normalized_value)
        )""",
    ]
    for statement in statements:
        connection.execute(statement)
    reset_event_id = int(
        connection.execute("SELECT COALESCE(MAX(id), 0) FROM review_events").fetchone()[
            0
        ]
    )
    connection.execute(
        """
        INSERT INTO preference_state (
            singleton, explicit_configured, explicit_updated_at,
            learning_reset_at, learning_reset_review_event_id, created_at
        )
        VALUES (1, 0, NULL, NULL, ?, ?)
        """,
        (reset_event_id, datetime.now(UTC).isoformat()),
    )


MIGRATIONS[11] = _apply_11_to_12


def migrate_11_to_12(connection: sqlite3.Connection) -> None:
    """Apply the v11 boundary as one independently usable transaction."""
    if not _valid_structure(connection, 11):
        raise ValueError("invalid v11 schema")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _apply_11_to_12(connection)
        if not _valid_structure(connection, 12):
            raise ValueError("migration produced invalid v12 schema")
        connection.execute(
            "UPDATE schema_metadata SET value='12' WHERE key='schema_version'"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _apply_12_to_13(connection: sqlite3.Connection) -> None:
    """Add the v1.05 persistence foundation without reinterpretation."""
    if any(
        str(row[1]).startswith(("resume_", "career_"))
        for row in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index','trigger')"
        )
    ):
        raise ValueError("database contains pre-existing v13 objects")
    for statement in V13_DDL.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)


MIGRATIONS[12] = _apply_12_to_13


def _apply_13_to_14(connection: sqlite3.Connection) -> None:
    if any(
        _table_exists(connection, table)
        for table in (
            "review_ranking_current",
            "review_ranking_items",
            "review_ranking_generations",
            "review_ranking_authority",
        )
    ):
        raise ValueError("schema-13 database contains pre-existing ranking objects")
    dimensions = (
        "desired_titles",
        "skills",
        "required_skills",
        "preferred_locations",
        "remote_preference",
        "excluded_terms",
        "hard_excluded_terms",
        "preferred_employment_types",
        "preferred_seniority",
    )
    from careeros.preferences.models import (
        ExplicitPreferenceProfile,
        explicit_ranking_authority_fingerprint,
    )

    values = {
        dimension: tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT value FROM explicit_preferences WHERE dimension=? ORDER BY position",
                (dimension,),
            )
        )
        for dimension in dimensions
    }
    profile = ExplicitPreferenceProfile(
        desired_titles=values["desired_titles"],
        skills=values["skills"],
        required_skills=values["required_skills"],
        preferred_locations=values["preferred_locations"],
        remote_preference=(
            values["remote_preference"][0] if values["remote_preference"] else None
        ),
        excluded_terms=values["excluded_terms"],
        hard_excluded_terms=values["hard_excluded_terms"],
        preferred_employment_types=values["preferred_employment_types"],
        preferred_seniority=values["preferred_seniority"],
    )
    explicit_fp = explicit_ranking_authority_fingerprint(profile)
    connection.execute("ALTER TABLE preference_state RENAME TO preference_state_v13")
    connection.execute(
        """CREATE TABLE preference_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            explicit_configured INTEGER NOT NULL DEFAULT 0
                CHECK (explicit_configured IN (0,1)),
            explicit_updated_at TEXT,
            explicit_ranking_revision INTEGER NOT NULL DEFAULT 0 CHECK (typeof(explicit_ranking_revision)='integer' AND explicit_ranking_revision >= 0),
            explicit_ranking_fingerprint TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' CHECK (length(explicit_ranking_fingerprint)=64),
            learning_reset_at TEXT,
            learning_reset_review_event_id INTEGER NOT NULL DEFAULT 0
                CHECK (learning_reset_review_event_id >= 0),
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """INSERT INTO preference_state(
               singleton,explicit_configured,explicit_updated_at,
               explicit_ranking_revision,explicit_ranking_fingerprint,
               learning_reset_at,learning_reset_review_event_id,created_at
           )
           SELECT singleton,explicit_configured,explicit_updated_at,0,?,
                  learning_reset_at,learning_reset_review_event_id,created_at
           FROM preference_state_v13""",
        (explicit_fp,),
    )
    connection.execute("DROP TABLE preference_state_v13")
    for statement in V14_DDL.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)
    from careeros.database.review_ranking import (
        REVIEW_RANKING_GENERATION_FORMAT_VERSION,
        REVIEW_RANKING_NORMALIZER_VERSION,
        REVIEW_RANKING_SCORER_VERSION,
    )
    from careeros.preferences.service import _learned_authority_fingerprint
    from careeros.ranking.materialization import (
        opportunity_corpus_fingerprint,
        snapshot_opportunities,
    )

    learned_fp = _learned_authority_fingerprint(connection)
    opportunities = snapshot_opportunities(connection)
    opportunity_fp = opportunity_corpus_fingerprint(opportunities)
    connection.execute(
        "INSERT INTO review_ranking_authority(singleton,target_revision,explicit_revision,explicit_fingerprint,opportunity_revision,opportunity_fingerprint,opportunity_count,learned_revision,learned_fingerprint,scorer_version,normalizer_version,generation_format_version,required_mode_mask,state,updated_at) SELECT 1,0,explicit_ranking_revision,?,0,?,?,0,?,?,?,?,3,'rebuild_required',? FROM preference_state WHERE singleton=1",
        (
            explicit_fp,
            opportunity_fp,
            len(opportunities),
            learned_fp,
            REVIEW_RANKING_SCORER_VERSION,
            REVIEW_RANKING_NORMALIZER_VERSION,
            REVIEW_RANKING_GENERATION_FORMAT_VERSION,
            datetime.now(UTC).isoformat(),
        ),
    )


MIGRATIONS[13] = _apply_13_to_14


def migrate_12_to_13(connection: sqlite3.Connection) -> None:
    """Apply the v12 boundary as one independently usable transaction."""
    if inspect_schema(connection) != "upgrade_required":
        raise ValueError("database is not a structurally valid v12 database")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _apply_12_to_13(connection)
        if not _valid_structure(connection, 13):
            raise ValueError("migration produced invalid v13 schema")
        connection.execute(
            "UPDATE schema_metadata SET value='13' WHERE key='schema_version'"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


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
                if not _valid_structure(connection, version):
                    raise ValueError(f"invalid database schema boundary (v{version})")
                MIGRATIONS[version](connection)
                target = version + 1
                if not _valid_structure(connection, target):
                    raise ValueError(f"migration produced invalid v{target} schema")
                connection.execute(
                    "UPDATE schema_metadata SET value=? WHERE key='schema_version'",
                    (str(target),),
                )
                version = target
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
