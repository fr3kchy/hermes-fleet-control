from pathlib import Path
from hermes_fleet.db import Database


def test_database_has_version_and_passes_integrity_check(tmp_path: Path):
    db = Database(tmp_path / "fleet.db")
    assert db.integrity_check() == "ok"
    with db.connect() as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 2
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(tasks)")}
    assert "idx_tasks_status_created" in indexes
