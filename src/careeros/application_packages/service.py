"""Transactional persistence primitives for job-specific application packages.

The service deliberately keeps package rows anchored to concrete application
IDs.  Callers own the database connection; each mutating operation is one
SQLite transaction and uses both revisions as independent compare-and-swap
guards.
"""

# SQL statements retain their direct correspondence to the package contract.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from careeros.application_packages.compatibility import requirement_compatibility
from careeros.application_packages.fingerprint import job_content_fingerprint
from careeros.locking import collection_lock
from careeros.materials.models import StorageError
from careeros.materials.storage import (
    ensure_binding_fd,
    marker_binding_evidence_fd,
    open_bound_object_with_root,
    root_identity,
)


class PackageError(ValueError):
    """Stable domain failure raised by package operations."""


class PackageConflict(PackageError):
    def __init__(self, code: str, current_revision: int) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision


@dataclass
class VerifiedMaterialHandle:
    """Operation-scoped descriptor retained from preflight through freeze."""

    fd: int
    root_fd: int
    before: tuple[int, int, int, int, int, int, int, int]
    root: Path
    root_identity: tuple[int, int]
    material_version_id: str
    store_id: str
    storage_key: str
    marker_evidence: tuple[int, int, int, int, int, int, int, int]

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1

    def is_still_valid(self) -> bool:
        """Revalidate the held object and configured root without rereading bytes."""
        try:
            return (
                self.fd >= 0
                and self.root_fd >= 0
                and _stat_evidence(os.fstat(self.fd)) == self.before
                and _root_stat_identity(os.fstat(self.root_fd)) == self.root_identity
                and root_identity(self.root) == self.root_identity
                and ensure_binding_fd(self.root_fd, self.root, self.store_id)
                == self.store_id
                and marker_binding_evidence_fd(self.root_fd)
                == (self.store_id, self.marker_evidence)
            )
        except (OSError, StorageError, TypeError, ValueError):
            return False


def _stat_evidence(st: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
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


def _root_stat_identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def open_verified_material(
    connection: sqlite3.Connection, root: Path, material_version_id: str
) -> VerifiedMaterialHandle | None:
    row = connection.execute(
        "SELECT v.storage_key,v.byte_size,v.sha256,s.store_id FROM material_versions v JOIN material_store s ON s.singleton=1 WHERE v.id=?",
        (material_version_id,),
    ).fetchone()
    if row is None:
        return None
    return _open_verified_material_metadata(
        root,
        material_version_id=material_version_id,
        store_id=str(row["store_id"]),
        storage_key=str(row["storage_key"]),
        byte_size=int(row["byte_size"]),
        sha256=str(row["sha256"]),
    )


def _open_verified_material_metadata(
    root: Path,
    *,
    material_version_id: str,
    store_id: str,
    storage_key: str,
    byte_size: int,
    sha256: str,
) -> VerifiedMaterialHandle | None:
    fd = -1
    root_fd = -1
    try:
        opened_root_identity = root_identity(root)
        fd, root_fd, marker_evidence = open_bound_object_with_root(
            root, store_id, storage_key
        )
        if _root_stat_identity(os.fstat(root_fd)) != opened_root_identity:
            os.close(fd)
            os.close(root_fd)
            return None
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size != byte_size
        ):
            os.close(fd)
            os.close(root_fd)
            return None
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != sha256:
            os.close(fd)
            os.close(root_fd)
            return None
        after = os.fstat(fd)
        if _stat_evidence(after) != _stat_evidence(before):
            os.close(fd)
            os.close(root_fd)
            return None
        if root_identity(root) != opened_root_identity:
            os.close(fd)
            os.close(root_fd)
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        return VerifiedMaterialHandle(
            fd=fd,
            root_fd=root_fd,
            before=_stat_evidence(before),
            root=root,
            root_identity=opened_root_identity,
            material_version_id=material_version_id,
            store_id=store_id,
            storage_key=storage_key,
            marker_evidence=marker_evidence,
        )
    except (OSError, StorageError, TypeError, ValueError):
        if fd >= 0:
            os.close(fd)
        if root_fd >= 0:
            os.close(root_fd)
        return None


REASON_MESSAGES = {
    "job_information_unavailable": "CareerOS lacks usable stored job information.",
    "preparation_revision_stale": "The draft targets an older preparation revision.",
    "job_snapshot_stale": "The stored job changed after the draft snapshot.",
    "job_reconfirmation_required": "M1 review and ready confirmation must be renewed after accepting a changed job.",
    "requirements_not_reviewed": "The requirement set has not been reviewed.",
    "requirements_review_stale": "The requirement set changed after review.",
    "requirement_unconfirmed": "A captured requirement is unconfirmed.",
    "required_deadline_passed": "A required deadline has passed.",
    "required_requirement_open": "A required non-deadline requirement remains open.",
    "ready_confirmation_missing": "The current revision has not been confirmed ready.",
    "ready_confirmation_stale": "Ready confirmation targets an older revision.",
    "material_selection_missing": "A required applicable Material requirement lacks a selection.",
    "selected_material_retired": "A selected Material is currently retired.",
    "selected_material_unavailable": "A selected exact version cannot pass safe bound-store verification.",
    "answer_missing": "A required applicable answer-capable requirement lacks an answer.",
    "answer_unconfirmed": "A stored package answer is not confirmed.",
    "package_complete": "The package is deterministically complete.",
}


