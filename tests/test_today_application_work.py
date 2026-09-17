import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from careeros.api.main import create_app
from careeros.application_packages import service as package_service
from careeros.application_packages.service import delete_version, open_draft
from careeros.database.connection import connect
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.database.today import list_today_applications
from careeros.today.models import TodayApplication
from careeros.today.service import build_today
from tests.support import TestClient

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    return connection


def _application(
    connection: sqlite3.Connection,
    status: str,
    *,
    updated_at: str = "2026-08-01T00:00:00+00:00",
    title: str | None = None,
) -> tuple[int, int]:
    cursor = connection.execute(
        "INSERT INTO jobs(source,title,company,description,collected_at) "
        "VALUES ('test',?,?,?,?)",
        (
            title or status,
            "Private Company",
            "Private job description",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    job_id = cast(int, cursor.lastrowid)
    cursor = connection.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) "
        "VALUES (?,?,?,?)",
        (job_id, status, updated_at, updated_at),
    )
    return job_id, cast(int, cursor.lastrowid)


def _preparation(
    connection: sqlite3.Connection,
    application_id: int,
    *,
    revision: int = 0,
    reviewed_revision: int | None = None,
    ready_revision: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO application_preparations("
        "application_id,revision,requirements_reviewed_revision,"
        "ready_confirmed_revision,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (
            application_id,
            revision,
            reviewed_revision,
            ready_revision,
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )


def _requirement(
    connection: sqlite3.Connection,
    application_id: int,
    *,
    suffix: str,
    kind: str = "deadline",
    required: bool = True,
    fulfillment_state: str = "open",
    deadline_at: str | None = "2026-08-27T13:00:00+00:00",
    confirmed: bool = True,
) -> None:
    connection.execute(
        "INSERT INTO application_requirement_items("
        "id,application_id,kind,label,required,fulfillment_state,deadline_at,"
        "sort_order,origin,capture_method,confirmed,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"requirement-{application_id}-{suffix}",
            application_id,
            kind,
            f"Private requirement {suffix}",
            int(required),
            fulfillment_state,
            deadline_at,
            int(suffix) if suffix.isdigit() else 0,
            "manual",
            "manual",
            int(confirmed),
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )


def _all_application_items(
    connection: sqlite3.Connection,
) -> list[TodayApplication]:
    snapshot = build_today(connection, now=NOW, limit=20)
    return [
        *snapshot.application_overdue.items,
        *snapshot.application_due_today.items,
        *snapshot.application_work.items,
    ]


@pytest.mark.parametrize(
    ("status", "included"),
    [
        ("offer", True),
        ("interview", True),
        ("applied", False),
        ("interested", False),
        ("rejected", False),
        ("withdrawn", False),
    ],
)
def test_status_only_eligibility_matrix(status: str, included: bool) -> None:
    connection = _db()
    _application(connection, status)
    connection.commit()
    items = _all_application_items(connection)
    assert bool(items) is included
    if included:
        assert items[0].classification == "attention"
        assert items[0].next_action == "manage_application"


@pytest.mark.parametrize("status", ["rejected", "withdrawn"])
def test_terminal_status_excludes_persisted_work_artifacts(status: str) -> None:
    connection = _db()
    job_id, application_id = _application(connection, status)
    _preparation(connection, application_id)
    _requirement(
        connection,
        application_id,
        suffix="0",
        deadline_at="2026-08-27T10:00:00+00:00",
    )
    connection.commit()
    open_draft(connection, job_id, preparation_revision=0)
    assert _all_application_items(connection) == []


@pytest.mark.parametrize("status", ["applied", "interested"])
def test_preparation_row_presence_is_explicit_authority(status: str) -> None:
    connection = _db()
    _, no_row = _application(connection, status)
    _, persisted = _application(connection, status)
    _, ready = _application(connection, status)
    _preparation(connection, persisted)
    _preparation(connection, ready, reviewed_revision=0, ready_revision=0)
    connection.commit()

    items = _all_application_items(connection)
    assert [item.application_id for item in items] == [persisted]
    assert items[0].preparation is not None
    assert items[0].preparation.category == "unknown"
    assert items[0].next_action == "complete_preparation"
    assert no_row not in {item.application_id for item in items}
    assert ready not in {item.application_id for item in items}


def test_requirements_satisfied_uses_confirmation_action() -> None:
    connection = _db()
    _, application_id = _application(connection, "applied")
    _preparation(connection, application_id, reviewed_revision=0)
    connection.commit()
    item = _all_application_items(connection)[0]
    assert item.preparation is not None
    assert item.preparation.category == "requirements_satisfied"
    assert item.preparation.blocking_count == 0
    assert item.next_action == "confirm_preparation_ready"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"confirmed": False}, "attention"),
        ({"required": False}, None),
        ({"fulfillment_state": "not_applicable"}, None),
        ({"kind": "task", "deadline_at": None}, "attention"),
        ({"deadline_at": None}, None),
        ({"deadline_at": "not-a-timestamp"}, None),
        ({"deadline_at": "2026-08-27T13:00:00"}, None),
    ],
)
def test_each_deadline_authority_predicate_is_required(
    overrides: dict[str, object], expected: str | None
) -> None:
    connection = _db()
    _, application_id = _application(connection, "applied")
    _preparation(connection, application_id, reviewed_revision=0, ready_revision=0)
    _requirement(connection, application_id, suffix="0", **overrides)
    connection.commit()
    items = _all_application_items(connection)
    if expected is None:
        assert items == []
    else:
        assert [item.classification for item in items] == [expected]
        assert items[0].earliest_deadline_at is None


