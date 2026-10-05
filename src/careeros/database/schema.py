"""Database schema initialization for CareerOS."""

# The bootstrap SQL is deliberately kept readable as SQL; long constraint
# expressions are validated by the structural inspector.
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA_VERSION = 14


V13_DDL = """
CREATE TABLE resume_extraction_runs (
    id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'),
    material_version_id TEXT NOT NULL REFERENCES material_versions(id) ON DELETE RESTRICT,
    source_name TEXT NOT NULL CHECK (length(source_name) BETWEEN 1 AND 160),
    source_media_type TEXT NOT NULL CHECK (length(source_media_type) BETWEEN 1 AND 127),
    source_byte_size INTEGER NOT NULL CHECK (source_byte_size BETWEEN 1 AND 26214400),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256)=64 AND source_sha256=lower(source_sha256) AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_format TEXT NOT NULL CHECK (source_format IN ('pdf','docx','text','unknown')),
    detected_format TEXT NOT NULL CHECK (detected_format IN ('pdf','docx','text','unknown')),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed')),
    raw_text TEXT,
    raw_text_sha256 TEXT CHECK (raw_text_sha256 IS NULL OR (length(raw_text_sha256)=64 AND raw_text_sha256=lower(raw_text_sha256) AND raw_text_sha256 NOT GLOB '*[^0-9a-f]*')),
    segment_count INTEGER NOT NULL DEFAULT 0 CHECK (segment_count BETWEEN 0 AND 100000),
    extractor_fingerprint TEXT NOT NULL CHECK (length(extractor_fingerprint) BETWEEN 1 AND 256),
    cache_fingerprint TEXT NOT NULL CHECK (length(cache_fingerprint) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL CHECK (length(created_at)>0),
    CHECK ((outcome='succeeded' AND raw_text IS NOT NULL AND raw_text_sha256 IS NOT NULL) OR (outcome='failed' AND raw_text IS NULL AND raw_text_sha256 IS NULL))
);
CREATE UNIQUE INDEX idx_resume_extraction_cache_success ON resume_extraction_runs(cache_fingerprint) WHERE outcome='succeeded';
CREATE INDEX idx_resume_extraction_material_history ON resume_extraction_runs(material_version_id, created_at DESC, id DESC);

CREATE TABLE resume_text_segments (
    extraction_run_id TEXT NOT NULL REFERENCES resume_extraction_runs(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    raw_start_byte INTEGER NOT NULL CHECK (raw_start_byte >= 0),
    raw_end_byte INTEGER NOT NULL CHECK (raw_end_byte >= raw_start_byte),
    source_locator TEXT NOT NULL CHECK (length(source_locator) BETWEEN 1 AND 1024),
    slice_sha256 TEXT NOT NULL CHECK (length(slice_sha256)=64 AND slice_sha256=lower(slice_sha256) AND slice_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (extraction_run_id, ordinal),
    CHECK (raw_end_byte - raw_start_byte <= 26214400)
);
CREATE INDEX idx_resume_text_segments_range ON resume_text_segments(extraction_run_id, raw_start_byte, raw_end_byte, ordinal);

CREATE TABLE resume_sanitization_runs (
    id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'),
    extraction_run_id TEXT NOT NULL REFERENCES resume_extraction_runs(id) ON DELETE RESTRICT,
    parent_sanitization_run_id TEXT,
    sanitized_text TEXT,
    sanitized_text_sha256 TEXT CHECK (sanitized_text_sha256 IS NULL OR (length(sanitized_text_sha256)=64 AND sanitized_text_sha256=lower(sanitized_text_sha256) AND sanitized_text_sha256 NOT GLOB '*[^0-9a-f]*')),
    sanitizer_fingerprint TEXT NOT NULL CHECK (length(sanitizer_fingerprint) BETWEEN 1 AND 256),
    policy_fingerprint TEXT NOT NULL CHECK (length(policy_fingerprint) BETWEEN 1 AND 256),
    override_manifest_json TEXT NOT NULL DEFAULT '{}' CHECK (length(override_manifest_json) BETWEEN 2 AND 100000),
    cache_fingerprint TEXT NOT NULL CHECK (length(cache_fingerprint) BETWEEN 1 AND 256),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed')),
    created_at TEXT NOT NULL CHECK (length(created_at)>0),
    CHECK ((outcome='succeeded' AND sanitized_text IS NOT NULL AND sanitized_text_sha256 IS NOT NULL) OR (outcome='failed' AND sanitized_text IS NULL AND sanitized_text_sha256 IS NULL)),
    UNIQUE(id, extraction_run_id),
    FOREIGN KEY (parent_sanitization_run_id, extraction_run_id) REFERENCES resume_sanitization_runs(id, extraction_run_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX idx_resume_sanitization_cache_success ON resume_sanitization_runs(cache_fingerprint) WHERE outcome='succeeded';
CREATE INDEX idx_resume_sanitization_extraction ON resume_sanitization_runs(extraction_run_id, created_at DESC, id DESC);

CREATE TABLE resume_sanitization_spans (
    sanitization_run_id TEXT NOT NULL REFERENCES resume_sanitization_runs(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('copy','replace','delete')),
    raw_start_byte INTEGER NOT NULL CHECK (raw_start_byte >= 0),
    raw_end_byte INTEGER NOT NULL CHECK (raw_end_byte >= raw_start_byte),
    sanitized_start_byte INTEGER NOT NULL CHECK (sanitized_start_byte >= 0),
    sanitized_end_byte INTEGER NOT NULL CHECK (sanitized_end_byte >= sanitized_start_byte),
    raw_slice_sha256 TEXT NOT NULL CHECK (length(raw_slice_sha256)=64 AND raw_slice_sha256=lower(raw_slice_sha256) AND raw_slice_sha256 NOT GLOB '*[^0-9a-f]*'),
    sanitized_slice_sha256 TEXT NOT NULL CHECK (length(sanitized_slice_sha256)=64 AND sanitized_slice_sha256=lower(sanitized_slice_sha256) AND sanitized_slice_sha256 NOT GLOB '*[^0-9a-f]*'),
    placeholder_key TEXT,
    PRIMARY KEY (sanitization_run_id, ordinal),
    CHECK ((kind='delete' AND raw_end_byte>raw_start_byte AND sanitized_end_byte=sanitized_start_byte) OR (kind='copy' AND raw_end_byte>raw_start_byte AND sanitized_end_byte>sanitized_start_byte AND raw_end_byte-raw_start_byte = sanitized_end_byte-sanitized_start_byte) OR (kind='replace' AND raw_end_byte>raw_start_byte AND sanitized_end_byte>sanitized_start_byte)),
    CHECK (placeholder_key IS NULL OR length(placeholder_key) BETWEEN 1 AND 160),
    CHECK (kind='replace' OR placeholder_key IS NULL),
    CHECK (kind!='copy' OR raw_slice_sha256=sanitized_slice_sha256)
);
CREATE INDEX idx_resume_sanitization_spans_raw ON resume_sanitization_spans(sanitization_run_id, raw_start_byte, raw_end_byte, ordinal);
CREATE INDEX idx_resume_sanitization_spans_sanitized ON resume_sanitization_spans(sanitization_run_id, sanitized_start_byte, sanitized_end_byte, ordinal);

CREATE TABLE resume_pii_findings (
    sanitization_run_id TEXT NOT NULL REFERENCES resume_sanitization_runs(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    category TEXT NOT NULL CHECK (length(category) BETWEEN 1 AND 80),
    rule_identity TEXT NOT NULL CHECK (length(rule_identity) BETWEEN 1 AND 256),
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    disposition TEXT NOT NULL CHECK (disposition IN ('pending','replace','keep','review')), origin TEXT NOT NULL CHECK (length(origin) BETWEEN 1 AND 256), raw_start_byte INTEGER NOT NULL CHECK (raw_start_byte >= 0), raw_end_byte INTEGER NOT NULL CHECK (raw_end_byte > raw_start_byte),
    sanitization_span_ordinal INTEGER,
    PRIMARY KEY (sanitization_run_id, ordinal),
    CHECK (sanitization_span_ordinal IS NULL OR sanitization_span_ordinal >= 0),
    FOREIGN KEY (sanitization_run_id, sanitization_span_ordinal) REFERENCES resume_sanitization_spans(sanitization_run_id, ordinal) ON DELETE RESTRICT
);
CREATE INDEX idx_resume_pii_findings_range ON resume_pii_findings(sanitization_run_id, raw_start_byte, raw_end_byte, ordinal);

CREATE TABLE resume_sanitization_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sanitization_run_id TEXT NOT NULL REFERENCES resume_sanitization_runs(id) ON DELETE RESTRICT,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('approved','revoked')),
    note TEXT,
    created_at TEXT NOT NULL CHECK (length(created_at)>0)
);
CREATE INDEX idx_resume_sanitization_review_history ON resume_sanitization_review_events(sanitization_run_id, created_at DESC, id DESC);

CREATE TABLE career_fact_runs (
    id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'),
    extraction_run_id TEXT NOT NULL REFERENCES resume_extraction_runs(id) ON DELETE RESTRICT,
    sanitization_run_id TEXT,
    input_kind TEXT NOT NULL CHECK (input_kind IN ('extracted','sanitized')),
    mode TEXT NOT NULL CHECK (mode IN ('deterministic','model')),
    extractor_fingerprint TEXT CHECK (extractor_fingerprint IS NULL OR length(extractor_fingerprint) BETWEEN 1 AND 256), schema_fingerprint TEXT CHECK (schema_fingerprint IS NULL OR length(schema_fingerprint) BETWEEN 1 AND 256), provider_fingerprint TEXT CHECK (provider_fingerprint IS NULL OR length(provider_fingerprint) BETWEEN 1 AND 256), model_fingerprint TEXT CHECK (model_fingerprint IS NULL OR length(model_fingerprint) BETWEEN 1 AND 256), prompt_fingerprint TEXT CHECK (prompt_fingerprint IS NULL OR length(prompt_fingerprint) BETWEEN 1 AND 256),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed')),
    cache_fingerprint TEXT NOT NULL CHECK (length(cache_fingerprint) BETWEEN 1 AND 256),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256)=64 AND input_sha256=lower(input_sha256) AND input_sha256 NOT GLOB '*[^0-9a-f]*'),
    input_byte_size INTEGER NOT NULL CHECK (input_byte_size >= 0),
    token_count INTEGER CHECK (token_count IS NULL OR token_count BETWEEN 0 AND 100000000),
    created_at TEXT NOT NULL CHECK (length(created_at)>0),
    CHECK ((mode='deterministic' AND input_kind='extracted' AND sanitization_run_id IS NULL) OR (mode='model' AND input_kind='sanitized' AND sanitization_run_id IS NOT NULL)),
    UNIQUE(id, extraction_run_id),
    FOREIGN KEY (sanitization_run_id, extraction_run_id) REFERENCES resume_sanitization_runs(id, extraction_run_id) ON DELETE RESTRICT
);
CREATE INDEX idx_career_fact_runs_extraction ON career_fact_runs(extraction_run_id, created_at DESC, id DESC);

CREATE TABLE career_fact_candidates (
    id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'),
    fact_run_id TEXT NOT NULL REFERENCES career_fact_runs(id) ON DELETE RESTRICT,
    fact_type TEXT NOT NULL CHECK (length(fact_type) BETWEEN 1 AND 160), normalized_key TEXT NOT NULL CHECK (length(normalized_key) BETWEEN 1 AND 512),
    canonical_value_json TEXT NOT NULL CHECK (length(canonical_value_json) BETWEEN 1 AND 100000), value_sha256 TEXT NOT NULL CHECK (length(value_sha256)=64 AND value_sha256=lower(value_sha256) AND value_sha256 NOT GLOB '*[^0-9a-f]*'), confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0), origin TEXT NOT NULL CHECK (origin IN ('deterministic','model'))
);
CREATE INDEX idx_career_fact_candidates_run ON career_fact_candidates(fact_run_id, id);

CREATE TABLE career_fact_evidence (
    candidate_id TEXT NOT NULL REFERENCES career_fact_candidates(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0), coordinate_space TEXT NOT NULL CHECK (coordinate_space IN ('extracted','sanitized')),
    start_byte INTEGER NOT NULL CHECK (start_byte >= 0), end_byte INTEGER NOT NULL CHECK (end_byte >= start_byte), slice_sha256 TEXT NOT NULL CHECK (length(slice_sha256)=64 AND slice_sha256=lower(slice_sha256) AND slice_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (candidate_id, ordinal)
);
CREATE INDEX idx_career_fact_evidence_range ON career_fact_evidence(candidate_id, coordinate_space, start_byte, end_byte, ordinal);

CREATE TABLE career_fact_review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL REFERENCES career_fact_candidates(id) ON DELETE RESTRICT,
    event_kind TEXT NOT NULL CHECK (event_kind IN ('approved','rejected','approved_edit')), edited_value_json TEXT CHECK (edited_value_json IS NULL OR length(edited_value_json) BETWEEN 1 AND 100000), edited_value_sha256 TEXT, note TEXT, created_at TEXT NOT NULL CHECK (length(created_at)>0),
    CHECK ((event_kind='approved_edit' AND edited_value_json IS NOT NULL AND edited_value_sha256 IS NOT NULL) OR (event_kind!='approved_edit' AND edited_value_json IS NULL AND edited_value_sha256 IS NULL)),
    CHECK (edited_value_sha256 IS NULL OR (length(edited_value_sha256)=64 AND edited_value_sha256=lower(edited_value_sha256) AND edited_value_sha256 NOT GLOB '*[^0-9a-f]*'))
);
CREATE INDEX idx_career_fact_review_history ON career_fact_review_events(candidate_id, created_at DESC, id DESC);

CREATE TABLE career_profile_versions (
    id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'), version_number INTEGER NOT NULL UNIQUE CHECK (version_number >= 1), parent_version_id TEXT REFERENCES career_profile_versions(id) ON DELETE RESTRICT,
    content_fingerprint TEXT NOT NULL CHECK (length(content_fingerprint)=64 AND content_fingerprint=lower(content_fingerprint) AND content_fingerprint NOT GLOB '*[^0-9a-f]*'), origin TEXT NOT NULL CHECK (origin IN ('manual','approved_facts','imported')), created_at TEXT NOT NULL CHECK (length(created_at)>0)
);
CREATE INDEX idx_career_profile_versions_history ON career_profile_versions(version_number DESC);

CREATE TABLE career_profile_facts (
    id TEXT PRIMARY KEY CHECK (length(id)=32 AND id=lower(id) AND id NOT GLOB '*[^0-9a-f]*'), profile_version_id TEXT NOT NULL REFERENCES career_profile_versions(id) ON DELETE RESTRICT,
    logical_fact_id TEXT NOT NULL CHECK (length(logical_fact_id) BETWEEN 1 AND 128), fact_type TEXT NOT NULL CHECK (length(fact_type) BETWEEN 1 AND 160), normalized_key TEXT NOT NULL CHECK (length(normalized_key) BETWEEN 1 AND 512), canonical_value_json TEXT NOT NULL CHECK (length(canonical_value_json) BETWEEN 1 AND 100000), value_sha256 TEXT NOT NULL CHECK (length(value_sha256)=64 AND value_sha256=lower(value_sha256) AND value_sha256 NOT GLOB '*[^0-9a-f]*'), origin TEXT NOT NULL CHECK (origin IN ('manual','approved_candidate','imported')), privacy_classification TEXT NOT NULL CHECK (privacy_classification IN ('public','private','sensitive')), sort_order INTEGER NOT NULL CHECK (sort_order >= 0), UNIQUE(profile_version_id, logical_fact_id), UNIQUE(profile_version_id, sort_order)
);
CREATE INDEX idx_career_profile_facts_version ON career_profile_facts(profile_version_id, sort_order, id);

CREATE TABLE career_profile_fact_sources (
    profile_fact_id TEXT NOT NULL REFERENCES career_profile_facts(id) ON DELETE RESTRICT, candidate_id TEXT NOT NULL REFERENCES career_fact_candidates(id) ON DELETE RESTRICT, PRIMARY KEY (profile_fact_id, candidate_id)
);
CREATE INDEX idx_career_profile_fact_sources_candidate ON career_profile_fact_sources(candidate_id, profile_fact_id);

CREATE TABLE career_profile_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1), current_profile_version_id TEXT REFERENCES career_profile_versions(id) ON DELETE RESTRICT, revision INTEGER NOT NULL CHECK (revision >= 0), next_version_number INTEGER NOT NULL CHECK (next_version_number >= 1)
);
INSERT INTO career_profile_state(singleton, current_profile_version_id, revision, next_version_number) VALUES (1, NULL, 0, 1);
"""

