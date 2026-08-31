from __future__ import annotations

import sqlite3
from pathlib import Path

from careeros.database.admin import _check
from careeros.materials.models import StorageError


def validate_startup_database(
    database: Path, materials_root_path: Path | None = None
) -> None:
    """Validate configured database without creating or migrating anything."""
    try:
        state = _check(database)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise ValueError("CareerOS requires a current schema v10 database") from exc
    if state != "current":
        raise ValueError("CareerOS requires a current schema v10 database")
    # Validate every selected root state, including an unbound database. This
    # remains read-only: absent/empty roots are admissible only while unbound,
    # and populated or unsafe roots are never adopted at startup.
    try:
        from careeros.materials.storage import ensure_binding
        from careeros.settings import materials_root

        with sqlite3.connect(
            f"file:{database.resolve()}?mode=ro", uri=True
        ) as connection:
            row = connection.execute(
                "SELECT store_id FROM material_store WHERE singleton=1"
            ).fetchone()
        ensure_binding(
            materials_root_path or materials_root(),
            str(row[0]) if row is not None else None,
            allow_create=False,
        )
    except (OSError, sqlite3.Error, ValueError, StorageError) as exc:
        raise ValueError("CareerOS requires a matching materials store") from exc


def serve(
    *,
    port: int = 8000,
    ipv6: bool = False,
    database: Path,
    materials_root: Path | None = None,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    validate_startup_database(database, materials_root)
    import uvicorn

    from careeros.api.main import create_app

    host = "::1" if ipv6 else "127.0.0.1"
    print(
        f"CareerOS serving at http://[{host}]:{port}"
        if ipv6
        else f"CareerOS serving at http://{host}:{port}"
    )
    uvicorn.run(
        create_app(database, materials_root),
        host=host,
        port=port,
        proxy_headers=False,
        forwarded_allow_ips="",
        reload=False,
        access_log=False,
        workers=1,
    )