@pytest.mark.parametrize(
    ("deadline", "classification"),
    [
        ("2026-08-27T11:59:59+00:00", "overdue"),
        ("2026-08-27T12:00:00+00:00", "due_today"),
        ("2026-08-27T23:59:59+00:00", "due_today"),
        ("2026-08-28T00:00:00+00:00", "attention"),
    ],
)
def test_deadline_boundaries_use_one_utc_snapshot(
    deadline: str, classification: str
) -> None:
    connection = _db()
    _, application_id = _application(connection, "offer")
    _preparation(connection, application_id, reviewed_revision=0, ready_revision=0)
    _requirement(connection, application_id, suffix="0", deadline_at=deadline)
    connection.commit()
    item = _all_application_items(connection)[0]
    assert item.classification == classification
    assert item.earliest_deadline_at == (
        deadline if classification != "attention" else None
    )


@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 8, 27, 12, tzinfo=UTC),
        datetime.fromisoformat("2026-08-27T15:00:00+03:00"),
    ],
)
def test_exact_as_of_deadline_is_due_today_and_blocks_preparation(
    as_of: datetime,
) -> None:
    connection = _db()
    _, application_id = _application(connection, "applied")
    _preparation(
        connection,
        application_id,
        reviewed_revision=0,
        ready_revision=0,
    )
    _requirement(
        connection,
        application_id,
        suffix="0",
        deadline_at="2026-08-27T12:00:00+00:00",
    )
    connection.commit()

    item = build_today(connection, now=as_of, limit=20).application_due_today.items[0]

    assert item.classification == "due_today"
    assert item.earliest_deadline_at == "2026-08-27T12:00:00+00:00"
    assert item.preparation is not None
    assert item.preparation.category == "incomplete"
    assert item.preparation.blocking_count == 1
    assert item.next_action == "review_preparation_deadline"


def test_earliest_qualifying_deadline_owns_single_card() -> None:
    connection = _db()
    job_id, application_id = _application(connection, "offer")
    _preparation(connection, application_id)
    _requirement(
        connection,
        application_id,
        suffix="0",
        deadline_at="2026-08-27T15:00:00+00:00",
    )
    _requirement(
        connection,
        application_id,
        suffix="1",
        deadline_at="2026-08-27T10:00:00+00:00",
    )
    connection.commit()
    open_draft(connection, job_id, preparation_revision=0)
    items = _all_application_items(connection)
    assert len(items) == 1
    assert items[0].classification == "overdue"
    assert items[0].earliest_deadline_at == "2026-08-27T10:00:00+00:00"
    assert items[0].preparation is not None
    assert items[0].package is not None
    assert items[0].next_action == "review_preparation_deadline"