def completeness(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate draft authority with five batched reads and stable ordering."""
    authority = _load_authority(connection, version_id)
    version = authority["version"]
    if version["state"] == "finalized":
        stored_reasons = [
            dict(r)
            for r in connection.execute(
                "SELECT position,code,blocking,requirement_id FROM package_completeness_reasons WHERE package_version_id=? ORDER BY position",
                (version_id,),
            )
        ]
        return {
            "category": "complete",
            "blocking_count": 0,
            "evaluated_at": version["finalized_at"],
            "reasons": [
                {**r, "message": REASON_MESSAGES[r["code"]]} for r in stored_reasons
            ],
        }
    availability = _verify_distinct_materials(connection, root, authority["selections"])
    return _evaluate_authority(
        authority, availability=availability, now=now or datetime.now(UTC)
    )


def _load_authority(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    include_live_items: bool | None = True,
) -> dict[str, Any]:
    head = connection.execute(
        """SELECT v.*,p.application_id,a.job_id AS owner_job_id,
                  j.source AS live_source,j.source_job_id AS live_source_job_id,
                  j.title AS live_title,j.company AS live_company,
                  j.location AS live_location,j.description AS live_description,
                  j.url AS live_url,j.employment_type AS live_employment_type,
                  j.remote_status AS live_remote_status,
                  j.published_at AS live_published_at,j.updated_at AS live_updated_at,
                  prep.revision AS live_preparation_revision,
                  prep.requirements_reviewed_revision,prep.requirements_reviewed_at,
                  prep.ready_confirmed_revision,prep.ready_confirmed_at
           FROM application_package_versions v
           JOIN application_packages p ON p.id=v.package_id
           JOIN applications a ON a.id=p.application_id
           LEFT JOIN jobs j ON j.id=a.job_id
           LEFT JOIN application_preparations prep ON prep.application_id=a.id
           WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if head is None:
        raise PackageError("package_version_not_found")
    load_live_items = (
        head["state"] == "draft" if include_live_items is None else include_live_items
    )
    live_items = (
        [
            dict(r)
            for r in connection.execute(
                "SELECT id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at FROM application_requirement_items WHERE application_id=? ORDER BY sort_order,id",
                (head["application_id"],),
            )
        ]
        if load_live_items
        else []
    )
    snapshots = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM application_package_requirement_snapshots WHERE package_version_id=? ORDER BY sort_order,requirement_id",
            (version_id,),
        )
    ]
    selections = [
        dict(r)
        for r in connection.execute(
            """SELECT s.*,v.material_id AS live_material_id,
                      v.version_number AS live_version_number,
                      v.display_filename AS live_display_filename,
                      v.declared_media_type AS live_media_type,
                      v.byte_size AS live_byte_size,v.sha256 AS live_sha256,
                      v.storage_key AS live_storage_key,m.name AS live_material_name,
                      m.category AS live_material_category,m.status AS live_material_status,
                      ms.store_id AS live_store_id
               FROM package_material_selections s
               LEFT JOIN material_versions v ON v.id=s.material_version_id
               LEFT JOIN materials m ON m.id=v.material_id
               LEFT JOIN material_store ms ON ms.singleton=1
               WHERE s.package_version_id=? ORDER BY s.requirement_id""",
            (version_id,),
        )
    ]
    answers = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM package_answers WHERE package_version_id=? ORDER BY requirement_id",
            (version_id,),
        )
    ]
    return {
        "version": dict(head),
        "live_items": live_items,
        "snapshots": snapshots,
        "selections": selections,
        "answers": answers,
    }


def _manifest_matches(selection: dict[str, Any]) -> bool:
    return bool(
        selection.get("live_material_id") == selection["material_id_snapshot"]
        and selection.get("live_version_number")
        == selection["material_version_number_snapshot"]
        and selection.get("live_display_filename")
        == selection["display_filename_snapshot"]
        and selection.get("live_media_type")
        == selection["declared_media_type_snapshot"]
        and selection.get("live_byte_size") == selection["byte_size_snapshot"]
        and selection.get("live_sha256") == selection["sha256_snapshot"]
    )


def _verify_distinct_materials(
    _connection: sqlite3.Connection,
    root: Path | None,
    selections: list[dict[str, Any]],
) -> dict[str, bool]:
    availability: dict[str, bool] = {}
    for selection in selections:
        material_version_id = str(selection["material_version_id"])
        if material_version_id in availability:
            continue
        handle = (
            _open_verified_material_metadata(
                root,
                material_version_id=material_version_id,
                store_id=str(selection["live_store_id"]),
                storage_key=str(selection["live_storage_key"]),
                byte_size=int(selection["live_byte_size"]),
                sha256=str(selection["live_sha256"]),
            )
            if root is not None
            and _manifest_matches(selection)
            and selection.get("live_store_id") is not None
            else None
        )
        availability[material_version_id] = handle is not None
        if handle is not None:
            handle.close()
    return availability


def _aware(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _evaluate_authority(
    authority: dict[str, Any], *, availability: dict[str, bool], now: datetime
) -> dict[str, Any]:
    version = authority["version"]
    snapshots = authority["snapshots"]
    selections = {s["requirement_id"]: s for s in authority["selections"]}
    answers = {a["requirement_id"]: a for a in authority["answers"]}
    candidates: dict[str, list[str | None]] = {code: [] for code in REASON_MESSAGES}

    def add(code: str, requirement_id: str | None = None) -> None:
        if requirement_id not in candidates[code]:
            candidates[code].append(requirement_id)

    live_job = {
        "source": version.get("live_source"),
        "source_job_id": version.get("live_source_job_id"),
        "title": version.get("live_title"),
        "company": version.get("live_company"),
        "location": version.get("live_location"),
        "description": version.get("live_description"),
        "url": version.get("live_url"),
        "employment_type": version.get("live_employment_type"),
        "remote_status": version.get("live_remote_status"),
        "published_at": version.get("live_published_at"),
    }
    if (
        version.get("live_source") is None
        or not (version.get("live_description") or "").strip()
    ):
        add("job_information_unavailable")
    if (
        version.get("live_source") is None
        or int(version["owner_job_id"]) != int(version["job_id_snapshot"])
        or job_content_fingerprint(live_job) != version["job_content_fingerprint"]
    ):
        add("job_snapshot_stale")

    live_revision = int(version.get("live_preparation_revision") or 0)
    snapshot_comparable = [
        {
            "id": item["requirement_id"],
            **{
                key: item[key]
                for key in (
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
                )
            },
        }
        for item in snapshots
    ]
    if (
        live_revision != int(version["preparation_revision"])
        or authority["live_items"] != snapshot_comparable
    ):
        add("preparation_revision_stale")
    if version.get("requirements_reviewed_revision") is None:
        add("requirements_not_reviewed")
    elif int(version["requirements_reviewed_revision"]) != live_revision:
        add("requirements_review_stale")

    reconfirm = _aware(version.get("job_reconfirmation_required_at"))
    if reconfirm is not None:
        reviewed = _aware(version.get("requirements_reviewed_at"))
        ready = _aware(version.get("ready_confirmed_at"))
        if (
            reviewed is None
            or ready is None
            or version.get("requirements_reviewed_revision") != live_revision
            or version.get("ready_confirmed_revision") != live_revision
            or reviewed <= reconfirm
            or ready <= reconfirm
        ):
            add("job_reconfirmation_required")
    elif version.get("job_reconfirmation_required_at") is not None:
        add("job_reconfirmation_required")

    for item in snapshots:
        rid = str(item["requirement_id"])
        applicable = item["fulfillment_state"] != "not_applicable"
        if not item["confirmed"]:
            add("requirement_unconfirmed", rid)
        if item["required"] and item["fulfillment_state"] == "open":
            if item["kind"] == "deadline" and item["deadline_at"]:
                due = _aware(item["deadline_at"])
                if due is not None and due <= now.astimezone(UTC):
                    add("required_deadline_passed", rid)
            elif item["kind"] != "deadline":
                add("required_requirement_open", rid)
        if item["kind"] == "material" and applicable:
            selected = selections.get(rid)
            if selected is None:
                if item["required"]:
                    add("material_selection_missing", rid)
            elif selected.get("live_material_status") == "retired":
                add("selected_material_retired", rid)
            elif not _manifest_matches(selected) or not availability.get(
                str(selected["material_version_id"]), False
            ):
                add("selected_material_unavailable", rid)
        if item["kind"] in ("question", "link", "other") and applicable:
            answer = answers.get(rid)
            if answer is None:
                if item["required"]:
                    add("answer_missing", rid)
            elif not answer["confirmed"]:
                add("answer_unconfirmed", rid)

    if version.get("ready_confirmed_revision") is None:
        add("ready_confirmation_missing")
    elif int(version["ready_confirmed_revision"]) != live_revision:
        add("ready_confirmation_stale")

    ordered_codes = list(REASON_MESSAGES)
    blocking_codes = [
        code
        for code in ordered_codes
        if code not in ("job_information_unavailable", "package_complete")
    ]
    if not any(candidates[code] for code in blocking_codes):
        add("package_complete")
    reasons: list[dict[str, Any]] = []
    for code in ordered_codes:
        for requirement_id in candidates[code]:
            reasons.append(
                {
                    "position": len(reasons),
                    "code": code,
                    "blocking": code
                    not in ("job_information_unavailable", "package_complete"),
                    "requirement_id": requirement_id,
                    "message": REASON_MESSAGES[code],
                }
            )
    stale_codes = {
        "preparation_revision_stale",
        "job_snapshot_stale",
        "job_reconfirmation_required",
        "requirements_review_stale",
        "ready_confirmation_stale",
    }
    category = (
        "stale"
        if any(reason["code"] in stale_codes for reason in reasons)
        else (
            "complete"
            if reasons and reasons[-1]["code"] == "package_complete"
            else "incomplete"
        )
    )
    return {
        "category": category,
        "blocking_count": sum(bool(r["blocking"]) for r in reasons),
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "reasons": reasons,
    }


def verify_material_version(
    connection: sqlite3.Connection, root: Path, material_version_id: str
) -> bool:
    """Verify one bound M2 object without mutating the database or root."""
    handle = open_verified_material(connection, root, material_version_id)
    if handle is None:
        return False
    try:
        return handle.is_still_valid()
    finally:
        handle.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return secrets.token_hex(16)


def _application(
    connection: sqlite3.Connection, job_id: int
) -> tuple[int, sqlite3.Row]:
    row = connection.execute(
        "SELECT a.id AS application_id, j.* FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        if (
            connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
            is None
        ):
            raise PackageError("job_not_found")
        raise PackageError("application_not_tracked")
    return int(row["application_id"]), cast(sqlite3.Row, row)


def _snapshot_job(row: sqlite3.Row) -> dict[str, Any]:
    values = {key: row[key] for key in row.keys()}
    return {
        "job_id_snapshot": int(values["id"]),
        "job_source": values["source"],
        "job_source_job_id": values.get("source_job_id"),
        "job_title": values["title"],
        "job_company": values.get("company"),
        "job_location": values.get("location"),
        "job_url": values.get("url"),
        "job_employment_type": values.get("employment_type"),
        "job_remote_status": values.get("remote_status"),
        "job_published_at": values.get("published_at"),
        "job_updated_at_snapshot": values["updated_at"],
        "job_description_present": int(bool((values.get("description") or "").strip())),
        "job_content_fingerprint": job_content_fingerprint(values),
    }


def get_package(connection: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    aid, _ = _application(connection, job_id)
    package = connection.execute(
        "SELECT * FROM application_packages WHERE application_id=?", (aid,)
    ).fetchone()
    if package is None:
        return {"application_id": aid, "package": None, "versions": []}
    versions = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM application_package_versions WHERE package_id=? ORDER BY version_number",
            (package["id"],),
        )
    ]
    return {"application_id": aid, "package": dict(package), "versions": versions}


