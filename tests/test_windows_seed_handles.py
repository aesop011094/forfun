"""Windows denies moving an SQLite file while a connection still holds it."""
from contextlib import closing
from pathlib import Path
import sqlite3

from portable import desktop_launcher


def test_first_run_closes_sqlite_handles_before_rename(tmp_path, monkeypatch):
    directory = tmp_path / "足球 資料"
    directory.mkdir()
    seed = directory / "seed.db"
    destination = directory / "football.db"
    original_connect = sqlite3.connect
    original_replace = Path.replace
    with closing(original_connect(seed)) as connection:
        connection.execute("CREATE TABLE matches (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO matches VALUES ('real-seed-row')")
        connection.commit()
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def tracking_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    def windows_file_move(path, target):
        # This catches the real WinError32 regression on Linux as well.
        if any(not connection.closed for connection in connections):
            raise PermissionError("Windows sharing violation: SQLite handle remains open")
        return original_replace(path, target)

    monkeypatch.setattr(desktop_launcher.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(Path, "replace", windows_file_move)
    desktop_launcher.copy_seed_once(seed, destination)
    assert len(connections) == 2
    assert all(connection.closed for connection in connections)
    with closing(original_connect(destination)) as connection:
        assert connection.execute("SELECT id FROM matches").fetchone()[0] == "real-seed-row"
    assert not list(directory.glob("football.db.seed-*"))
