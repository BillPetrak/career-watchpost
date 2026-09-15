"""Database schema initialization for CareerOS."""

# The bootstrap SQL is deliberately kept readable as SQL; long constraint
# expressions are validated by the structural inspector.
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA_VERSION = 12


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

        CREATE TABLE IF NOT EXISTS preference_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            explicit_configured INTEGER NOT NULL DEFAULT 0
                CHECK (explicit_configured IN (0,1)),
            explicit_updated_at TEXT,
            learning_reset_at TEXT,
            learning_reset_review_event_id INTEGER NOT NULL DEFAULT 0
                CHECK (learning_reset_review_event_id >= 0),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS explicit_preferences (
            dimension TEXT NOT NULL CHECK (dimension IN (
                'desired_titles','skills','required_skills',
                'preferred_locations','remote_preference','excluded_terms',
                'hard_excluded_terms','preferred_employment_types',
                'preferred_seniority'
            )),
            position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 99),
            value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 100),
            PRIMARY KEY (dimension, position)
        );

        CREATE TABLE IF NOT EXISTS preference_evidence (
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
        );

        CREATE INDEX IF NOT EXISTS idx_preference_evidence_dimension_value
            ON preference_evidence(dimension, normalized_value);

        CREATE INDEX IF NOT EXISTS idx_preference_evidence_review_event
            ON preference_evidence(review_event_id);

        CREATE TABLE IF NOT EXISTS learned_preference_suppressions (
            preference_key TEXT PRIMARY KEY CHECK (
                length(preference_key) = 32
                AND preference_key = lower(preference_key)
                AND preference_key NOT GLOB '*[^0-9a-f]*'
            ),
            dimension TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            suppressed_at TEXT NOT NULL,
            UNIQUE (dimension, normalized_value)
        );

        CREATE TABLE IF NOT EXISTS material_store (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            store_id TEXT NOT NULL UNIQUE CHECK (
                length(store_id) = 32 AND store_id = lower(store_id)
                AND store_id NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL CHECK (length(created_at) > 0)
        );

        CREATE TABLE IF NOT EXISTS materials (
            id TEXT PRIMARY KEY CHECK (
                length(id) = 32 AND id = lower(id)
                AND id NOT GLOB '*[^0-9a-f]*'
            ),
            name TEXT NOT NULL CHECK (
                length(name) BETWEEN 1 AND 160 AND name = trim(name)
            ),
            category TEXT NOT NULL CHECK (category IN
                ('resume','cover_letter_base','portfolio','supporting_document','other')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            status TEXT NOT NULL DEFAULT 'active' CHECK (
                status IN ('active','retired')
            ),
            retired_at TEXT,
            created_at TEXT NOT NULL CHECK (length(created_at) > 0),
            updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
            CHECK ((status = 'active' AND retired_at IS NULL) OR
                   (status = 'retired' AND retired_at IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_materials_status_updated
            ON materials(status, updated_at DESC, id);

        CREATE TABLE IF NOT EXISTS material_versions (
            id TEXT PRIMARY KEY CHECK (
                length(id) = 32 AND id = lower(id)
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
                length(sha256) = 64 AND sha256 = lower(sha256)
                AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            storage_key TEXT NOT NULL CHECK (
                length(storage_key) = 64 AND storage_key = lower(storage_key)
                AND storage_key NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL CHECK (length(created_at) > 0)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_material_versions_number
            ON material_versions(material_id, version_number);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_material_versions_content
            ON material_versions(material_id, sha256);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_material_versions_storage_key
            ON material_versions(storage_key);
        CREATE INDEX IF NOT EXISTS idx_material_versions_material
            ON material_versions(material_id, version_number DESC);

        CREATE TABLE application_packages (
            id TEXT PRIMARY KEY CHECK (length(id) = 32 AND id = lower(id)
                AND id NOT GLOB '*[^0-9a-f]*'),
            application_id INTEGER NOT NULL REFERENCES applications(id)
                ON DELETE RESTRICT,
            next_version_number INTEGER NOT NULL DEFAULT 1 CHECK
                (next_version_number BETWEEN 1 AND 2147483647),
            created_at TEXT NOT NULL CHECK (length(created_at) > 0),
            updated_at TEXT NOT NULL CHECK (length(updated_at) > 0)
        );
        CREATE UNIQUE INDEX idx_application_packages_application
            ON application_packages(application_id ASC);

        CREATE TABLE application_package_versions (
            id TEXT PRIMARY KEY CHECK (length(id) = 32 AND id = lower(id)
                AND id NOT GLOB '*[^0-9a-f]*'),
            package_id TEXT NOT NULL REFERENCES application_packages(id)
                ON DELETE CASCADE,
            version_number INTEGER NOT NULL CHECK
                (version_number BETWEEN 1 AND 2147483647),
            state TEXT NOT NULL CHECK (state IN ('draft', 'finalized')),
            revision INTEGER NOT NULL DEFAULT 0 CHECK
                (revision BETWEEN 0 AND 2147483647),
            preparation_revision INTEGER NOT NULL CHECK
                (preparation_revision BETWEEN 0 AND 2147483647),
            forked_from_version_id TEXT,
            forked_from_version_number INTEGER,
            job_id_snapshot INTEGER NOT NULL CHECK (job_id_snapshot > 0),
            job_source TEXT NOT NULL CHECK (length(job_source) > 0),
            job_source_job_id TEXT, job_title TEXT NOT NULL CHECK (length(job_title) > 0),
            job_company TEXT, job_location TEXT, job_url TEXT,
            job_employment_type TEXT, job_remote_status TEXT, job_published_at TEXT,
            job_updated_at_snapshot TEXT NOT NULL CHECK (length(job_updated_at_snapshot) > 0),
            job_description_present INTEGER NOT NULL CHECK (job_description_present IN (0,1)),
            job_content_fingerprint TEXT NOT NULL CHECK
                (length(job_content_fingerprint)=64 AND job_content_fingerprint=lower(job_content_fingerprint)
                 AND job_content_fingerprint NOT GLOB '*[^0-9a-f]*'),
            job_reconfirmation_required_at TEXT,
            approved_revision INTEGER, approved_at TEXT,
            finalized_at TEXT, final_completeness_category TEXT,
            created_at TEXT NOT NULL CHECK (length(created_at)>0),
            updated_at TEXT NOT NULL CHECK (length(updated_at)>0),
            CHECK ((forked_from_version_id IS NULL AND forked_from_version_number IS NULL) OR
                (forked_from_version_id IS NOT NULL AND length(forked_from_version_id)=32
                 AND forked_from_version_id=lower(forked_from_version_id)
                 AND forked_from_version_id NOT GLOB '*[^0-9a-f]*'
                 AND forked_from_version_number BETWEEN 1 AND 2147483647)),
            CHECK ((approved_revision IS NULL AND approved_at IS NULL) OR
                (approved_revision IS NOT NULL AND approved_revision BETWEEN 0 AND revision
                 AND approved_at IS NOT NULL AND length(approved_at)>0)),
            CHECK ((state='draft' AND finalized_at IS NULL AND final_completeness_category IS NULL) OR
                (state='finalized' AND finalized_at IS NOT NULL AND length(finalized_at)>0
                 AND final_completeness_category='complete' AND approved_revision=revision
                 AND approved_at IS NOT NULL))
        );
        CREATE UNIQUE INDEX idx_package_versions_package_number
            ON application_package_versions(package_id COLLATE BINARY ASC, version_number ASC);
        CREATE UNIQUE INDEX idx_package_versions_one_draft
            ON application_package_versions(package_id COLLATE BINARY ASC) WHERE state='draft';
        CREATE INDEX idx_package_versions_history
            ON application_package_versions(package_id COLLATE BINARY ASC, version_number DESC)
            WHERE state='finalized';

        CREATE TABLE application_package_requirement_snapshots (
            package_version_id TEXT NOT NULL REFERENCES application_package_versions(id) ON DELETE CASCADE,
            requirement_id TEXT NOT NULL, kind TEXT NOT NULL CHECK
                (kind IN ('material','link','question','task','deadline','other')),
            label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 160),
            detail TEXT CHECK (detail IS NULL OR length(detail) BETWEEN 1 AND 1000),
            required INTEGER NOT NULL CHECK (required IN (0,1)),
            fulfillment_state TEXT NOT NULL CHECK (fulfillment_state IN ('open','satisfied','not_applicable')),
            deadline_at TEXT, sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
            origin TEXT NOT NULL CHECK (origin='manual'), capture_method TEXT NOT NULL CHECK (capture_method='manual'),
            confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (package_version_id, requirement_id),
            CHECK (kind != 'deadline' OR fulfillment_state != 'satisfied')
        );
        CREATE UNIQUE INDEX idx_package_requirement_snapshots_order
            ON application_package_requirement_snapshots(package_version_id COLLATE BINARY ASC, sort_order ASC);

        CREATE TABLE package_material_selections (
            package_version_id TEXT NOT NULL, requirement_id TEXT NOT NULL,
            material_version_id TEXT NOT NULL REFERENCES material_versions(id) ON DELETE RESTRICT,
            material_id_snapshot TEXT NOT NULL CHECK (length(material_id_snapshot)=32 AND material_id_snapshot=lower(material_id_snapshot) AND material_id_snapshot NOT GLOB '*[^0-9a-f]*'),
            material_name_snapshot TEXT NOT NULL CHECK (length(material_name_snapshot) BETWEEN 1 AND 160 AND material_name_snapshot=trim(material_name_snapshot)),
            material_category_snapshot TEXT NOT NULL CHECK (material_category_snapshot IN ('resume','cover_letter_base','portfolio','supporting_document','other')),
            material_version_number_snapshot INTEGER NOT NULL CHECK (material_version_number_snapshot BETWEEN 1 AND 100),
            display_filename_snapshot TEXT NOT NULL CHECK (length(display_filename_snapshot) BETWEEN 1 AND 255),
            declared_media_type_snapshot TEXT NOT NULL CHECK (length(declared_media_type_snapshot) BETWEEN 3 AND 127),
            byte_size_snapshot INTEGER NOT NULL CHECK (byte_size_snapshot BETWEEN 1 AND 26214400),
            sha256_snapshot TEXT NOT NULL CHECK (length(sha256_snapshot)=64 AND sha256_snapshot=lower(sha256_snapshot) AND sha256_snapshot NOT GLOB '*[^0-9a-f]*'),
            origin TEXT NOT NULL CHECK (origin='manual'), capture_method TEXT NOT NULL CHECK (capture_method='manual'),
            created_at TEXT NOT NULL CHECK (length(created_at)>0), updated_at TEXT NOT NULL CHECK (length(updated_at)>0),
            PRIMARY KEY (package_version_id, requirement_id),
            FOREIGN KEY (package_version_id, requirement_id) REFERENCES application_package_requirement_snapshots(package_version_id, requirement_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_package_material_selections_material_version
            ON package_material_selections(material_version_id COLLATE BINARY ASC);

        CREATE TABLE package_answers (
            package_version_id TEXT NOT NULL, requirement_id TEXT NOT NULL,
            answer_text TEXT NOT NULL CHECK (length(answer_text) BETWEEN 1 AND 10000),
            origin TEXT NOT NULL CHECK (origin='manual'), capture_method TEXT NOT NULL CHECK (capture_method='manual'),
            confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)), confirmed_at TEXT,
            created_at TEXT NOT NULL CHECK (length(created_at)>0), updated_at TEXT NOT NULL CHECK (length(updated_at)>0),
            PRIMARY KEY (package_version_id, requirement_id),
            FOREIGN KEY (package_version_id, requirement_id) REFERENCES application_package_requirement_snapshots(package_version_id, requirement_id) ON DELETE CASCADE,
            CHECK ((confirmed=0 AND confirmed_at IS NULL) OR (confirmed=1 AND confirmed_at IS NOT NULL AND length(confirmed_at)>0))
        );
        CREATE TABLE package_completeness_reasons (
            package_version_id TEXT NOT NULL REFERENCES application_package_versions(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            code TEXT NOT NULL CHECK (code IN ('job_information_unavailable','preparation_revision_stale','job_snapshot_stale','job_reconfirmation_required','requirements_not_reviewed','requirements_review_stale','requirement_unconfirmed','required_deadline_passed','required_requirement_open','ready_confirmation_missing','ready_confirmation_stale','material_selection_missing','selected_material_retired','selected_material_unavailable','answer_missing','answer_unconfirmed','package_complete')),
            blocking INTEGER NOT NULL CHECK (blocking IN (0,1)), requirement_id TEXT,
            PRIMARY KEY (package_version_id, position),
            CHECK ((code IN ('job_information_unavailable','package_complete') AND blocking=0) OR (code NOT IN ('job_information_unavailable','package_complete') AND blocking=1))
        );
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
        INSERT INTO preference_state (
            singleton, explicit_configured, explicit_updated_at,
            learning_reset_at, learning_reset_review_event_id, created_at
        )
        VALUES (1, 0, NULL, NULL, 0, ?)
        """,
        (datetime.now(UTC).isoformat(),),
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