def test_concrete_applications_are_not_collapsed_by_duplicate_group() -> None:
    connection = _db()
    first_job, first_application = _application(connection, "offer")
    second_job, second_application = _application(connection, "interview")
    connection.execute("INSERT INTO job_groups(id) VALUES ('shared-group')")
    for job_id in (first_job, second_job):
        connection.execute(
            "INSERT INTO job_group_members(job_id,group_id,reason) "
            "VALUES (?,'shared-group','test')",
            (job_id,),
        )
    connection.commit()
    assert {item.application_id for item in _all_application_items(connection)} == {
        first_application,
        second_application,
    }


def test_open_draft_is_narrow_eligibility_and_finalized_only_is_not() -> None:
    connection = _db()
    job_id, application_id = _application(connection, "applied")
    connection.commit()
    draft, _ = open_draft(connection, job_id, preparation_revision=0)
    item = _all_application_items(connection)[0]
    assert item.application_id == application_id
    assert item.preparation is None
    assert item.package is not None
    assert item.package.has_draft is True
    assert item.package.draft_approved_for_current_revision is False
    assert item.package.draft_preparation_revision_stale is False
    assert item.next_action == "continue_package"

    connection.execute(
        "UPDATE application_package_versions SET state='finalized',"
        "approved_revision=revision,approved_at=?,finalized_at=?,"
        "final_completeness_category='complete' WHERE id=?",
        (
            "2026-08-27T12:00:00+00:00",
            "2026-08-27T12:00:00+00:00",
            draft["id"],
        ),
    )
    connection.commit()
    assert _all_application_items(connection) == []


def test_draft_preparation_and_approval_revisions_are_independent() -> None:
    connection = _db()
    job_id, application_id = _application(connection, "applied")
    _preparation(connection, application_id, revision=1, reviewed_revision=1)
    connection.commit()
    draft, _ = open_draft(connection, job_id, preparation_revision=1)
    connection.execute(
        "UPDATE application_package_versions SET approved_revision=revision,"
        "approved_at=? WHERE id=?",
        ("2026-08-27T11:00:00+00:00", draft["id"]),
    )
    connection.execute(
        "UPDATE application_preparations SET revision=2 WHERE application_id=?",
        (application_id,),
    )
    connection.commit()
    item = _all_application_items(connection)[0]
    assert item.package is not None
    assert item.package.draft_approved_for_current_revision is True
    assert item.package.draft_preparation_revision_stale is True
    assert item.next_action == "complete_preparation"

    connection.execute(
        "UPDATE application_preparations SET requirements_reviewed_revision=2,"
        "ready_confirmed_revision=2 WHERE application_id=?",
        (application_id,),
    )
    connection.commit()
    item = _all_application_items(connection)[0]
    assert item.next_action == "review_rebase_package"

    connection.execute(
        "UPDATE application_package_versions SET preparation_revision=2,"
        "revision=revision+1 WHERE id=?",
        (draft["id"],),
    )
    connection.commit()
    item = _all_application_items(connection)[0]
    assert item.package is not None
    assert item.package.draft_approved_for_current_revision is False
    assert item.package.draft_preparation_revision_stale is False
    assert item.next_action == "continue_package"


def test_work_order_uses_stage_update_then_application_id() -> None:
    connection = _db()
    expected: list[int] = []
    for status, updated in (
        ("interested", "2026-08-27T11:00:00+00:00"),
        ("applied", "2026-08-27T11:00:00+00:00"),
        ("interview", "2026-08-27T10:00:00+00:00"),
        ("offer", "2026-08-27T09:00:00+00:00"),
        ("offer", "2026-08-27T11:00:00+00:00"),
        ("offer", "2026-08-27T11:00:00+00:00"),
    ):
        _, application_id = _application(connection, status, updated_at=updated)
        expected.append(application_id)
        if status in ("applied", "interested"):
            _preparation(connection, application_id)
    connection.commit()
    work = build_today(connection, now=NOW, limit=20).application_work.items
    assert [item.application_id for item in work] == [
        expected[4],
        expected[5],
        expected[3],
        expected[2],
        expected[1],
        expected[0],
    ]


