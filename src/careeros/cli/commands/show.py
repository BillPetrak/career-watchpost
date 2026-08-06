"""The ``show`` command."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from careeros.cli.common import no_job_found_message
from careeros.database.connection import connect
from careeros.database.jobs import get_job_by_id
from careeros.database.schema import initialize_schema
from careeros.text import clean_description


def run_show(job_id: int, database_path: Path) -> int:
    """Display a single job by its database ID."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            job = get_job_by_id(connection, job_id)
    except (OSError, sqlite3.Error) as error:
        print(f"Show failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if job is None:
        print(no_job_found_message(job_id), file=sys.stderr)
        return 1

    print(f"ID:               {job.id}")
    print(f"Title:            {job.title}")
    print(f"Company:          {job.company or 'N/A'}")
    print(f"Location:         {job.location or 'N/A'}")
    print(f"Remote status:    {job.remote_status or 'N/A'}")
    print(f"Employment type:  {job.employment_type or 'N/A'}")
    print(f"Source:           {job.source}")
    print(f"Source job ID:    {job.source_job_id or 'N/A'}")
    print(f"Published:        {job.published_at or 'N/A'}")
    print(f"Collected:        {job.collected_at}")
    print(f"URL:              {job.url or 'N/A'}")
    print()
    print("Description:")
    print(clean_description(job.description) if job.description else "N/A")

    return 0


def run_show_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch the ``show`` command."""
    if arguments.job_id < 1:
        parser.error("job-id must be a positive integer")

    return run_show(
        job_id=arguments.job_id,
        database_path=arguments.database,
    )
