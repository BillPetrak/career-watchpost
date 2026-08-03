"""Database schema initialization for CareerOS."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 4


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the initial CareerOS database schema."""
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
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_application_events_application
            ON application_events(application_id, created_at DESC, id DESC);
        """
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
