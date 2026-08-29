"""Application preparation requirements and deterministic readiness."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast

KINDS = ("material", "link", "question", "task", "deadline", "other")
FULFILLMENT = ("open", "satisfied", "not_applicable")


class PreparationError(ValueError):
    pass


class RevisionConflict(PreparationError):
    def __init__(self, message: str, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreparationError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise PreparationError("timestamp requires explicit offset")
    return _ts(parsed)


def _application_id(connection: sqlite3.Connection, job_id: int) -> int | None:
    row = connection.execute(
        "SELECT id FROM applications WHERE job_id=?", (job_id,)
    ).fetchone()
    return None if row is None else int(row[0])


def _require_application_id(connection: sqlite3.Connection, job_id: int) -> int:
    if (
        connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone()
        is None
    ):
        raise PreparationError("job not found")
    aid = _application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    return aid


def _prep(
    connection: sqlite3.Connection, application_id: int, *, create: bool = False
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM application_preparations WHERE application_id=?",
        (application_id,),
    ).fetchone()
    if row is None and create:
        now = _ts(_now())
        connection.execute(
            "INSERT INTO application_preparations(application_id,created_at,"
            "updated_at) VALUES (?,?,?)",
            (application_id, now, now),
        )
        row = connection.execute(
            "SELECT * FROM application_preparations WHERE application_id=?",
            (application_id,),
        ).fetchone()
    return cast(sqlite3.Row | None, row)


def get_preparation(
    connection: sqlite3.Connection, job_id: int
) -> dict[str, Any] | None:
    aid = _application_id(connection, job_id)
    if aid is None:
        return None
    row = _prep(connection, aid)
    if row is None:
        return {
            "application_id": aid,
            "revision": 0,
            "items": [],
            "requirements_reviewed_revision": None,
            "requirements_reviewed_at": None,
            "ready_confirmed_revision": None,
            "ready_confirmed_at": None,
            "created_at": None,
            "updated_at": None,
        }
    items = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM application_requirement_items WHERE application_id=? "
            "ORDER BY sort_order",
            (aid,),
        )
    ]
    result = dict(row)
    result["items"] = items
    return result


def _write_begin(
    connection: sqlite3.Connection, aid: int, expected_revision: int
) -> sqlite3.Row:
    connection.execute("BEGIN IMMEDIATE")
    row = _prep(connection, aid, create=True)
    assert row is not None
    if row is None:
        connection.rollback()
        raise PreparationError("preparation unavailable")
    if int(row["revision"]) != expected_revision:
        connection.rollback()
        raise RevisionConflict(
            "preparation revision conflict", current_revision=int(row["revision"])
        )
    return row


def add_requirement(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    expected_revision: int,
    kind: str,
    label: str,
    detail: str | None = None,
    required: bool = True,
    fulfillment_state: str = "open",
    deadline_at: str | None = None,
) -> dict[str, Any]:
    aid = _require_application_id(connection, job_id)
    if (
        kind not in KINDS
        or fulfillment_state not in FULFILLMENT
        or (kind == "deadline" and fulfillment_state == "satisfied")
    ):
        raise PreparationError("invalid requirement state")
    label = label.strip()
    if not label or len(label) > 160:
        raise PreparationError("invalid requirement label")
    if detail is not None and (not detail.strip() or len(detail) > 1000):
        raise PreparationError("invalid requirement detail")
    deadline = parse_timestamp(deadline_at) if deadline_at is not None else None
    row = _write_begin(connection, aid, expected_revision)
    count = connection.execute(
        "SELECT count(*) FROM application_requirement_items WHERE application_id=?",
        (aid,),
    ).fetchone()[0]
    if count >= 200:
        connection.rollback()
        raise PreparationError("maximum requirements reached")
    now = _ts(_now())
    order = connection.execute(
        "SELECT coalesce(max(sort_order)+1,0) FROM "
        "application_requirement_items WHERE application_id=?",
        (aid,),
    ).fetchone()[0]
    for _ in range(3):
        rid = secrets.token_hex(16)
        try:
            connection.execute(
                "INSERT INTO application_requirement_items "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    aid,
                    kind,
                    label,
                    detail,
                    int(required),
                    fulfillment_state,
                    deadline,
                    order,
                    "manual",
                    "manual",
                    0,
                    now,
                    now,
                ),
            )
            break
        except sqlite3.IntegrityError as exc:
            if _ == 2:
                connection.rollback()
                raise PreparationError("requirement ID collision") from exc
    newrev = int(row["revision"]) + 1
    connection.execute(
        "UPDATE application_preparations SET revision=?,updated_at=? "
        "WHERE application_id=?",
        (newrev, now, aid),
    )
    connection.commit()
    return get_preparation(connection, job_id) or {}


def _item(connection: sqlite3.Connection, aid: int, item_id: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        connection.execute(
            "SELECT * FROM application_requirement_items WHERE id=? "
            "AND application_id=?",
            (item_id, aid),
        ).fetchone(),
    )


def update_requirement(
    connection: sqlite3.Connection,
    job_id: int,
    item_id: str,
    *,
    expected_revision: int,
    **changes: Any,
) -> dict[str, Any]:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    _write_begin(connection, aid, expected_revision)
    item = _item(connection, aid, item_id)
    if item is None:
        connection.rollback()
        raise PreparationError("requirement not found")
    allowed = {
        "kind",
        "label",
        "detail",
        "required",
        "fulfillment_state",
        "deadline_at",
        "confirmed",
    }
    values = {key: value for key, value in changes.items() if key in allowed}
    if "label" in values:
        values["label"] = str(values["label"]).strip()
        if not values["label"] or len(values["label"]) > 160:
            connection.rollback()
            raise PreparationError("invalid requirement label")
    if "detail" in values and values["detail"] is not None:
        values["detail"] = str(values["detail"]).strip()
        if not values["detail"] or len(values["detail"]) > 1000:
            connection.rollback()
            raise PreparationError("invalid requirement detail")
    if "kind" in values and values["kind"] not in KINDS:
        connection.rollback()
        raise PreparationError("invalid requirement kind")
    if "fulfillment_state" in values and values["fulfillment_state"] not in FULFILLMENT:
        connection.rollback()
        raise PreparationError("invalid fulfillment state")
    kind = values.get("kind", item["kind"])
    state = values.get("fulfillment_state", item["fulfillment_state"])
    if kind == "deadline" and state == "satisfied":
        connection.rollback()
        raise PreparationError("deadline cannot be satisfied")
    if "deadline_at" in values and values["deadline_at"] is not None:
        values["deadline_at"] = parse_timestamp(values["deadline_at"])
    changed = any(values.get(k, item[k]) != item[k] for k in values)
    if changed:
        now = _ts(_now())
        assignments = ", ".join(f"{k}=?" for k in values) + ", updated_at=?"
        connection.execute(
            f"UPDATE application_requirement_items SET {assignments} WHERE id=?",
            (*values.values(), now, item_id),
        )
        connection.execute(
            "UPDATE application_preparations SET revision=revision+1,updated_at=? "
            "WHERE application_id=?",
            (now, aid),
        )
    connection.commit()
    return get_preparation(connection, job_id) or {}


def confirm_requirement(
    connection: sqlite3.Connection, job_id: int, item_id: str, *, expected_revision: int
) -> dict[str, Any]:
    """Confirm one manually captured requirement, incrementing revision once."""
    return update_requirement(
        connection,
        job_id,
        item_id,
        expected_revision=expected_revision,
        confirmed=True,
    )


def delete_requirement(
    connection: sqlite3.Connection, job_id: int, item_id: str, *, expected_revision: int
) -> dict[str, Any]:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    _write_begin(connection, aid, expected_revision)
    if _item(connection, aid, item_id) is None:
        connection.rollback()
        raise PreparationError("requirement not found")
    item = _item(connection, aid, item_id)
    assert item is not None
    old_order = int(item["sort_order"])
    connection.execute(
        "DELETE FROM application_requirement_items WHERE id=?", (item_id,)
    )
    connection.execute(
        "UPDATE application_requirement_items SET sort_order=sort_order-1 "
        "WHERE application_id=? AND sort_order > ?",
        (aid, old_order),
    )
    now = _ts(_now())
    connection.execute(
        "UPDATE application_preparations SET revision=revision+1,updated_at=? "
        "WHERE application_id=?",
        (now, aid),
    )
    connection.commit()
    return get_preparation(connection, job_id) or {}


def reorder_requirements(
    connection: sqlite3.Connection,
    job_id: int,
    ids: list[str],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    _write_begin(connection, aid, expected_revision)
    current = [
        r[0]
        for r in connection.execute(
            "SELECT id FROM application_requirement_items WHERE application_id=? "
            "ORDER BY sort_order",
            (aid,),
        )
    ]
    if len(ids) != len(set(ids)) or set(ids) != set(current):
        connection.rollback()
        raise PreparationError("invalid requirement order")
    changed = ids != current
    if changed:
        connection.execute(
            "UPDATE application_requirement_items SET sort_order=sort_order+1000 "
            "WHERE application_id=?",
            (aid,),
        )
        for index, item_id in enumerate(ids):
            connection.execute(
                "UPDATE application_requirement_items SET sort_order=? WHERE id=?",
                (index, item_id),
            )
        now = _ts(_now())
        connection.execute(
            "UPDATE application_preparations SET revision=revision+1,updated_at=? "
            "WHERE application_id=?",
            (now, aid),
        )
    connection.commit()
    return get_preparation(connection, job_id) or {}


def clear_ready(
    connection: sqlite3.Connection, job_id: int, expected_revision: int | None = None
) -> None:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    connection.execute("BEGIN IMMEDIATE")
    row = _prep(connection, aid, create=True)
    assert row is not None
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        connection.rollback()
        raise RevisionConflict(
            "preparation revision conflict", current_revision=int(row["revision"])
        )
    if row["ready_confirmed_revision"] is None:
        connection.commit()
        return
    connection.execute(
        "UPDATE application_preparations SET ready_confirmed_revision=NULL,"
        "ready_confirmed_at=NULL WHERE application_id=?",
        (aid,),
    )
    connection.commit()


def clear_review(
    connection: sqlite3.Connection, job_id: int, expected_revision: int | None = None
) -> None:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    connection.execute("BEGIN IMMEDIATE")
    row = _prep(connection, aid, create=True)
    assert row is not None
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        connection.rollback()
        raise RevisionConflict(
            "preparation revision conflict", current_revision=int(row["revision"])
        )
    if (
        row["requirements_reviewed_revision"] is None
        and row["requirements_reviewed_at"] is None
    ):
        connection.commit()
        return
    now = _ts(_now())
    connection.execute(
        "UPDATE application_preparations SET requirements_reviewed_revision=NULL,"
        "requirements_reviewed_at=NULL,ready_confirmed_revision=NULL,"
        "ready_confirmed_at=NULL,updated_at=? WHERE application_id=?",
        (now, aid),
    )
    connection.commit()


def mark_reviewed(
    connection: sqlite3.Connection, job_id: int, expected_revision: int
) -> None:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    row = _write_begin(connection, aid, expected_revision)
    if connection.execute(
        "SELECT 1 FROM application_requirement_items WHERE application_id=? "
        "AND confirmed=0",
        (aid,),
    ).fetchone():
        connection.rollback()
        raise PreparationError("requirements have unconfirmed items")
    if row["requirements_reviewed_revision"] == expected_revision:
        connection.commit()
        return
    now = _ts(_now())
    connection.execute(
        "UPDATE application_preparations SET requirements_reviewed_revision=?,"
        "requirements_reviewed_at=?,updated_at=? WHERE application_id=?",
        (expected_revision, now, now, aid),
    )
    connection.commit()


def confirm_ready(
    connection: sqlite3.Connection, job_id: int, expected_revision: int
) -> None:
    aid = _require_application_id(connection, job_id)
    if aid is None:
        raise PreparationError("application not tracked")
    row = _write_begin(connection, aid, expected_revision)
    if row["ready_confirmed_revision"] == expected_revision:
        connection.commit()
        return
    if row["requirements_reviewed_revision"] is None:
        connection.rollback()
        raise PreparationError("requirements not reviewed")
    if row["requirements_reviewed_revision"] != expected_revision:
        connection.rollback()
        raise PreparationError("requirements review stale")
    state = readiness(connection, job_id, now=_now())
    if state["category"] != "requirements_satisfied":
        connection.rollback()
        raise PreparationError("preparation not ready")
    now = _ts(_now())
    connection.execute(
        "UPDATE application_preparations SET ready_confirmed_revision=?,"
        "ready_confirmed_at=?,updated_at=? WHERE application_id=?",
        (expected_revision, now, now, aid),
    )
    connection.commit()


def readiness(
    connection: sqlite3.Connection, job_id: int, *, now: datetime | None = None
) -> dict[str, Any]:
    snapshot = get_preparation(connection, job_id)
    if snapshot is None:
        return {
            "category": "unknown",
            "reasons": [
                {
                    "code": "job_information_unavailable",
                    "message": "CareerOS lacks usable stored job information.",
                }
            ],
        }
    now = now or _now()
    job = connection.execute(
        "SELECT description FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    rev = snapshot["revision"]
    reviewed = snapshot.get("requirements_reviewed_revision")
    reasons = []
    if job is None or not (job["description"] or "").strip():
        reasons.append(
            {
                "code": "job_information_unavailable",
                "message": "CareerOS lacks usable stored job information.",
            }
        )
    if reviewed is None:
        reasons.append(
            {
                "code": "requirements_not_reviewed",
                "message": "Requirements have not been reviewed.",
            }
        )
    elif reviewed != rev:
        reasons.append(
            {
                "code": "requirements_review_stale",
                "message": "Requirements review is stale.",
            }
        )
    unconfirmed: list[dict[str, str]] = []
    deadlines: list[dict[str, str]] = []
    open_requirements: list[dict[str, str]] = []
    for item in snapshot["items"]:
        if not item["confirmed"]:
            unconfirmed.append(
                {
                    "code": "requirement_unconfirmed",
                    "message": "A requirement is unconfirmed.",
                }
            )
        if item["required"] and item["fulfillment_state"] == "open":
            if (
                item["kind"] == "deadline"
                and item["deadline_at"]
                and datetime.fromisoformat(item["deadline_at"]) <= now
            ):
                deadlines.append(
                    {
                        "code": "required_deadline_passed",
                        "message": "A required deadline has passed.",
                    }
                )
            elif item["kind"] != "deadline":
                open_requirements.append(
                    {
                        "code": "required_requirement_open",
                        "message": "A required requirement remains open.",
                    }
                )
    reasons.extend(unconfirmed)
    reasons.extend(deadlines)
    reasons.extend(open_requirements)
    blockers = [
        reason for reason in reasons if reason["code"] != "job_information_unavailable"
    ]
    if blockers:
        return {
            "category": "unknown" if reviewed != rev else "incomplete",
            "reasons": reasons,
        }
    if (
        snapshot.get("ready_confirmed_revision") is not None
        and snapshot.get("ready_confirmed_revision") != rev
    ):
        reasons.extend(
            [
                {
                    "code": "requirements_satisfied",
                    "message": "All captured requirements are satisfied.",
                },
                {
                    "code": "ready_confirmation_stale",
                    "message": "Ready confirmation is stale.",
                },
            ]
        )
        return {"category": "requirements_satisfied", "reasons": reasons}
    if snapshot.get("ready_confirmed_revision") == rev:
        return {
            "category": "user_confirmed_ready",
            "reasons": reasons
            + [
                {
                    "code": "user_confirmed_current_revision",
                    "message": "You confirmed readiness for this revision.",
                }
            ],
        }
    return {
        "category": "requirements_satisfied",
        "reasons": reasons
        + [
            {
                "code": "requirements_satisfied",
                "message": "All captured requirements are satisfied.",
            }
        ],
    }