def get_version(connection: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    """Return an explicit version detail projection (answers only here)."""
    row = connection.execute(
        "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
    ).fetchone()
    if row is None:
        raise PackageError("package_version_not_found")
    result = dict(row)
    result["requirements"] = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM application_package_requirement_snapshots WHERE package_version_id=? ORDER BY sort_order,requirement_id",
            (version_id,),
        )
    ]
    result["material_selections"] = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM package_material_selections WHERE package_version_id=?",
            (version_id,),
        )
    ]
    result["answers"] = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM package_answers WHERE package_version_id=?", (version_id,)
        )
    ]
    result["completeness_reasons"] = [
        dict(r)
        for r in connection.execute(
            "SELECT position,code,blocking,requirement_id FROM package_completeness_reasons WHERE package_version_id=? ORDER BY position",
            (version_id,),
        )
    ]
    return result


def open_draft(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    preparation_revision: int | None = None,
) -> tuple[dict[str, Any], bool]:
    aid, job = _application(connection, job_id)
    now = _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        live_preparation_revision = _prep_revision(connection, aid)
        if (
            preparation_revision is not None
            and live_preparation_revision != preparation_revision
        ):
            raise PackageConflict(
                "preparation_revision_conflict", live_preparation_revision
            )
        package = connection.execute(
            "SELECT * FROM application_packages WHERE application_id=?", (aid,)
        ).fetchone()
        if package is None:
            pid = _id()
            connection.execute(
                "INSERT INTO application_packages(id,application_id,created_at,updated_at) VALUES(?,?,?,?)",
                (pid, aid, now, now),
            )
            package = connection.execute(
                "SELECT * FROM application_packages WHERE id=?", (pid,)
            ).fetchone()
        draft = connection.execute(
            "SELECT * FROM application_package_versions WHERE package_id=? AND state='draft'",
            (package["id"],),
        ).fetchone()
        if draft is not None:
            connection.commit()
            return dict(draft), False
        previous = connection.execute(
            "SELECT * FROM application_package_versions WHERE package_id=? AND state='finalized' ORDER BY version_number DESC LIMIT 1",
            (package["id"],),
        ).fetchone()
        retained_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM application_package_versions WHERE package_id=?",
                (package["id"],),
            ).fetchone()[0]
        )
        if retained_count >= 100:
            raise PackageError("package_version_limit_reached")
        number = int(package["next_version_number"])
        values = _snapshot_job(job)
        vid = _id()
        columns = [
            "id",
            "package_id",
            "version_number",
            "state",
            "preparation_revision",
            *values,
            "created_at",
            "updated_at",
        ]
        params = [
            vid,
            package["id"],
            number,
            "draft",
            live_preparation_revision,
            *values.values(),
            now,
            now,
        ]
        if previous is not None:
            columns[5:5] = ["forked_from_version_id", "forked_from_version_number"]
            params[5:5] = [previous["id"], previous["version_number"]]
        marks = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO application_package_versions({','.join(columns)}) VALUES({marks})",
            params,
        )
        if (
            previous is not None
            and previous["job_content_fingerprint"] != values["job_content_fingerprint"]
        ):
            connection.execute(
                "UPDATE application_package_versions SET job_reconfirmation_required_at=? WHERE id=?",
                (now, vid),
            )
        current_items = connection.execute(
            "SELECT id,kind,label,detail,required,fulfillment_state,deadline_at,"
            "sort_order,origin,capture_method,confirmed,created_at,updated_at "
            "FROM application_requirement_items WHERE application_id=? "
            "ORDER BY sort_order,id",
            (aid,),
        ).fetchall()
        for requirement in current_items:
            connection.execute(
                "INSERT INTO application_package_requirement_snapshots "
                "(package_version_id,requirement_id,kind,label,detail,required,"
                "fulfillment_state,deadline_at,sort_order,origin,capture_method,"
                "confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, *tuple(requirement)),
            )
        if previous is not None:
            old_items = {
                r["requirement_id"]: dict(r)
                for r in connection.execute(
                    "SELECT * FROM application_package_requirement_snapshots WHERE package_version_id=?",
                    (previous["id"],),
                )
            }
            old_answers = {
                str(row["requirement_id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM package_answers WHERE package_version_id=?",
                    (previous["id"],),
                )
            }
            old_selections = {
                str(row["requirement_id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM package_material_selections WHERE package_version_id=?",
                    (previous["id"],),
                )
            }
            selected_version_ids = sorted(
                {str(row["material_version_id"]) for row in old_selections.values()}
            )
            current_materials: dict[str, dict[str, Any]] = {}
            if selected_version_ids:
                placeholders = ",".join("?" for _ in selected_version_ids)
                current_materials = {
                    str(row["id"]): dict(row)
                    for row in connection.execute(
                        f"SELECT v.*,m.name,m.category FROM material_versions v JOIN materials m ON m.id=v.material_id WHERE v.id IN ({placeholders})",
                        selected_version_ids,
                    )
                }
            for current_row in current_items:
                current_item = {
                    "package_version_id": vid,
                    "requirement_id": current_row["id"],
                    **dict(current_row),
                }
                old_item = old_items.get(current_item["requirement_id"])
                if old_item is None:
                    continue
                copy_selection, copy_answer, copy_confirmation = (
                    requirement_compatibility(old_item, dict(current_item))
                )
                old_answer = old_answers.get(str(current_item["requirement_id"]))
                if copy_answer and old_answer is not None:
                    connection.execute(
                        "INSERT INTO package_answers(package_version_id,requirement_id,answer_text,origin,capture_method,confirmed,confirmed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            vid,
                            current_item["requirement_id"],
                            old_answer["answer_text"],
                            old_answer["origin"],
                            old_answer["capture_method"],
                            old_answer["confirmed"] if copy_confirmation else 0,
                            old_answer["confirmed_at"] if copy_confirmation else None,
                            now,
                            now,
                        ),
                    )
                old_selection = old_selections.get(str(current_item["requirement_id"]))
                if copy_selection and old_selection is not None:
                    current_material = current_materials.get(
                        str(old_selection["material_version_id"])
                    )
                    if current_material is None:
                        continue
                    connection.execute(
                        "INSERT INTO package_material_selections(package_version_id,requirement_id,material_version_id,material_id_snapshot,material_name_snapshot,material_category_snapshot,material_version_number_snapshot,display_filename_snapshot,declared_media_type_snapshot,byte_size_snapshot,sha256_snapshot,origin,capture_method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            vid,
                            current_item["requirement_id"],
                            current_material["id"],
                            current_material["material_id"],
                            current_material["name"],
                            current_material["category"],
                            current_material["version_number"],
                            current_material["display_filename"],
                            current_material["declared_media_type"],
                            current_material["byte_size"],
                            current_material["sha256"],
                            "manual",
                            "manual",
                            now,
                            now,
                        ),
                    )
        connection.execute(
            "UPDATE application_packages SET next_version_number=?,updated_at=? WHERE id=?",
            (number + 1, now, package["id"]),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM application_package_versions WHERE id=?", (vid,)
        ).fetchone()
        assert row is not None
        return dict(row), True
    except Exception:
        connection.rollback()
        raise


