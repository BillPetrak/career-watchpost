"""Database schema initialization for CareerOS."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 9


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Bootstrap a genuinely empty database; never migrate an existing one."""
    tables = int(
        connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    )
    if tables:
        try:
            metadata = connection.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError("database schema is malformed") from exc
        if metadata is None or str(metadata[0]) != str(SCHEMA_VERSION):
            raise ValueError("database requires explicit migration")
        from careeros.database.migrations.core import inspect_schema

        if inspect_schema(connection) != "current":
            raise ValueError("database schema is structurally invalid")
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_job_id TEXT,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            description TEXT,
            url TEXT,
            employment_type TEXT,
            remote_status TEXT,
            published_at TEXT,
            collected_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, source_job_id)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_title
            ON jobs(title);

        CREATE INDEX IF NOT EXISTS idx_jobs_company
            ON jobs(company);

        CREATE INDEX IF NOT EXISTS idx_jobs_published_at
            ON jobs(published_at);

        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            succeeded INTEGER,
            fetched INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_collection_runs_source_started
            ON collection_runs(source, started_at DESC);

        CREATE TABLE IF NOT EXISTS job_groups (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS job_group_members (
            job_id INTEGER PRIMARY KEY
                REFERENCES jobs(id) ON DELETE CASCADE,
            group_id TEXT NOT NULL
                REFERENCES job_groups(id),
            reason TEXT NOT NULL,
            remote_sig TEXT,
            location_sig TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_job_group_members_group
            ON job_group_members(group_id);

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE
                REFERENCES jobs(id) ON DELETE RESTRICT,
            current_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_applications_status
            ON applications(current_status);

        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL
                REFERENCES applications(id) ON DELETE RESTRICT,
            old_status TEXT,
            new_status TEXT NOT NULL,
            note TEXT,
            note_redacted_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_application_events_application
            ON application_events(application_id, created_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS application_preparations (
            application_id INTEGER PRIMARY KEY REFERENCES applications(id)
                ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            requirements_reviewed_revision INTEGER,
            requirements_reviewed_at TEXT,
            ready_confirmed_revision INTEGER,
            ready_confirmed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS application_requirement_items (
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
            deadline_at TEXT,
            sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
            origin TEXT NOT NULL CHECK (origin = 'manual'),
            capture_method TEXT NOT NULL CHECK (capture_method = 'manual'),
            confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (kind != 'deadline' OR fulfillment_state != 'satisfied'),
            UNIQUE(application_id, sort_order)
        );
        CREATE INDEX IF NOT EXISTS idx_requirement_items_application
            ON application_requirement_items(application_id, sort_order);

        CREATE TABLE IF NOT EXISTS saved_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT,
            query TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('search', 'rank')),
            profile_path TEXT,
            grouped INTEGER NOT NULL DEFAULT 0,
            source TEXT,
            remote INTEGER,
            location TEXT,
            untracked INTEGER NOT NULL DEFAULT 0,
            include_statuses TEXT,
            exclude_statuses TEXT,
            min_score INTEGER CHECK (min_score IS NULL OR min_score BETWEEN 0 AND 100),
            result_limit INTEGER NOT NULL DEFAULT 20 CHECK (result_limit > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_saved_searches_name
            ON saved_searches(name COLLATE NOCASE);

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL
                REFERENCES jobs(id) ON DELETE RESTRICT,
            kind TEXT NOT NULL CHECK (
                kind IN ('follow_up', 'interview', 'offer', 'review', 'custom')
            ),
            message TEXT,
            due_at TEXT NOT NULL,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_reminders_job
            ON reminders(job_id);

        CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(due_at, completed_at);

        CREATE TABLE IF NOT EXISTS reminder_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id INTEGER NOT NULL
                REFERENCES reminders(id) ON DELETE RESTRICT,
            scheduled_due_at TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            delivered_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('attempted', 'delivered', 'failed')
            ),
            error TEXT,
            backend TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS
            idx_reminder_notifications_delivered_occurrence
            ON reminder_notifications(reminder_id, scheduled_due_at)
            WHERE status = 'delivered';

        CREATE INDEX IF NOT EXISTS idx_reminder_notifications_reminder
            ON reminder_notifications(reminder_id, scheduled_due_at, id);

        CREATE INDEX IF NOT EXISTS idx_reminder_notifications_attempted
            ON reminder_notifications(attempted_at);

        CREATE TABLE IF NOT EXISTS review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE
                REFERENCES jobs(id) ON DELETE RESTRICT,
            decision TEXT NOT NULL
                CHECK (decision IN ('shortlist', 'dismiss', 'review_later')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_review_decisions_decision
            ON review_decisions(decision);

        CREATE TABLE IF NOT EXISTS review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL
                REFERENCES jobs(id) ON DELETE RESTRICT,
            old_decision TEXT,
            new_decision TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_review_events_job
            ON review_events(job_id, created_at DESC, id DESC);
        """
    )

    # Existing v8 databases need the additive event column; preparation tables
    # above are intentionally created with IF NOT EXISTS for safe retries.
    event_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(application_events)")
    }
    if "note_redacted_at" not in event_columns:
        connection.execute(
            "ALTER TABLE application_events ADD COLUMN note_redacted_at TEXT"
        )

    connection.execute(
        """
        INSERT INTO schema_metadata(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )

    connection.commit()