def test_temporal_order_uses_deadline_stage_update_then_id() -> None:
    connection = _db()
    records: list[int] = []
    for index, (status, updated, deadline) in enumerate(
        (
            ("interested", "2026-08-27T11:00:00+00:00", "2026-08-27T10:00:00+00:00"),
            ("offer", "2026-08-27T09:00:00+00:00", "2026-08-27T10:00:00+00:00"),
            ("offer", "2026-08-27T11:00:00+00:00", "2026-08-27T10:00:00+00:00"),
            ("offer", "2026-08-27T11:00:00+00:00", "2026-08-27T10:00:00+00:00"),
            ("interview", "2026-08-27T11:00:00+00:00", "2026-08-27T09:00:00+00:00"),
        )
    ):
        _, application_id = _application(connection, status, updated_at=updated)
        records.append(application_id)
        _preparation(connection, application_id, reviewed_revision=0)
        _requirement(
            connection,
            application_id,
            suffix=str(index),
            deadline_at=deadline,
        )
    connection.commit()
    overdue = build_today(connection, now=NOW, limit=20).application_overdue.items
    assert [item.application_id for item in overdue] == [
        records[4],
        records[2],
        records[3],
        records[1],
        records[0],
    ]


def test_application_totals_precede_each_independent_limit() -> None:
    connection = _db()
    for index in range(3):
        _, application_id = _application(connection, "offer")
        _preparation(connection, application_id, reviewed_revision=0)
        _requirement(
            connection,
            application_id,
            suffix=str(index),
            deadline_at="2026-08-27T10:00:00+00:00",
        )
    for index in range(3, 6):
        _, application_id = _application(connection, "interview")
        _preparation(connection, application_id, reviewed_revision=0)
        _requirement(
            connection,
            application_id,
            suffix=str(index),
            deadline_at="2026-08-27T13:00:00+00:00",
        )
    for _ in range(3):
        _application(connection, "offer")
    connection.commit()
    snapshot = build_today(connection, now=NOW, limit=2)
    assert (
        snapshot.application_overdue.total,
        snapshot.application_due_today.total,
        snapshot.application_work.total,
    ) == (3, 3, 3)
    assert len(snapshot.application_overdue.items) == 2
    assert len(snapshot.application_due_today.items) == 2
    assert len(snapshot.application_work.items) == 2


def test_requirement_readiness_and_package_transitions_rederive_work() -> None:
    connection = _db()
    job_id, application_id = _application(connection, "applied")
    _preparation(connection, application_id, reviewed_revision=0)
    _requirement(
        connection,
        application_id,
        suffix="0",
        deadline_at="2026-08-27T10:00:00+00:00",
    )
    connection.commit()
    assert _all_application_items(connection)[0].classification == "overdue"

    connection.execute(
        "UPDATE application_requirement_items SET deadline_at=? WHERE application_id=?",
        ("2026-08-28T00:00:00+00:00", application_id),
    )
    connection.commit()
    assert _all_application_items(connection)[0].classification == "attention"

    connection.execute(
        "UPDATE application_requirement_items SET fulfillment_state='not_applicable' "
        "WHERE application_id=?",
        (application_id,),
    )
    connection.execute(
        "UPDATE application_preparations SET ready_confirmed_revision=0 "
        "WHERE application_id=?",
        (application_id,),
    )
    connection.commit()
    assert _all_application_items(connection) == []

    connection.execute(
        "DELETE FROM application_requirement_items WHERE application_id=?",
        (application_id,),
    )
    connection.execute(
        "UPDATE application_preparations SET ready_confirmed_revision=NULL "
        "WHERE application_id=?",
        (application_id,),
    )
    connection.commit()
    assert (
        _all_application_items(connection)[0].next_action == "confirm_preparation_ready"
    )

    connection.execute(
        "UPDATE application_preparations SET ready_confirmed_revision=0 "
        "WHERE application_id=?",
        (application_id,),
    )
    connection.commit()
    draft, _ = open_draft(connection, job_id, preparation_revision=0)
    assert _all_application_items(connection)[0].next_action == "continue_package"
    delete_version(connection, draft["id"], package_revision=0)
    assert _all_application_items(connection) == []

    connection.execute(
        "UPDATE applications SET current_status='rejected' WHERE id=?",
        (application_id,),
    )
    connection.commit()
    assert _all_application_items(connection) == []


def test_application_status_transition_rederives_eligibility() -> None:
    connection = _db()
    _, application_id = _application(connection, "offer")
    connection.commit()
    assert _all_application_items(connection)

    connection.execute(
        "UPDATE applications SET current_status='applied' WHERE id=?",
        (application_id,),
    )
    connection.commit()
    assert _all_application_items(connection) == []

    _preparation(connection, application_id)
    connection.commit()
    assert _all_application_items(connection)

    connection.execute(
        "UPDATE applications SET current_status='withdrawn' WHERE id=?",
        (application_id,),
    )
    connection.commit()
    assert _all_application_items(connection) == []


