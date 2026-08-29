import json
from pathlib import Path

from careeros.locking import collection_lock, lock_path_for


def test_lock_metadata_is_bounded_and_private(tmp_path: Path):
    database = tmp_path / "private.sqlite3"
    with collection_lock(database, command="bounded operation"):
        lock = lock_path_for(database)
        assert lock.stat().st_mode & 0o777 == 0o600
        metadata = json.loads(lock.read_text())
        assert set(metadata) == {"pid", "command", "acquired_at"}
        assert str(database) not in lock.read_text()
