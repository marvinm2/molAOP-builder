"""#276: backup_db.sh kept a 0-byte file when the database was locked.

Two faults, the second defeating the guard against the first:

1. The `sqlite3` CLI opens with no busy timeout, so a concurrent writer made
   `.backup` fail outright ("database is locked") instead of waiting. Nothing
   removed the empty file it left, and it sorts newest — so anyone reaching for
   "the latest backup", under exactly the pressure that makes you reach for one,
   got an empty file.
2. `PRAGMA integrity_check` returns "ok" on a zero-length file, because SQLite
   treats one as a valid empty database. The one check standing between a failed
   backup and a kept one passed on the precise failure it most needed to catch:
   it is a corruption guard, and was being asked to serve as a completion guard.

The nightly 02:00 UTC job never saw this because nothing is curating then, which
is why five nights of backups were intact and the first ad-hoc daytime run was
not. Intermittent, and only looked at during an incident.
"""
import os
import shutil
import sqlite3
import subprocess
import threading
import time

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "backup_db.sh",
)

pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None or shutil.which("bash") is None,
    reason="requires the sqlite3 CLI and bash",
)


@pytest.fixture
def db_and_backups(tmp_path):
    """A database shaped like the real one: a populated `mappings` table."""
    db_path = tmp_path / "ke_wp_mapping.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE mappings (id INTEGER PRIMARY KEY, ke_id TEXT)")
    conn.executemany(
        "INSERT INTO mappings (ke_id) VALUES (?)", [("KE 1",), ("KE 2",)]
    )
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    return db_path, backup_dir


def run_backup(db_path, backup_dir, **env_overrides):
    env = {
        **os.environ,
        "DB_PATH": str(db_path),
        "BACKUP_DIR": str(backup_dir),
        # The real floor is sized for a ~40 MB database; these fixtures are a
        # few pages, so the test pins its own rather than asserting on 40 MB.
        "MIN_BACKUP_BYTES": "1024",
        **env_overrides,
    }
    return subprocess.run(
        ["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=120
    )


def backups(backup_dir):
    return sorted(backup_dir.glob("ke_wp_mapping_*.db"))


class TestSuccessfulBackup:
    def test_backup_is_written_and_reported(self, db_and_backups):
        db_path, backup_dir = db_and_backups
        result = run_backup(db_path, backup_dir)
        assert result.returncode == 0, result.stderr
        assert "[BACKUP OK]" in result.stdout
        # The success line states what was verified, so a log reader can tell a
        # real backup from a file that merely exists.
        assert "integrity ok" in result.stdout
        assert "2 mappings" in result.stdout

        written = backups(backup_dir)
        assert len(written) == 1
        assert written[0].stat().st_size > 0

    def test_the_backup_holds_the_data(self, db_and_backups):
        db_path, backup_dir = db_and_backups
        run_backup(db_path, backup_dir)
        conn = sqlite3.connect(backups(backup_dir)[0])
        try:
            assert conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 2
        finally:
            conn.close()


class TestConcurrentWriter:
    def test_backup_waits_for_a_writer_instead_of_failing(self, db_and_backups):
        """The reported failure, reproduced: a write transaction held open.

        Without a busy timeout this is where `.backup` returned "database is
        locked" and left the empty file behind.
        """
        db_path, backup_dir = db_and_backups
        released = threading.Event()

        def hold_write_lock():
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("INSERT INTO mappings (ke_id) VALUES ('KE 3')")
            time.sleep(2)
            conn.execute("COMMIT")
            conn.close()
            released.set()

        writer = threading.Thread(target=hold_write_lock)
        writer.start()
        time.sleep(0.3)  # let the exclusive lock be taken before backing up
        try:
            result = run_backup(db_path, backup_dir)
        finally:
            writer.join()

        assert released.is_set()
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert len(backups(backup_dir)) == 1
        # It waited for the commit rather than racing it.
        assert "3 mappings" in result.stdout

    def test_a_lock_that_never_clears_leaves_no_file(self, db_and_backups):
        """When waiting does not help, fail loudly and clean up after yourself."""
        db_path, backup_dir = db_and_backups
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("INSERT INTO mappings (ke_id) VALUES ('KE 4')")
        try:
            result = run_backup(db_path, backup_dir, BUSY_TIMEOUT_MS="500")
        finally:
            conn.execute("ROLLBACK")
            conn.close()

        assert result.returncode != 0
        assert "[BACKUP ERROR]" in result.stderr
        assert backups(backup_dir) == [], "a failed backup must not be kept"


class TestIncompleteBackupsAreNotKept:
    def test_an_empty_file_would_not_survive_the_checks(self, db_and_backups):
        """Guard the guard: assert PRAGMA integrity_check really does pass on a
        zero-length file, so the size floor is load-bearing rather than
        belt-and-braces. If SQLite ever changes this, the comment explaining why
        the floor exists should change with it."""
        db_path, backup_dir = db_and_backups
        empty = backup_dir / "ke_wp_mapping_00000000_000000.db"
        empty.touch()
        check = subprocess.run(
            ["sqlite3", str(empty), "PRAGMA integrity_check;"],
            capture_output=True, text=True,
        )
        assert check.stdout.strip() == "ok"

    def test_a_stale_empty_backup_is_pruned(self, db_and_backups):
        """Files left by runs predating these checks age out immediately rather
        than sitting for a full retention period, sorting newest."""
        db_path, backup_dir = db_and_backups
        empty = backup_dir / "ke_wp_mapping_20200101_000000.db"
        empty.touch()

        result = run_backup(db_path, backup_dir)
        assert result.returncode == 0
        assert not empty.exists()
        assert len(backups(backup_dir)) == 1

    def test_a_database_without_mappings_is_refused(self, tmp_path):
        """Structurally valid but not a backup of this database."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE mappings (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        result = run_backup(db_path, backup_dir)
        assert result.returncode != 0
        assert "holds no mappings" in result.stderr
        assert backups(backup_dir) == []

    def test_a_source_that_is_not_a_database_leaves_nothing(self, tmp_path):
        junk = tmp_path / "not-a-database.db"
        junk.write_text("this is not a database")
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        result = run_backup(junk, backup_dir)
        assert result.returncode != 0
        assert "[BACKUP ERROR]" in result.stderr
        assert backups(backup_dir) == []
