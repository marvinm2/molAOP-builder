"""Phase 32 H-2 port (GO sibling): race-safe pending-duplicate handling on
the `ke_go_proposals` table.

Mirrors `tests/test_proposal_models.py` (WP — Plan 32-03) one-to-one, plus
two additional ordering-invariant tests that lock in the CONTEXT.md L27
locked decision: pre-migration cleanup keeper-selection MUST sort by
`created_at ASC, id ASC` — `created_at` is the PRIMARY sort key, `id` is
only the tiebreaker / NULL fallback. A naive `MIN(id)` shortcut would
pass the canonical Reactome-style tests but fail the ordering-invariant
tests below.

Target table: `ke_go_proposals` (NOT `go_proposals`).
Target index: `idx_go_proposals_pending_pair` on (ke_id, go_id) WHERE
`status='pending' AND mapping_id IS NULL`.
"""
import os
import sqlite3
import tempfile

import pytest

from src.core.models import Database, GoProposalModel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database with all tables + migrations."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db = Database(path)  # init_db() runs in __init__
    yield db
    os.close(fd)
    os.unlink(path)


@pytest.fixture
def go_proposal_model(temp_db):
    return GoProposalModel(temp_db)


# ---------------------------------------------------------------------------
# Helpers for pre-migration cleanup tests
# ---------------------------------------------------------------------------


