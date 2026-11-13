"""The ``export`` command family (deterministic read-only exports)."""

from __future__ import annotations

import argparse
import sqlite3
import sys

from careeros.database.connection import connect_read_only
from careeros.export.models import ExportError, ExportFormat, ExportRequest
from careeros.export.serializers import render_export
from careeros.export.service import build_records
from careeros.export.writer import stdout_stream, write_export, write_file_export


def run_export(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Export deterministic stored state (read-only, no lock, no writes).

    A missing database file yields a valid empty export without ever creating
    the file. Output goes to stdout by default; ``--output`` writes a file and
    refuses to clobber it unless ``--overwrite`` is given. A broken pipe (e.g.
    ``careeros export jobs | head``) is not an error.
    """
    command = arguments.export_command

    if arguments.limit < 0:
        parser.error("--limit must be 0 (no cap) or a positive integer")
    if command == "ranked" and arguments.query is None:
        parser.error("export ranked requires --query")
    if command in ("ranked", "review") and arguments.profile is None:
        parser.error(f"export {command} requires --profile")

    export_format = ExportFormat(arguments.format)
    request = ExportRequest(
        format=export_format,
        output=arguments.output,
        limit=arguments.limit,
        overwrite=arguments.overwrite,
        grouped=getattr(arguments, "grouped", False),
        clean_descriptions=getattr(arguments, "clean_descriptions", False),
        query=getattr(arguments, "query", None),
        profile=getattr(arguments, "profile", None),
        remote=getattr(arguments, "remote", False) or None,
        location=getattr(arguments, "location", None),
        source=getattr(arguments, "source", None),
        employment_type=getattr(arguments, "employment_type", None),
        min_score=getattr(arguments, "min_score", 0),
        state=getattr(arguments, "state", "unreviewed"),
        include_tracked=getattr(arguments, "include_tracked", False),
        use_feedback=not getattr(arguments, "no_feedback", False),
        use_adaptive=not getattr(arguments, "no_adaptive", False),
        failed_only=getattr(arguments, "failed", False),
    )

    connection: sqlite3.Connection | None = None
    try:
        if arguments.database.exists():
            connection = connect_read_only(arguments.database)
        result = build_records(
            connection,
            command,
            request,
            config_path=(
                str(arguments.sources_config)
                if getattr(arguments, "sources_config", None) is not None
                else None
            ),
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Export failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    text = render_export(result, export_format)
    if arguments.output is not None:
        try:
            return write_file_export(
                arguments.output, text, overwrite=arguments.overwrite
            )
        except ExportError as error:
            print(str(error), file=sys.stderr)
            return 1

    return write_export(text, stdout_stream())


def run_export_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch an ``export`` subcommand."""
    return run_export(arguments, parser)