def test_today_does_not_call_completeness_or_read_material_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _db()
    job_id, _ = _application(connection, "applied")
    connection.commit()
    open_draft(connection, job_id, preparation_revision=0)

    def fail_completeness(*args: object, **kwargs: object) -> object:
        raise AssertionError("Package completeness must not run")

    monkeypatch.setattr(package_service, "completeness", fail_completeness)
    monkeypatch.setattr(
        package_service, "_verify_distinct_materials", fail_completeness
    )
    monkeypatch.setattr(
        package_service, "_open_verified_material_metadata", fail_completeness
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    assert _all_application_items(connection)
    connection.set_trace_callback(None)
    sql = "\n".join(statements).lower()
    assert "package_material_selections" not in sql
    assert "materials" not in sql
    assert "package_answers" not in sql
    assert "package_completeness_reasons" not in sql


def test_application_query_count_is_cardinality_independent() -> None:
    def traced_query_kinds(count: int) -> tuple[str, ...]:
        connection = _db()
        draft_job_ids: list[int] = []
        for index in range(count):
            job_id, application_id = _application(connection, "applied")
            artifact_kind = index % 3
            if artifact_kind == 0:
                _preparation(connection, application_id)
            elif artifact_kind == 1:
                _preparation(
                    connection,
                    application_id,
                    reviewed_revision=0,
                    ready_revision=0,
                )
                _requirement(
                    connection,
                    application_id,
                    suffix=str(index),
                    deadline_at="2026-08-27T13:00:00+00:00",
                )
            else:
                draft_job_ids.append(job_id)
        connection.commit()

        for job_id in draft_job_ids:
            open_draft(connection, job_id, preparation_revision=0)

        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        list_today_applications(
            connection,
            as_of=NOW.isoformat(),
            today_ends_at="2026-08-28T00:00:00+00:00",
            limit=count,
        )
        connection.set_trace_callback(None)
        queries = tuple(
            statement.lstrip().split(None, 1)[0] for statement in statements
        )
        assert len(queries) == 2
        assert queries == ("WITH", "SELECT")
        return queries

    assert traced_query_kinds(3) == traced_query_kinds(30) == ("WITH", "SELECT")


def test_http_projection_is_strictly_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "today.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
        job_id, application_id = _application(connection, "offer", title="Public title")
        _preparation(connection, application_id)
        _requirement(
            connection,
            application_id,
            suffix="0",
            deadline_at="2020-01-01T00:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO application_events("
            "application_id,old_status,new_status,note,created_at) "
            "VALUES (?,NULL,'offer','Private application note',?)",
            (application_id, "2026-08-01T00:00:00+00:00"),
        )
        connection.commit()
        open_draft(connection, job_id, preparation_revision=0)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    response = TestClient(create_app()).get("/today?limit=20")
    assert response.status_code == 200
    item = response.json()["applications"]["overdue"]["items"][0]
    assert set(item) == {
        "application_id",
        "job_id",
        "current_status",
        "updated_at",
        "job",
        "classification",
        "next_action",
        "earliest_deadline_at",
        "preparation",
        "package",
    }
    assert item["package"] is not None
    assert set(item["package"]) == {
        "has_draft",
        "draft_approved_for_current_revision",
        "draft_preparation_revision_stale",
    }
    assert set(item["preparation"]) == {"category", "blocking_count"}
    assert set(item["job"]) == {
        "id",
        "source",
        "source_job_id",
        "title",
        "company",
        "location",
        "remote_status",
        "employment_type",
        "published_at",
        "url",
    }
    serialized = str(item)
    assert "Private application note" not in serialized
    assert "Private requirement" not in serialized
    assert "Private job description" not in serialized
    for private_name in (
        "answers",
        "materials",
        "filename",
        "version",
        "hash",
        "storage_key",
        "path",
        "fingerprint",
        "completeness",
        "diagnostic",
    ):
        assert private_name not in serialized.lower()


def test_schema_version_remains_twelve() -> None:
    assert SCHEMA_VERSION == 12
