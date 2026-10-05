"""Local asyncio coordinator for durable Review ranking rebuilds."""

from __future__ import annotations

import asyncio
from pathlib import Path

from careeros.database.connection import connect_current_rw
from careeros.ranking.rebuild import rebuild_once, recover_and_rebuild

REDISCOVERY_SECONDS = 5.0


def _startup_recovery(path: Path) -> None:
    """Audit once at startup and leave durable state ready for the worker."""
    with connect_current_rw(path) as connection:
        recover_and_rebuild(connection)


def _rebuild_pass(path: Path) -> None:
    """Perform one bounded durable claim/rebuild attempt in a private thread DB."""
    with connect_current_rw(path) as connection:
        rebuild_once(connection)


async def run_ranking_worker(
    path: Path,
    wake: asyncio.Event,
    stop: asyncio.Event,
    *,
    audit: asyncio.Event | None = None,
) -> None:
    """Own the local rebuild loop until application shutdown."""
    try:
        await asyncio.to_thread(_startup_recovery, path)
    except Exception:
        # The API startup validator has already established schema/material
        # safety. A damaged ranking authority remains fail-closed while the
        # durable polling loop keeps retrying without taking down the server.
        pass
    while not stop.is_set():
        try:
            await asyncio.wait_for(wake.wait(), timeout=REDISCOVERY_SECONDS)
        except TimeoutError:
            pass
        wake.clear()
        if stop.is_set():
            break
        try:
            if audit is not None and audit.is_set():
                audit.clear()
                await asyncio.to_thread(_startup_recovery, path)
            else:
                await asyncio.to_thread(_rebuild_pass, path)
        except Exception:
            # Failure details remain durable in the authority row; the worker
            # stays alive for the next durable retry/backoff window.
            continue


__all__ = ("REDISCOVERY_SECONDS", "run_ranking_worker")