def set_material_selection(
    connection: sqlite3.Connection,
    version_id: str,
    requirement_id: str,
    material_version_id: str,
    *,
    package_revision: int,
    preparation_revision: int,
) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        _check_preparation_authority(connection, row, preparation_revision)
        req = connection.execute(
            "SELECT kind,fulfillment_state FROM application_package_requirement_snapshots WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).fetchone()
        if req is None:
            raise PackageError("package_requirement_not_found")
        if req["kind"] != "material" or req["fulfillment_state"] == "not_applicable":
            raise PackageError("package_requirement_incompatible")
        material = connection.execute(
            "SELECT v.*,m.name,m.category,m.status FROM material_versions v JOIN materials m ON m.id=v.material_id WHERE v.id=?",
            (material_version_id,),
        ).fetchone()
        if material is None:
            raise PackageError("material_version_not_found")
        if material["status"] != "active":
            raise PackageError("material_retired")
        old = connection.execute(
            "SELECT material_version_id FROM package_material_selections WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).fetchone()
        if old is not None and old[0] == material_version_id:
            connection.commit()
            return dict(row)
        now = _now()
        connection.execute(
            "INSERT INTO package_material_selections(package_version_id,requirement_id,material_version_id,material_id_snapshot,material_name_snapshot,material_category_snapshot,material_version_number_snapshot,display_filename_snapshot,declared_media_type_snapshot,byte_size_snapshot,sha256_snapshot,origin,capture_method,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(package_version_id,requirement_id) DO UPDATE SET material_version_id=excluded.material_version_id,material_id_snapshot=excluded.material_id_snapshot,material_name_snapshot=excluded.material_name_snapshot,material_category_snapshot=excluded.material_category_snapshot,material_version_number_snapshot=excluded.material_version_number_snapshot,display_filename_snapshot=excluded.display_filename_snapshot,declared_media_type_snapshot=excluded.declared_media_type_snapshot,byte_size_snapshot=excluded.byte_size_snapshot,sha256_snapshot=excluded.sha256_snapshot,updated_at=excluded.updated_at",
            (
                version_id,
                requirement_id,
                material_version_id,
                material["material_id"],
                material["name"],
                material["category"],
                material["version_number"],
                material["display_filename"],
                material["declared_media_type"],
                material["byte_size"],
                material["sha256"],
                "manual",
                "manual",
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE application_package_versions SET revision=revision+1,updated_at=? WHERE id=?",
            (now, version_id),
        )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def remove_material_selection(
    connection: sqlite3.Connection,
    version_id: str,
    requirement_id: str,
    *,
    package_revision: int,
    preparation_revision: int,
) -> dict[str, Any]:
    """Remove one draft selection; absent selections are idempotent."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        _check_preparation_authority(connection, row, preparation_revision)
        if (
            connection.execute(
                "SELECT 1 FROM application_package_requirement_snapshots WHERE package_version_id=? AND requirement_id=?",
                (version_id, requirement_id),
            ).fetchone()
            is None
        ):
            raise PackageError("package_requirement_not_found")
        deleted = connection.execute(
            "DELETE FROM package_material_selections WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).rowcount
        if deleted:
            now = _now()
            connection.execute(
                "UPDATE application_package_versions SET revision=revision+1,updated_at=? WHERE id=?",
                (now, version_id),
            )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def rebase(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    package_revision: int,
    preparation_revision: int,
) -> dict[str, Any]:
    """Synchronize a draft with current concrete job and M1 requirements."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        package = connection.execute(
            "SELECT application_id FROM application_packages WHERE id=?",
            (row["package_id"],),
        ).fetchone()
        if package is None:
            raise PackageError("package_version_not_found")
        live_prep = _prep_revision(connection, int(package["application_id"]))
        if live_prep != preparation_revision:
            raise PackageConflict("preparation_revision_conflict", live_prep)
        job = connection.execute(
            "SELECT * FROM jobs WHERE id=(SELECT job_id FROM applications WHERE id=?)",
            (package["application_id"],),
        ).fetchone()
        if job is None:
            raise PackageError("job_not_found")
        values = _snapshot_job(job)
        old_items = {
            r["requirement_id"]: dict(r)
            for r in connection.execute(
                "SELECT * FROM application_package_requirement_snapshots WHERE package_version_id=?",
                (version_id,),
            )
        }
        live_items = connection.execute(
            "SELECT id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at FROM application_requirement_items WHERE application_id=? ORDER BY sort_order,id",
            (package["application_id"],),
        ).fetchall()
        changed = (
            row["job_content_fingerprint"] != values["job_content_fingerprint"]
            or int(row["preparation_revision"]) != live_prep
            or len(old_items) != len(live_items)
        )
        for item in live_items:
            old = old_items.get(item[0])
            if old is None or any(
                old[k] != item[k]
                for k in (
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
                )
            ):
                changed = True
        if changed:
            now = _now()
            reconfirm = (
                now
                if row["job_content_fingerprint"] != values["job_content_fingerprint"]
                else row["job_reconfirmation_required_at"]
            )
            sets = (
                ",".join(f"{k}=?" for k in values)
                + ",preparation_revision=?,job_reconfirmation_required_at=?,revision=revision+1,updated_at=?"
            )
            connection.execute(
                f"UPDATE application_package_versions SET {sets} WHERE id=?",
                (*values.values(), live_prep, reconfirm, now, version_id),
            )
            # Reconcile children before replacing snapshot rows; deferred
            # sort-order staging avoids transient UNIQUE conflicts on reorder.
            connection.execute(
                "UPDATE application_package_requirement_snapshots SET sort_order=sort_order+1000000 WHERE package_version_id=?",
                (version_id,),
            )
            for rid in set(old_items) - {i[0] for i in live_items}:
                connection.execute(
                    "DELETE FROM application_package_requirement_snapshots WHERE package_version_id=? AND requirement_id=?",
                    (version_id, rid),
                )
            for item in live_items:
                old_item = old_items.get(item[0])
                if old_item is None:
                    connection.execute(
                        "INSERT INTO application_package_requirement_snapshots(package_version_id,requirement_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (version_id, *tuple(item)),
                    )
                    continue
                selection, answer, confirmation = requirement_compatibility(
                    old_item, dict(item)
                )
                if not selection:
                    connection.execute(
                        "DELETE FROM package_material_selections WHERE package_version_id=? AND requirement_id=?",
                        (version_id, item[0]),
                    )
                if not answer:
                    connection.execute(
                        "DELETE FROM package_answers WHERE package_version_id=? AND requirement_id=?",
                        (version_id, item[0]),
                    )
                elif not confirmation:
                    connection.execute(
                        "UPDATE package_answers SET confirmed=0,confirmed_at=NULL,updated_at=? WHERE package_version_id=? AND requirement_id=?",
                        (now, version_id, item[0]),
                    )
                connection.execute(
                    "UPDATE application_package_requirement_snapshots SET kind=?,label=?,detail=?,required=?,fulfillment_state=?,deadline_at=?,sort_order=?,origin=?,capture_method=?,confirmed=?,updated_at=? WHERE package_version_id=? AND requirement_id=?",
                    (
                        item[1],
                        item[2],
                        item[3],
                        item[4],
                        item[5],
                        item[6],
                        item[7],
                        item[8],
                        item[9],
                        item[10],
                        item[12],
                        version_id,
                        item[0],
                    ),
                )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def _authority_signature(authority: dict[str, Any]) -> tuple[Any, ...]:
    signature: list[tuple[tuple[str, Any], ...]] = []
    for section in ("version", "live_items", "snapshots", "selections", "answers"):
        rows = (
            [authority[section]]
            if isinstance(authority[section], dict)
            else authority[section]
        )
        for source in rows:
            row = dict(source)
            if section == "version":
                row.pop("live_updated_at", None)
            elif section == "selections":
                row.pop("live_material_name", None)
                row.pop("live_material_category", None)
            signature.append(tuple(row.items()))
    return tuple(signature)


def _finalization_error(result: dict[str, Any]) -> PackageError | None:
    codes = {reason["code"] for reason in result["reasons"]}
    if "selected_material_unavailable" in codes:
        return PackageError("selected_material_unavailable")
    if "preparation_revision_stale" in codes:
        return PackageError("package_preparation_stale")
    if "job_snapshot_stale" in codes:
        return PackageError("package_job_stale")
    if "job_reconfirmation_required" in codes:
        return PackageError("package_job_reconfirmation_required")
    if result["category"] != "complete":
        return PackageError("package_incomplete")
    return None


def _frozen_material_availability(
    selections: list[dict[str, Any]],
) -> dict[str, bool]:
    """Project frozen integrity without reopening bytes on an idempotent retry."""
    return {
        str(selection["material_version_id"]): _manifest_matches(selection)
        for selection in selections
    }


def _finalize_locked(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    package_revision: int,
    preparation_revision: int,
    preflight: dict[str, Any],
    handles: dict[str, VerifiedMaterialHandle],
    evaluated_at: datetime,
) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        authority = _load_authority(connection, version_id, include_live_items=None)
        row = authority["version"]
        if row["state"] == "finalized":
            if int(row["revision"]) != package_revision:
                raise PackageConflict("package_revision_conflict", int(row["revision"]))
            if int(row["preparation_revision"]) != preparation_revision:
                raise PackageConflict(
                    "preparation_revision_conflict", int(row["preparation_revision"])
                )
            reasons = [
                dict(reason)
                for reason in connection.execute(
                    "SELECT position,code,blocking,requirement_id FROM package_completeness_reasons WHERE package_version_id=? ORDER BY position",
                    (version_id,),
                )
            ]
            connection.commit()
            return _version_detail_projection(
                authority,
                availability=_frozen_material_availability(authority["selections"]),
                finalized_reasons=reasons,
            )
        if int(row["revision"]) != package_revision:
            raise PackageConflict("package_revision_conflict", int(row["revision"]))
        live_preparation_revision = int(row.get("live_preparation_revision") or 0)
        if live_preparation_revision != preparation_revision:
            raise PackageConflict(
                "preparation_revision_conflict", live_preparation_revision
            )
        if int(row["preparation_revision"]) != preparation_revision:
            raise PackageError("package_preparation_stale")
        availability = {
            material_version_id: handle.is_still_valid()
            for material_version_id, handle in handles.items()
        }
        selected_ids = {
            str(selection["material_version_id"])
            for selection in authority["selections"]
        }
        if selected_ids != set(handles):
            availability.update({key: False for key in selected_ids - set(handles)})
        result = _evaluate_authority(
            authority, availability=availability, now=evaluated_at
        )
        error = _finalization_error(result)
        if error is not None:
            raise error
        if _authority_signature(authority) != _authority_signature(preflight):
            raise PackageError("package_incomplete")
        if row["approved_revision"] is None:
            raise PackageError("package_approval_missing")
        if int(row["approved_revision"]) != package_revision:
            raise PackageError("package_approval_stale")
        now = _now()
        for position, reason in enumerate(result["reasons"]):
            connection.execute(
                "INSERT INTO package_completeness_reasons(package_version_id,position,code,blocking,requirement_id) VALUES(?,?,?,?,?)",
                (
                    version_id,
                    position,
                    reason["code"],
                    int(reason["blocking"]),
                    reason["requirement_id"],
                ),
            )
        connection.execute(
            "UPDATE application_package_versions SET state='finalized',finalized_at=?,final_completeness_category='complete',updated_at=? WHERE id=?",
            (now, now, version_id),
        )
        connection.commit()
        authority["version"].update(
            {
                "state": "finalized",
                "finalized_at": now,
                "final_completeness_category": "complete",
                "updated_at": now,
            }
        )
        return _version_detail_projection(
            authority,
            availability=availability,
            finalized_reasons=result["reasons"],
        )
    except Exception:
        connection.rollback()
        raise


def finalize(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    package_revision: int,
    preparation_revision: int,
    root: Path | None = None,
    database_path: Path | None = None,
    _after_preflight: Any = None,
) -> dict[str, Any]:
    """Hash once outside coordination, then freeze under lock and transaction."""
    preflight = _load_authority(connection, version_id, include_live_items=None)
    row = preflight["version"]
    if row["state"] == "finalized":
        if int(row["revision"]) != package_revision:
            raise PackageConflict("package_revision_conflict", int(row["revision"]))
        if int(row["preparation_revision"]) != preparation_revision:
            raise PackageConflict(
                "preparation_revision_conflict", int(row["preparation_revision"])
            )
        reasons = [
            dict(reason)
            for reason in connection.execute(
                "SELECT position,code,blocking,requirement_id FROM package_completeness_reasons WHERE package_version_id=? ORDER BY position",
                (version_id,),
            )
        ]
        return _version_detail_projection(
            preflight,
            availability=_frozen_material_availability(preflight["selections"]),
            finalized_reasons=reasons,
        )
    if int(row["revision"]) != package_revision:
        raise PackageConflict("package_revision_conflict", int(row["revision"]))
    live_preparation_revision = int(row.get("live_preparation_revision") or 0)
    if live_preparation_revision != preparation_revision:
        raise PackageConflict(
            "preparation_revision_conflict", live_preparation_revision
        )
    if int(row["preparation_revision"]) != preparation_revision:
        raise PackageError("package_preparation_stale")

    handles: dict[str, VerifiedMaterialHandle] = {}
    evaluated_at = datetime.now(UTC)
    try:
        for selection in preflight["selections"]:
            material_version_id = str(selection["material_version_id"])
            if material_version_id in handles:
                continue
            handle = (
                _open_verified_material_metadata(
                    root,
                    material_version_id=material_version_id,
                    store_id=str(selection["live_store_id"]),
                    storage_key=str(selection["live_storage_key"]),
                    byte_size=int(selection["live_byte_size"]),
                    sha256=str(selection["live_sha256"]),
                )
                if root is not None
                and _manifest_matches(selection)
                and selection.get("live_store_id") is not None
                else None
            )
            if handle is not None:
                handles[material_version_id] = handle
        availability = {
            str(selection["material_version_id"]): str(selection["material_version_id"])
            in handles
            for selection in preflight["selections"]
        }
        result = _evaluate_authority(
            preflight, availability=availability, now=evaluated_at
        )
        error = _finalization_error(result)
        if error is not None:
            raise error
        if row["approved_revision"] is None:
            raise PackageError("package_approval_missing")
        if int(row["approved_revision"]) != package_revision:
            raise PackageError("package_approval_stale")
        if _after_preflight is not None:
            _after_preflight()

        def freeze() -> dict[str, Any]:
            return _finalize_locked(
                connection,
                version_id,
                package_revision=package_revision,
                preparation_revision=preparation_revision,
                preflight=preflight,
                handles=handles,
                evaluated_at=evaluated_at,
            )

        if database_path is None:
            return freeze()
        with collection_lock(database_path, command="application package finalize"):
            return freeze()
    finally:
        for handle in handles.values():
            handle.close()


def confirm_answer(
    connection: sqlite3.Connection,
    version_id: str,
    requirement_id: str,
    *,
    confirmed: bool,
    package_revision: int,
    preparation_revision: int,
) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        _check_preparation_authority(connection, row, preparation_revision)
        requirement = connection.execute(
            "SELECT kind FROM application_package_requirement_snapshots WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).fetchone()
        if requirement is None:
            raise PackageError("package_requirement_not_found")
        if requirement["kind"] not in ("question", "link", "other"):
            raise PackageError("package_requirement_incompatible")
        answer = connection.execute(
            "SELECT * FROM package_answers WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).fetchone()
        if answer is None and confirmed:
            raise PackageError("package_answer_missing")
        if answer is None or int(answer["confirmed"]) == int(confirmed):
            connection.commit()
            return dict(row)
        now = _now()
        connection.execute(
            "UPDATE package_answers SET confirmed=?,confirmed_at=?,updated_at=? WHERE package_version_id=? AND requirement_id=?",
            (
                int(confirmed),
                now if confirmed else None,
                now,
                version_id,
                requirement_id,
            ),
        )
        connection.execute(
            "UPDATE application_package_versions SET revision=revision+1,updated_at=? WHERE id=?",
            (now, version_id),
        )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def set_approval(
    connection: sqlite3.Connection,
    version_id: str,
    *,
    approved: bool,
    package_revision: int,
    preparation_revision: int,
    root: Path | None = None,
) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        _check_preparation_authority(connection, row, preparation_revision)
        if approved:
            result = completeness(connection, version_id, root=root)
            error = _finalization_error(result)
            if error is not None:
                raise error
        now = _now()
        if approved and row["approved_revision"] != package_revision:
            connection.execute(
                "UPDATE application_package_versions SET approved_revision=?,approved_at=?,updated_at=? WHERE id=?",
                (package_revision, now, now, version_id),
            )
        elif not approved and row["approved_revision"] is not None:
            connection.execute(
                "UPDATE application_package_versions SET approved_revision=NULL,approved_at=NULL,updated_at=? WHERE id=?",
                (now, version_id),
            )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def delete_version(
    connection: sqlite3.Connection, version_id: str, *, package_revision: int
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT package_id,revision FROM application_package_versions WHERE id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise PackageError("package_version_not_found")
        if int(row["revision"]) != package_revision:
            raise PackageConflict("package_revision_conflict", int(row["revision"]))
        package_id = row["package_id"]
        connection.execute(
            "DELETE FROM application_package_versions WHERE id=?", (version_id,)
        )
        if (
            connection.execute(
                "SELECT 1 FROM application_package_versions WHERE package_id=?",
                (package_id,),
            ).fetchone()
            is None
        ):
            connection.execute(
                "DELETE FROM application_packages WHERE id=?", (package_id,)
            )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise PackageError("package_version_referenced") from error
    except Exception:
        connection.rollback()
        raise


def _prep_revision(connection: sqlite3.Connection, application_id: int) -> int:
    row = connection.execute(
        "SELECT revision FROM application_preparations WHERE application_id=?",
        (application_id,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _draft(
    connection: sqlite3.Connection, version_id: str, package_revision: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
    ).fetchone()
    if row is None:
        raise PackageError("package_version_not_found")
    if row["state"] != "draft":
        raise PackageError("package_version_immutable")
    if int(row["revision"]) != package_revision:
        raise PackageConflict("package_revision_conflict", int(row["revision"]))
    return cast(sqlite3.Row, row)


def _check_preparation_authority(
    connection: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    expected_revision: int,
) -> None:
    package = connection.execute(
        "SELECT application_id FROM application_packages WHERE id=?",
        (row["package_id"],),
    ).fetchone()
    if package is None:
        raise PackageError("package_version_not_found")
    live_revision = _prep_revision(connection, int(package["application_id"]))
    if live_revision != expected_revision:
        raise PackageConflict("preparation_revision_conflict", live_revision)
    if int(row["preparation_revision"]) != live_revision:
        raise PackageError("package_preparation_stale")


def set_answer(
    connection: sqlite3.Connection,
    version_id: str,
    requirement_id: str,
    text: str,
    *,
    package_revision: int,
    preparation_revision: int,
) -> dict[str, Any]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if (
        not text.strip()
        or len(text) > 10_000
        or any(ord(char) < 32 and char not in "\t\n" for char in text)
    ):
        raise PackageError("invalid_package_answer")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        _check_preparation_authority(connection, row, preparation_revision)
        req = connection.execute(
            "SELECT kind FROM application_package_requirement_snapshots WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).fetchone()
        if req is None:
            raise PackageError("package_requirement_not_found")
        if req["kind"] not in ("question", "link", "other"):
            raise PackageError("package_requirement_incompatible")
        if req["kind"] == "link":
            parsed = urllib.parse.urlsplit(text)
            if (
                len(text) > 2_048
                or parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise PackageError("invalid_package_answer")
        old = connection.execute(
            "SELECT answer_text,confirmed FROM package_answers WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).fetchone()
        if old is not None and old["answer_text"] == text:
            connection.commit()
            return dict(row)
        now = _now()
        connection.execute(
            "INSERT INTO package_answers(package_version_id,requirement_id,answer_text,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,'manual','manual',0,?,?) ON CONFLICT(package_version_id,requirement_id) DO UPDATE SET answer_text=excluded.answer_text,confirmed=0,confirmed_at=NULL,updated_at=excluded.updated_at",
            (version_id, requirement_id, text, now, now),
        )
        connection.execute(
            "UPDATE application_package_versions SET revision=revision+1,updated_at=? WHERE id=?",
            (now, version_id),
        )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def remove_answer(
    connection: sqlite3.Connection,
    version_id: str,
    requirement_id: str,
    *,
    package_revision: int,
    preparation_revision: int,
) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _draft(connection, version_id, package_revision)
        _check_preparation_authority(connection, row, preparation_revision)
        if (
            connection.execute(
                "SELECT 1 FROM application_package_requirement_snapshots WHERE package_version_id=? AND requirement_id=?",
                (version_id, requirement_id),
            ).fetchone()
            is None
        ):
            raise PackageError("package_requirement_not_found")
        if connection.execute(
            "DELETE FROM package_answers WHERE package_version_id=? AND requirement_id=?",
            (version_id, requirement_id),
        ).rowcount:
            now = _now()
            connection.execute(
                "UPDATE application_package_versions SET revision=revision+1,updated_at=? WHERE id=?",
                (now, version_id),
            )
        connection.commit()
        return dict(
            connection.execute(
                "SELECT * FROM application_package_versions WHERE id=?", (version_id,)
            ).fetchone()
        )
    except Exception:
        connection.rollback()
        raise


def _approval_projection(version: dict[str, Any]) -> dict[str, Any]:
    approved_revision = version.get("approved_revision")
    status = (
        "not_approved"
        if approved_revision is None
        else (
            "current" if int(approved_revision) == int(version["revision"]) else "stale"
        )
    )
    return {
        "status": status,
        "approved_revision": approved_revision,
        "approved_at": version.get("approved_at"),
    }


def _job_snapshot_projection(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": version["job_id_snapshot"],
        "source": version["job_source"],
        "source_job_id": version["job_source_job_id"],
        "title": version["job_title"],
        "company": version["job_company"],
        "location": version["job_location"],
        "url": version["job_url"],
        "employment_type": version["job_employment_type"],
        "remote_status": version["job_remote_status"],
        "published_at": version["job_published_at"],
        "updated_at": version["job_updated_at_snapshot"],
        "description_present": bool(version["job_description_present"]),
    }


def _version_detail_projection(
    authority: dict[str, Any],
    *,
    availability: dict[str, bool],
    finalized_reasons: list[dict[str, Any]] | None = None,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Project already-authoritative state without another database/hash pass."""
    version = authority["version"]
    selections = []
    for selected in authority["selections"]:
        current_availability = (
            "retired"
            if selected.get("live_material_status") == "retired"
            else (
                "available"
                if _manifest_matches(selected)
                and availability.get(str(selected["material_version_id"]), False)
                else "unavailable"
            )
        )
        selections.append(
            {
                "requirement_id": selected["requirement_id"],
                "material_version_id": selected["material_version_id"],
                "material_id": selected["material_id_snapshot"],
                "material_name": selected["material_name_snapshot"],
                "material_category": selected["material_category_snapshot"],
                "version_number": selected["material_version_number_snapshot"],
                "display_filename": selected["display_filename_snapshot"],
                "declared_media_type": selected["declared_media_type_snapshot"],
                "byte_size": selected["byte_size_snapshot"],
                "origin": selected["origin"],
                "capture_method": selected["capture_method"],
                "current_availability": current_availability,
                "created_at": selected["created_at"],
                "updated_at": selected["updated_at"],
            }
        )
    common = {
        "package_id": version["package_id"],
        "application_id": version["application_id"],
        "job_id": version["owner_job_id"],
        "version_id": version["id"],
        "version_number": version["version_number"],
        "state": version["state"],
        "revision": version["revision"],
        "preparation_revision": version["preparation_revision"],
        "forked_from": (
            None
            if version["forked_from_version_id"] is None
            else {
                "version_id": version["forked_from_version_id"],
                "version_number": version["forked_from_version_number"],
            }
        ),
        "job_reconfirmation_required_at": version["job_reconfirmation_required_at"],
        "created_at": version["created_at"],
        "updated_at": version["updated_at"],
        "job_snapshot": _job_snapshot_projection(version),
        "approval": _approval_projection(version),
        "requirements": [
            {
                key: value
                for key, value in requirement.items()
                if key != "package_version_id"
            }
            for requirement in authority["snapshots"]
        ],
        "material_selections": selections,
        "answers": [
            {
                "requirement_id": answer["requirement_id"],
                "text": answer["answer_text"],
                "origin": answer["origin"],
                "capture_method": answer["capture_method"],
                "confirmed": bool(answer["confirmed"]),
                "confirmed_at": answer["confirmed_at"],
                "created_at": answer["created_at"],
                "updated_at": answer["updated_at"],
            }
            for answer in authority["answers"]
        ],
    }
    if version["state"] == "finalized":
        if finalized_reasons is None:
            raise ValueError("finalized reasons are required")
        common.update(
            {
                "finalized_at": version["finalized_at"],
                "completeness": {
                    "category": "complete",
                    "blocking_count": 0,
                    "evaluated_at": version["finalized_at"],
                    "reasons": [
                        {
                            **dict(reason),
                            "blocking": bool(reason["blocking"]),
                            "message": REASON_MESSAGES[reason["code"]],
                        }
                        for reason in finalized_reasons
                    ],
                },
            }
        )
        return common
    live = _evaluate_authority(
        authority, availability=availability, now=evaluated_at or datetime.now(UTC)
    )
    codes = {reason["code"] for reason in live["reasons"]}
    common.update(
        {
            "preparation_stale": "preparation_revision_stale" in codes,
            "job_stale": "job_snapshot_stale" in codes,
            "job_reconfirmation_required": "job_reconfirmation_required" in codes,
            "completeness": live,
        }
    )
    return common


def version_detail(
    connection: sqlite3.Connection, version_id: str, *, root: Path | None
) -> dict[str, Any]:
    """Return the exact explicit detail projection in at most five SELECTs."""
    authority = _load_authority(
        connection,
        version_id,
        include_live_items=None,
    )
    availability = _verify_distinct_materials(connection, root, authority["selections"])
    reasons = None
    if authority["version"]["state"] == "finalized":
        reasons = [
            dict(reason)
            for reason in connection.execute(
                "SELECT position,code,blocking,requirement_id FROM package_completeness_reasons WHERE package_version_id=? ORDER BY position",
                (version_id,),
            )
        ]
    return _version_detail_projection(
        authority, availability=availability, finalized_reasons=reasons
    )


def package_summary(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    aid, _job = _application(connection, job_id)
    package = connection.execute(
        "SELECT * FROM application_packages WHERE application_id=?", (aid,)
    ).fetchone()
    if package is None:
        return {
            "application_id": aid,
            "job_id": job_id,
            "package_id": None,
            "draft": None,
            "history": {"items": [], "total": 0, "offset": offset, "limit": limit},
        }
    draft = connection.execute(
        """SELECT v.*,j.source AS live_source,j.source_job_id AS live_source_job_id,
                  j.title AS live_title,j.company AS live_company,
                  j.location AS live_location,j.description AS live_description,
                  j.url AS live_url,j.employment_type AS live_employment_type,
                  j.remote_status AS live_remote_status,j.published_at AS live_published_at,
                  prep.revision AS live_preparation_revision,
                  prep.requirements_reviewed_revision,prep.requirements_reviewed_at,
                  prep.ready_confirmed_revision,prep.ready_confirmed_at
           FROM application_package_versions v
           JOIN applications a ON a.id=? LEFT JOIN jobs j ON j.id=a.job_id
           LEFT JOIN application_preparations prep ON prep.application_id=a.id
           WHERE v.package_id=? AND v.state='draft'""",
        (aid, package["id"]),
    ).fetchone()
    draft_projection = None
    if draft is not None:
        current_fingerprint = job_content_fingerprint(
            {
                "source": draft["live_source"],
                "source_job_id": draft["live_source_job_id"],
                "title": draft["live_title"],
                "company": draft["live_company"],
                "location": draft["live_location"],
                "description": draft["live_description"],
                "url": draft["live_url"],
                "employment_type": draft["live_employment_type"],
                "remote_status": draft["live_remote_status"],
                "published_at": draft["live_published_at"],
            }
        )
        reconfirm = _aware(draft["job_reconfirmation_required_at"])
        reviewed = _aware(draft["requirements_reviewed_at"])
        ready = _aware(draft["ready_confirmed_at"])
        live_revision = int(draft["live_preparation_revision"] or 0)
        reconfirm_required = bool(
            draft["job_reconfirmation_required_at"] is not None
            and (
                reconfirm is None
                or reviewed is None
                or ready is None
                or reviewed <= reconfirm
                or ready <= reconfirm
                or draft["requirements_reviewed_revision"] != live_revision
                or draft["ready_confirmed_revision"] != live_revision
            )
        )
        draft_projection = {
            "version_id": draft["id"],
            "version_number": draft["version_number"],
            "revision": draft["revision"],
            "preparation_revision": draft["preparation_revision"],
            "preparation_stale": live_revision != draft["preparation_revision"],
            "job_stale": current_fingerprint != draft["job_content_fingerprint"],
            "job_reconfirmation_required": reconfirm_required,
            "approval_status": _approval_projection(dict(draft))["status"],
            "created_at": draft["created_at"],
            "updated_at": draft["updated_at"],
        }
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM application_package_versions WHERE package_id=? AND state='finalized'",
            (package["id"],),
        ).fetchone()[0]
    )
    history = [
        {
            "version_id": row["id"],
            "version_number": row["version_number"],
            "revision": row["revision"],
            "preparation_revision": row["preparation_revision"],
            "approved_at": row["approved_at"],
            "finalized_at": row["finalized_at"],
        }
        for row in connection.execute(
            "SELECT id,version_number,revision,preparation_revision,approved_at,finalized_at FROM application_package_versions WHERE package_id=? AND state='finalized' ORDER BY version_number DESC LIMIT ? OFFSET ?",
            (package["id"], limit, offset),
        )
    ]
    return {
        "application_id": aid,
        "job_id": job_id,
        "package_id": package["id"],
        "draft": draft_projection,
        "history": {
            "items": history,
            "total": total,
            "offset": offset,
            "limit": limit,
        },
    }


def package_lifecycle_for_applications(
    connection: sqlite3.Connection, application_ids: list[int]
) -> dict[int, dict[str, Any] | None]:
    result: dict[int, dict[str, Any] | None] = {
        application_id: None for application_id in application_ids
    }
    if not application_ids:
        return result
    marks = ",".join("?" for _ in application_ids)
    rows = connection.execute(
        f"""SELECT p.application_id,
                   MAX(CASE WHEN v.state='draft' THEN 1 ELSE 0 END) AS has_draft,
                   MAX(CASE WHEN v.state='draft' AND v.approved_revision=v.revision THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN v.state='finalized' THEN 1 ELSE 0 END) AS finalized_count,
                   MAX(CASE WHEN v.state='finalized' THEN v.finalized_at END) AS latest_finalized_at
            FROM application_packages p
            LEFT JOIN application_package_versions v ON v.package_id=p.id
            WHERE p.application_id IN ({marks}) GROUP BY p.application_id""",
        application_ids,
    )
    for row in rows:
        result[int(row["application_id"])] = {
            "has_draft": bool(row["has_draft"]),
            "draft_approved_for_current_revision": bool(row["approved"]),
            "finalized_count": int(row["finalized_count"] or 0),
            "latest_finalized_at": row["latest_finalized_at"],
        }
    return result
