"""Output handling for exports: file-open policy and broken-pipe semantics.

Export output is written to stdout by default or to a file given by
``--output``. Writing to a closed pipe (e.g. ``careeros export jobs | head``)
is not an error: the consumer chose to stop reading, so the export is
considered successful and the process exits 0. Genuine write failures (disk
full, permission denied) surface as :class:`ExportError` for the CLI to report.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from careeros.export.models import ExportError

_DEFAULT_ENCODING = "utf-8"


def open_output(path: Path, *, overwrite: bool) -> TextIO:
    """Open a file for export output, refusing to clobber without ``overwrite``.

    Raises :class:`ExportError` when the file already exists (and ``overwrite``
    is False) or the parent directory does not exist.
    """
    if path.exists() and not overwrite:
        raise ExportError(
            f"Output file already exists: {path} (use --overwrite to replace it)."
        )
    parent = path.parent
    if not parent.exists():
        raise ExportError(f"Output directory does not exist: {parent}.")
    try:
        return path.open("w", encoding=_DEFAULT_ENCODING, newline="")
    except OSError as error:
        raise ExportError(f"Failed to open output file {path}: {error}") from error


def write_export(text: str, stream: TextIO) -> int:
    """Write serialized export text to a stream and return the exit code.

    A broken pipe returns 0 (not an error); a real write failure raises
    :class:`ExportError`.
    """
    try:
        stream.write(text)
        stream.flush()
    except BrokenPipeError:
        return 0
    except OSError as error:
        raise ExportError(f"Failed to write export: {error}") from error
    return 0


def stdout_stream() -> TextIO:
    """Return the stdout stream (helper keeps broken-pipe policy in one place)."""
    return sys.stdout


__all__: tuple[str, ...] = (
    "open_output",
    "stdout_stream",
    "write_export",
)
