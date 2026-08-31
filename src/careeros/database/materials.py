from __future__ import annotations

import sqlite3
from typing import cast


def material_row(c: sqlite3.Connection, material_id: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        c.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone(),
    )


def list_material_rows(
    c: sqlite3.Connection,
    *,
    include_retired: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[sqlite3.Row]:
    where = "" if include_retired else "WHERE m.status='active'"
    return c.execute(
        f"""SELECT m.*, count(v.id) version_count,
      cv.id current_id, cv.version_number current_number,
      cv.display_filename current_filename,
      cv.declared_media_type current_media_type,
      cv.byte_size current_byte_size, cv.created_at current_created_at
      FROM materials m LEFT JOIN material_versions v ON v.material_id=m.id
      LEFT JOIN material_versions cv ON cv.id=(
        SELECT v2.id FROM material_versions v2
        WHERE v2.material_id=m.id
        ORDER BY v2.version_number DESC,v2.id LIMIT 1)
      {where} GROUP BY m.id ORDER BY m.updated_at DESC,m.id LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()


def version_rows(c: sqlite3.Connection, material_id: str) -> list[sqlite3.Row]:
    return c.execute(
        "SELECT * FROM material_versions WHERE material_id=? "
        "ORDER BY version_number DESC,id",
        (material_id,),
    ).fetchall()