# v14 contains only reconstructible ranking state.  Preference values and
# descriptions deliberately remain in their authoritative tables/jobs rows.
V14_DDL = """
CREATE TABLE review_ranking_authority (
    singleton INTEGER PRIMARY KEY CHECK (typeof(singleton)='integer' AND singleton=1),
    target_revision INTEGER NOT NULL CHECK (typeof(target_revision)='integer' AND target_revision >= 0),
    explicit_revision INTEGER NOT NULL CHECK (typeof(explicit_revision)='integer' AND explicit_revision >= 0),
    explicit_fingerprint TEXT NOT NULL CHECK (length(explicit_fingerprint)=64 AND explicit_fingerprint NOT GLOB '*[^0-9a-f]*'),
    opportunity_revision INTEGER NOT NULL CHECK (typeof(opportunity_revision)='integer' AND opportunity_revision >= 0),
    opportunity_fingerprint TEXT NOT NULL CHECK (length(opportunity_fingerprint)=64 AND opportunity_fingerprint NOT GLOB '*[^0-9a-f]*'),
    opportunity_count INTEGER NOT NULL CHECK (typeof(opportunity_count)='integer' AND opportunity_count >= 0),
    learned_revision INTEGER NOT NULL CHECK (typeof(learned_revision)='integer' AND learned_revision >= 0),
    learned_fingerprint TEXT NOT NULL CHECK (length(learned_fingerprint)=64 AND learned_fingerprint NOT GLOB '*[^0-9a-f]*'),
    scorer_version TEXT NOT NULL CHECK (length(scorer_version) BETWEEN 1 AND 64),
    normalizer_version TEXT NOT NULL CHECK (length(normalizer_version) BETWEEN 1 AND 64),
    generation_format_version TEXT NOT NULL CHECK (length(generation_format_version) BETWEEN 1 AND 64),
    required_mode_mask INTEGER NOT NULL CHECK (typeof(required_mode_mask)='integer' AND required_mode_mask BETWEEN 0 AND 3),
    state TEXT NOT NULL CHECK (state IN ('ready','rebuild_required','building','failed')),
    claim_token TEXT CHECK (claim_token IS NULL OR (length(claim_token)=32 AND claim_token NOT GLOB '*[^0-9a-f]*')),
    claim_target_revision INTEGER CHECK (claim_target_revision IS NULL OR (typeof(claim_target_revision)='integer' AND claim_target_revision >= 0)),
    claim_mode_mask INTEGER CHECK (claim_mode_mask IS NULL OR (typeof(claim_mode_mask)='integer' AND claim_mode_mask BETWEEN 0 AND 3)),
    claimed_at TEXT, heartbeat_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(attempt_count)='integer' AND attempt_count >= 0),
    last_failed_at TEXT, retry_not_before TEXT,
    failure_code TEXT CHECK (failure_code IS NULL OR failure_code IN ('database','validation','lost_claim','cancelled','unknown')),
    updated_at TEXT NOT NULL CHECK (length(updated_at)>0),
    CHECK ((state='building') = (claim_token IS NOT NULL AND claim_target_revision IS NOT NULL AND claim_mode_mask IS NOT NULL)),
    CHECK (claim_token IS NULL OR (claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL))
);
CREATE TABLE review_ranking_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_fingerprint TEXT NOT NULL UNIQUE CHECK (length(content_fingerprint)=64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'),
    target_revision INTEGER NOT NULL CHECK (typeof(target_revision)='integer' AND target_revision >= 0),
    learned_mode INTEGER NOT NULL CHECK (typeof(learned_mode)='integer' AND learned_mode IN (0,1)),
    explicit_revision INTEGER NOT NULL CHECK (typeof(explicit_revision)='integer' AND explicit_revision >= 0),
    explicit_fingerprint TEXT NOT NULL CHECK (length(explicit_fingerprint)=64 AND explicit_fingerprint NOT GLOB '*[^0-9a-f]*'),
    opportunity_revision INTEGER NOT NULL CHECK (typeof(opportunity_revision)='integer' AND opportunity_revision >= 0),
    opportunity_fingerprint TEXT NOT NULL CHECK (length(opportunity_fingerprint)=64 AND opportunity_fingerprint NOT GLOB '*[^0-9a-f]*'),
    opportunity_count INTEGER NOT NULL CHECK (typeof(opportunity_count)='integer' AND opportunity_count >= 0),
    learned_revision INTEGER NOT NULL CHECK (typeof(learned_revision)='integer' AND learned_revision >= 0),
    learned_fingerprint TEXT NOT NULL CHECK (length(learned_fingerprint)=64 AND learned_fingerprint NOT GLOB '*[^0-9a-f]*'),
    scorer_version TEXT NOT NULL CHECK (length(scorer_version) BETWEEN 1 AND 64),
    normalizer_version TEXT NOT NULL CHECK (length(normalizer_version) BETWEEN 1 AND 64),
    generation_format_version TEXT NOT NULL CHECK (length(generation_format_version) BETWEEN 1 AND 64),
    item_count INTEGER NOT NULL CHECK (typeof(item_count)='integer' AND item_count >= 0),
    items_fingerprint TEXT NOT NULL CHECK (length(items_fingerprint)=64 AND items_fingerprint NOT GLOB '*[^0-9a-f]*'),
    completed_at TEXT NOT NULL CHECK (length(completed_at)>0),
    UNIQUE(target_revision, learned_mode)
);
CREATE TABLE review_ranking_items (
    generation_id INTEGER NOT NULL CHECK (typeof(generation_id)='integer') REFERENCES review_ranking_generations(id) ON DELETE CASCADE,
    opportunity_key TEXT NOT NULL,
    group_id TEXT,
    representative_job_id INTEGER NOT NULL CHECK (typeof(representative_job_id)='integer') REFERENCES jobs(id) ON DELETE RESTRICT,
    opportunity_fingerprint TEXT NOT NULL CHECK (length(opportunity_fingerprint)=64 AND opportunity_fingerprint NOT GLOB '*[^0-9a-f]*'),
    rank_ordinal INTEGER NOT NULL CHECK (typeof(rank_ordinal)='integer' AND rank_ordinal >= 1),
    raw_score INTEGER NOT NULL CHECK (typeof(raw_score)='integer'),
    public_match_score INTEGER NOT NULL CHECK (typeof(public_match_score)='integer' AND public_match_score BETWEEN 0 AND 100),
    CHECK (
        (group_id IS NULL AND opportunity_key = 'job:' || representative_job_id)
        OR
        (group_id IS NOT NULL AND opportunity_key = 'group:' || group_id)
    ),
    PRIMARY KEY (generation_id, opportunity_key),
    UNIQUE (generation_id, rank_ordinal)
);
CREATE INDEX idx_review_ranking_items_order ON review_ranking_items(generation_id, rank_ordinal);
CREATE INDEX idx_review_ranking_items_rep ON review_ranking_items(representative_job_id);
CREATE TABLE review_ranking_current (
    learned_mode INTEGER PRIMARY KEY CHECK (typeof(learned_mode)='integer' AND learned_mode IN (0,1)),
    generation_id INTEGER NOT NULL CHECK (typeof(generation_id)='integer') REFERENCES review_ranking_generations(id) ON DELETE RESTRICT,
    bound_target_revision INTEGER NOT NULL CHECK (typeof(bound_target_revision)='integer' AND bound_target_revision >= 0),
    bound_at TEXT NOT NULL CHECK (length(bound_at)>0)
);
CREATE INDEX idx_review_ranking_generations_mode ON review_ranking_generations(learned_mode, target_revision DESC, id DESC);
"""


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
            explicit_ranking_revision INTEGER NOT NULL DEFAULT 0 CHECK (typeof(explicit_ranking_revision)='integer' AND explicit_ranking_revision >= 0),
            explicit_ranking_fingerprint TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' CHECK (length(explicit_ranking_fingerprint)=64),
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
    connection.executescript(V13_DDL)
    # Bootstrap is v14; migration uses the same normative DDL in a transaction.
    connection.executescript(V14_DDL)

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
        "UPDATE preference_state SET explicit_ranking_fingerprint = ?", ("0" * 64,)
    )

    connection.execute(
        """
        INSERT INTO schema_metadata(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    from careeros.database.review_ranking import (
        EMPTY_LEARNED_FINGERPRINT,
        REVIEW_RANKING_GENERATION_FORMAT_VERSION,
        REVIEW_RANKING_NORMALIZER_VERSION,
        REVIEW_RANKING_SCORER_VERSION,
        corpus_fingerprint,
    )
    from careeros.preferences.models import (
        ExplicitPreferenceProfile,
        explicit_ranking_authority_fingerprint,
    )

    empty_explicit = explicit_ranking_authority_fingerprint(ExplicitPreferenceProfile())
    empty_learned = EMPTY_LEARNED_FINGERPRINT
    empty_opportunities = corpus_fingerprint(())
    connection.execute(
        "UPDATE preference_state SET explicit_ranking_fingerprint=? WHERE singleton=1",
        (empty_explicit,),
    )
    connection.execute(
        "INSERT INTO review_ranking_authority(singleton,target_revision,explicit_revision,explicit_fingerprint,opportunity_revision,opportunity_fingerprint,opportunity_count,learned_revision,learned_fingerprint,scorer_version,normalizer_version,generation_format_version,required_mode_mask,state,updated_at) VALUES (1,0,0,?,0,?,0,0,?,?,?, ?,3,'rebuild_required',?)",
        (
            empty_explicit,
            empty_opportunities,
            empty_learned,
            REVIEW_RANKING_SCORER_VERSION,
            REVIEW_RANKING_NORMALIZER_VERSION,
            REVIEW_RANKING_GENERATION_FORMAT_VERSION,
            datetime.now(UTC).isoformat(),
        ),
    )

    connection.commit()