def _create_bare_go_proposals_table(conn):
    """Build a minimal `ke_go_proposals` table with the columns needed by the
    pre-migration cleanup pass. Uses INTEGER PRIMARY KEY (not AUTOINCREMENT)
    so we can explicitly assign id values in any order — autoincrement would
    refuse to go backwards.

    Mirrors the production schema's cleanup-relevant columns; intentionally
    omits the many other columns added across migrations (user_name,
    user_email, etc.) because the cleanup query only reads/writes these.
    """
    conn.execute(
        """
        CREATE TABLE ke_go_proposals (
            id INTEGER PRIMARY KEY,
            mapping_id INTEGER,
            ke_id TEXT,
            go_id TEXT,
            status TEXT DEFAULT 'pending',
            admin_notes TEXT,
            rejected_by TEXT,
            rejected_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _seed_pending_duplicate(conn, *, row_id, ke_id, go_id, created_at):
    """Insert one pending+new-pair row with an explicit id and created_at."""
    conn.execute(
        """
        INSERT INTO ke_go_proposals (id, mapping_id, ke_id, go_id, status, created_at)
        VALUES (?, NULL, ?, ?, 'pending', ?)
        """,
        (row_id, ke_id, go_id, created_at),
    )


# ---------------------------------------------------------------------------
# Phase 32 H-2 — partial-unique index + DUPLICATE_PENDING sentinel
# ---------------------------------------------------------------------------


class TestGoProposalsPendingUniqueIndex:
    """The partial-unique index on `ke_go_proposals(ke_id, go_id)` WHERE
    status='pending' AND mapping_id IS NULL must reject duplicate concurrent
    submits, surface as the DUPLICATE_PENDING sentinel, and release the slot
    once the pending row is rejected.
    """

    def test_partial_unique_index_exists(self, temp_db):
        """After migration, sqlite_master contains
        idx_go_proposals_pending_pair on ke_go_proposals(ke_id, go_id)
        WHERE status='pending' AND mapping_id IS NULL.
        """
        conn = temp_db.get_connection()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name = 'idx_go_proposals_pending_pair'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, (
            "Phase 32 H-2 regression: partial-unique index "
            "idx_go_proposals_pending_pair was not created by "
            "_migrate_go_proposals_pending_unique_index"
        )
        sql = row["sql"]
        assert "ke_go_proposals" in sql
        assert "(ke_id, go_id)" in sql or "(ke_id,go_id)" in sql
        assert "status" in sql and "pending" in sql
        assert "mapping_id" in sql

    def test_concurrent_inserts_blocked(self, go_proposal_model, temp_db):
        """Two near-simultaneous create_new_pair_go_proposal calls for the
        same (ke_id, go_id) → first returns int row_id, second returns
        GoProposalModel.DUPLICATE_PENDING. Exactly 1 pending row remains.
        """
        first = go_proposal_model.create_new_pair_go_proposal(
            ke_id="KE 9301",
            ke_title="GO race test",
            go_id="GO:9999301",
            go_name="some bp",
            connection_type="describes",
            confidence_level="medium",
            provider_username="github:racer1",
            suggestion_score=0.5,
        )
        assert isinstance(first, int) and first > 0

        second = go_proposal_model.create_new_pair_go_proposal(
            ke_id="KE 9301",
            ke_title="GO race test",
            go_id="GO:9999301",
            go_name="some bp",
            connection_type="describes",
            confidence_level="medium",
            provider_username="github:racer2",
            suggestion_score=0.5,
        )
        assert second == GoProposalModel.DUPLICATE_PENDING, (
            f"Phase 32 H-2 regression: expected DUPLICATE_PENDING sentinel, "
            f"got {second!r} — a second pending row was silently inserted."
        )

        conn = temp_db.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ke_go_proposals "
                "WHERE ke_id = ? AND go_id = ? "
                "AND status = 'pending' AND mapping_id IS NULL",
                ("KE 9301", "GO:9999301"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, (
            f"Phase 32 H-2 regression: expected exactly 1 pending row for "
            f"the pair after duplicate-rejected submit, found {count}."
        )

    def test_post_rejection_allows_resubmit(self, go_proposal_model):
        """After the pending proposal for a pair is rejected, the partial
        index no longer covers it (status != 'pending'), so a fresh submit
        for the same pair must succeed.
        """
        first = go_proposal_model.create_new_pair_go_proposal(
            ke_id="KE 9303",
            ke_title="GO resubmit test",
            go_id="GO:9999303",
            go_name="some bp",
            connection_type="describes",
            confidence_level="low",
            provider_username="github:user1",
        )
        assert isinstance(first, int)

        ok = go_proposal_model.update_go_proposal_status(
            proposal_id=first,
            status="rejected",
            admin_username="github:admin",
            admin_notes="not relevant",
        )
        assert ok is True

        second = go_proposal_model.create_new_pair_go_proposal(
            ke_id="KE 9303",
            ke_title="GO resubmit test",
            go_id="GO:9999303",
            go_name="some bp",
            connection_type="describes",
            confidence_level="medium",
            provider_username="github:user2",
        )
        assert isinstance(second, int) and second > 0, (
            f"Resubmission after rejection must be allowed; got {second!r}"
        )


# ---------------------------------------------------------------------------
# Phase 32 H-2 — pre-migration cleanup pass
# ---------------------------------------------------------------------------


class TestGoPreMigrationCleanup:
    """The cleanup pass must run BEFORE CREATE UNIQUE INDEX so existing
    duplicate pending+new-pair rows don't crash startup. Keeper-selection
    sorts by `created_at ASC, id ASC` (created_at PRIMARY, id tiebreaker /
    NULL fallback per CONTEXT.md L27 locked decision).
    """

    def _run_migration(self, db_path):
        """Instantiate Database on the seeded file — this runs all
        migrations including _migrate_go_proposals_pending_unique_index.
        """
        return Database(db_path)

    def test_pre_migration_cleanup_auto_resolves_duplicates(self, tmp_path):
        """Baseline / id-aligned-with-created_at case: id-order matches
        created_at-order. Oldest by both stays pending; newer is auto-rejected
        with the EXACT CONTEXT.md strings.
        """
        db_path = str(tmp_path / "test.db")

        # Seed the bare table with duplicates BEFORE running migrations.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            _create_bare_go_proposals_table(conn)
            # id=1 is older (2025-01-01); id=2 is newer (2025-06-01).
            _seed_pending_duplicate(
                conn, row_id=1, ke_id="KE 9999", go_id="GO:9999999",
                created_at="2025-01-01 00:00:00",
            )
            _seed_pending_duplicate(
                conn, row_id=2, ke_id="KE 9999", go_id="GO:9999999",
                created_at="2025-06-01 00:00:00",
            )
            conn.commit()
        finally:
            conn.close()

        # Run migration (Database.__init__ → init_db → migration block).
        self._run_migration(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            keeper = conn.execute(
                "SELECT status, rejected_by, rejected_at, admin_notes "
                "FROM ke_go_proposals WHERE id = 1"
            ).fetchone()
            loser = conn.execute(
                "SELECT status, rejected_by, rejected_at, admin_notes "
                "FROM ke_go_proposals WHERE id = 2"
            ).fetchone()
        finally:
            conn.close()

        assert keeper["status"] == "pending"
        assert keeper["rejected_by"] is None
        assert loser["status"] == "rejected"
        assert loser["rejected_by"] == "system:phase-32-migration"
        assert loser["rejected_at"] is not None
        assert loser["admin_notes"] == (
            "Auto-resolved by Phase 32 H-2 migration: "
            "superseded by older pending proposal #1"
        )

    def test_pre_migration_cleanup_prefers_created_at_over_id(self, tmp_path):
        """created_at-vs-id DISAGREEMENT case: id-order DISAGREES with
        created_at-order. Locks in CONTEXT.md L27 primary sort key —
        `created_at` is PRIMARY, `id` is only the tiebreaker. A `MIN(id)`
        shortcut would (incorrectly) pick id=1 as the keeper and fail this
        test.

        Inserted: id=2 with older created_at (2024-01-01); id=1 with newer
        created_at (2024-06-01). Expected: id=2 keeps pending (older
        created_at wins despite larger id); id=1 is auto-rejected and points
        to keeper #2 in admin_notes.
        """
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            _create_bare_go_proposals_table(conn)
            # Insert id=2 FIRST with the older timestamp.
            _seed_pending_duplicate(
                conn, row_id=2, ke_id="KE 9998", go_id="GO:9999998",
                created_at="2024-01-01 00:00:00",
            )
            # Then id=1 with the newer timestamp — id-order DISAGREES.
            _seed_pending_duplicate(
                conn, row_id=1, ke_id="KE 9998", go_id="GO:9999998",
                created_at="2024-06-01 00:00:00",
            )
            conn.commit()
        finally:
            conn.close()

        self._run_migration(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row_2 = conn.execute(
                "SELECT status, rejected_by, admin_notes "
                "FROM ke_go_proposals WHERE id = 2"
            ).fetchone()
            row_1 = conn.execute(
                "SELECT status, rejected_by, admin_notes "
                "FROM ke_go_proposals WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

        # Older created_at (id=2) is the keeper despite the larger id.
        assert row_2["status"] == "pending", (
            "created_at must be the PRIMARY sort key (CONTEXT.md L27). "
            "id=2 has the older created_at and MUST be kept; a MIN(id) "
            "shortcut would incorrectly pick id=1."
        )
        assert row_2["rejected_by"] is None
        # Newer created_at (id=1) is the loser; admin_notes references
        # keeper id=2.
        assert row_1["status"] == "rejected"
        assert row_1["rejected_by"] == "system:phase-32-migration"
        assert row_1["admin_notes"] == (
            "Auto-resolved by Phase 32 H-2 migration: "
            "superseded by older pending proposal #2"
        )

    def test_pre_migration_cleanup_falls_back_to_id_when_created_at_tied(
        self, tmp_path
    ):
        """NULL / identical-created_at fallback case: locks in CONTEXT.md
        L27 "falling back to `id` if absent" clause. When created_at is
        tied (identical timestamps — chosen over NULL because the
        production schema declares created_at with a CURRENT_TIMESTAMP
        default and existing data could realistically have identical
        timestamps via batch import), id is the tiebreaker.

        Note: the schema declares `created_at TIMESTAMP DEFAULT
        CURRENT_TIMESTAMP` (NOT NULL not enforced at the column level),
        but identical-timestamp variant is simpler and exercises the
        same `id ASC` tiebreaker branch.
        """
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            _create_bare_go_proposals_table(conn)
            _seed_pending_duplicate(
                conn, row_id=1, ke_id="KE 9997", go_id="GO:9999997",
                created_at="2025-01-01 12:00:00",
            )
            _seed_pending_duplicate(
                conn, row_id=2, ke_id="KE 9997", go_id="GO:9999997",
                created_at="2025-01-01 12:00:00",
            )
            conn.commit()
        finally:
            conn.close()

        self._run_migration(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row_1 = conn.execute(
                "SELECT status, rejected_by, admin_notes "
                "FROM ke_go_proposals WHERE id = 1"
            ).fetchone()
            row_2 = conn.execute(
                "SELECT status, rejected_by, admin_notes "
                "FROM ke_go_proposals WHERE id = 2"
            ).fetchone()
        finally:
            conn.close()

        # Tied created_at → smaller id (1) wins via the tiebreaker.
        assert row_1["status"] == "pending"
        assert row_1["rejected_by"] is None
        assert row_2["status"] == "rejected"
        assert row_2["rejected_by"] == "system:phase-32-migration"
        assert row_2["admin_notes"] == (
            "Auto-resolved by Phase 32 H-2 migration: "
            "superseded by older pending proposal #1"
        )


# ---------------------------------------------------------------------------
# Phase 32 H-2 — /submit_go_mapping route layer: 409 with check_go_mapping shape
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_client_filedb(client, tmp_path, monkeypatch):
    """Route-test fixture that pins the GO go_proposal_model + go_mapping_model
    to a file-backed SQLite DB (with all migrations run) instead of the
    default TestingConfig `:memory:` path. `:memory:` is per-connection
    in SQLite, so the migrations the conftest's `client` fixture runs
    on its temp file aren't visible to the app's blueprint-bound model
    instances (which were wired during create_app() to `:memory:`). For
    a full route-level integration test we need a single file the app's
    models all share.
    """
    from src.blueprints import api as api_module

    db_path = str(tmp_path / "route_test.db")
    real_db = Database(db_path)  # runs init_db + all migrations
    real_go_mapping = api_module.go_mapping_model
    real_go_proposal = api_module.go_proposal_model

    # Repoint the blueprint-bound models at the file-backed Database.
    monkeypatch.setattr(real_go_mapping, "db", real_db)
    monkeypatch.setattr(real_go_proposal, "db", real_db)

    with client.session_transaction() as sess:
        sess["user"] = {"username": "github:testuser", "email": "test@example.com"}
    return client


class TestSubmitGoMappingDuplicatePendingRoute:
    """Route-layer regression: /submit_go_mapping returns 409 with the
    check_go_mapping_exists_with_proposals shape (NOT Reactome's bare
    `{error, blocking_type}` shape) on duplicate-pending. Existing GO
    clients already handle this shape via /check_go_entry.
    """

    def test_submit_go_mapping_returns_409_with_check_shape_on_duplicate_pending(
        self, auth_client_filedb
    ):
        auth_client = auth_client_filedb
        form = {
            "ke_id": "KE 9501",
            "ke_title": "Route race test",
            "go_id": "GO:9999501",
            "go_name": "Route race bp",
            "connection_type": "describes",
            "confidence_level": "medium",
            "go_namespace": "biological_process",
        }
        first_resp = auth_client.post("/submit_go_mapping", data=form)
        assert first_resp.status_code == 200, (
            f"First submit must succeed; got {first_resp.status_code} "
            f"{first_resp.get_data(as_text=True)}"
        )

        second_resp = auth_client.post("/submit_go_mapping", data=form)
        assert second_resp.status_code == 409, (
            f"Duplicate-pending submit must return 409; got "
            f"{second_resp.status_code} {second_resp.get_data(as_text=True)}"
        )
        body = second_resp.get_json()
        assert body is not None
        assert body.get("pair_exists") is True, (
            "Response must use check_go_mapping_exists_with_proposals shape "
            "(pair_exists=True), NOT Reactome's bare {error, blocking_type} "
            "shape."
        )
        assert body.get("blocking_type") == "pending_proposal"
        existing = body.get("existing")
        assert existing is not None
        assert existing.get("ke_id") == "KE 9501"
        assert existing.get("go_id") == "GO:9999501"
