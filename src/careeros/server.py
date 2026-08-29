from __future__ import annotations

import sqlite3
from pathlib import Path

from careeros.database.admin import _check


def validate_startup_database(database: Path) -> None:
    """Validate configured database without creating or migrating anything."""
    try:
        state = _check(database)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise ValueError("CareerOS requires a current schema v9 database") from exc
    if state != "current":
        raise ValueError("CareerOS requires a current schema v9 database")


def serve(*, port: int = 8000, ipv6: bool = False, database: Path) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    validate_startup_database(database)
    import uvicorn

    from careeros.api.main import create_app

    host = "::1" if ipv6 else "127.0.0.1"
    print(
        f"CareerOS serving at http://[{host}]:{port}"
        if ipv6
        else f"CareerOS serving at http://{host}:{port}"
    )
    uvicorn.run(
        create_app(database),
        host=host,
        port=port,
        proxy_headers=False,
        forwarded_allow_ips="",
        reload=False,
        access_log=False,
    )
