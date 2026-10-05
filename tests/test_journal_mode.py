"""The database must not run in WAL mode (#292).

In production the file lives on GlusterFS. WAL keeps a shared-memory index in
the -shm file, which SQLite documents as unsafe on network filesystems, and a
WAL database on the same mount was corrupted in 2026-08.
"""

import sqlite3

from src.core.models import Database


def test_new_database_uses_a_rollback_journal(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    conn = db.get_connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    assert not (tmp_path / "t.db-shm").exists()


def test_database_left_in_wal_mode_is_converted(tmp_path):
    path = tmp_path / "t.db"
    Database(str(path))
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()

    conn = Database(str(path)).get_connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
