"""Tests for ReactomeProposalModel and ReactomeMappingModel methods added in Phase 25-01."""
import os
import tempfile

import pytest

from src.core.models import (
    Database,
    ReactomeMappingModel,
    ReactomeProposalModel,
)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database with all tables initialized."""
    fd, path = tempfile.mkstemp(suffix=".db")
    db = Database(path)
    db.init_db()
    yield db
    os.close(fd)
    os.unlink(path)


@pytest.fixture
def reactome_proposal_model(temp_db):
    return ReactomeProposalModel(temp_db)


@pytest.fixture
def reactome_mapping_model(temp_db):
    return ReactomeMappingModel(temp_db)


# ---------------------------------------------------------------------------
# ReactomeProposalModel.update_proposal_status
# ---------------------------------------------------------------------------


class TestReactomeProposalUpdateStatus:
    def test_approve_sets_approved_metadata(self, reactome_proposal_model, temp_db):
        proposal_id = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 100",
            ke_title="Test KE",
            reactome_id="R-HSA-1234",
            pathway_name="Test pathway",
            confidence_level="high",
            species="Homo sapiens",
            provider_username="github:user",
            suggestion_score=0.8,
        )
        assert proposal_id is not None

        ok = reactome_proposal_model.update_proposal_status(
            proposal_id=proposal_id,
            status="approved",
            admin_username="github:admin",
            admin_notes="ok",
        )
        assert ok is True

        conn = temp_db.get_connection()
        try:
            row = conn.execute(
                "SELECT status, approved_by, approved_at, admin_notes "
                "FROM ke_reactome_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        finally:
            conn.close()

        assert row["status"] == "approved"
        assert row["approved_by"] == "github:admin"
        assert row["admin_notes"] == "ok"
        assert row["approved_at"] is not None

    def test_reject_sets_rejected_metadata(self, reactome_proposal_model, temp_db):
        proposal_id = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 101",
            ke_title="Test KE 2",
            reactome_id="R-HSA-5678",
            pathway_name="Other pathway",
            confidence_level="medium",
            species="Homo sapiens",
            provider_username="github:user2",
            suggestion_score=0.5,
        )
        assert proposal_id is not None

        ok = reactome_proposal_model.update_proposal_status(
            proposal_id=proposal_id,
            status="rejected",
            admin_username="github:admin",
            admin_notes="not relevant",
        )
        assert ok is True

        conn = temp_db.get_connection()
        try:
            row = conn.execute(
                "SELECT status, rejected_by, rejected_at, admin_notes "
                "FROM ke_reactome_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        finally:
            conn.close()

        assert row["status"] == "rejected"
        assert row["rejected_by"] == "github:admin"
        assert row["admin_notes"] == "not relevant"
        assert row["rejected_at"] is not None

    def test_invalid_status_returns_false(self, reactome_proposal_model):
        ok = reactome_proposal_model.update_proposal_status(
            proposal_id=1,
            status="bogus",
            admin_username="github:admin",
            admin_notes="x",
        )
        assert ok is False

    def test_nonexistent_proposal_returns_true_no_raise(self, reactome_proposal_model):
        # Mirrors GO behavior: UPDATE matches 0 rows, no exception, returns True.
        ok = reactome_proposal_model.update_proposal_status(
            proposal_id=99999,
            status="approved",
            admin_username="github:admin",
            admin_notes="x",
        )
        assert ok is True


# ---------------------------------------------------------------------------
# ReactomeProposalModel.get_proposal_by_id — Phase 37 ASMT-04
# ---------------------------------------------------------------------------


class TestReactomeProposalGetById:
    """get_proposal_by_id must return the four step-answer columns so the
    admin detail JSON (Plan 03) can surface them without em-dashes."""

    def test_get_proposal_by_id_returns_four_step_columns(
        self, reactome_proposal_model
    ):
        """Round-trip: create with step answers, retrieve by id, assert columns present."""
        proposal_id = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 500",
            ke_title="Step-answer test",
            reactome_id="R-HSA-5001",
            pathway_name="Pathway step test",
            confidence_level="high",
            species="Homo sapiens",
            provider_username="github:user",
            suggestion_score=0.75,
            proposed_relationship="causative",
            proposed_basis="known",
            proposed_specificity="specific",
            proposed_coverage="complete",
        )
        assert isinstance(proposal_id, int)

        row = reactome_proposal_model.get_proposal_by_id(proposal_id)
        assert row is not None, "get_proposal_by_id returned None"
        assert row["proposed_relationship"] == "causative", (
            "proposed_relationship missing from get_proposal_by_id result; "
            "extend the SELECT column list in ReactomeProposalModel."
        )
        assert row["proposed_basis"] == "known"
        assert row["proposed_specificity"] == "specific"
        assert row["proposed_coverage"] == "complete"

    def test_get_proposal_by_id_none_step_columns_allowed(
        self, reactome_proposal_model
    ):
        """Proposals without step answers return NULL (v1 legacy) — not KeyError."""
        proposal_id = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 501",
            ke_title="Legacy step test",
            reactome_id="R-HSA-5002",
            pathway_name="Pathway legacy",
            confidence_level="low",
            species="Homo sapiens",
            provider_username="github:user",
        )
        row = reactome_proposal_model.get_proposal_by_id(proposal_id)
        assert row is not None
        # Columns must be present (not missing from SELECT) even if NULL
        for col in ("proposed_relationship", "proposed_basis",
                    "proposed_specificity", "proposed_coverage"):
            assert col in row, (
                f"Column '{col}' missing from get_proposal_by_id dict; "
                "extend the SELECT list in ReactomeProposalModel.get_proposal_by_id."
            )
        assert row["proposed_relationship"] is None


# ---------------------------------------------------------------------------
# ReactomeProposalModel.get_all_proposals
# ---------------------------------------------------------------------------


class TestReactomeProposalGetAll:
    def _create(self, model, ke_id, reactome_id, status_set=None):
        pid = model.create_new_pair_reactome_proposal(
            ke_id=ke_id,
            ke_title=f"Title for {ke_id}",
            reactome_id=reactome_id,
            pathway_name=f"Pathway {reactome_id}",
            confidence_level="high",
            species="Homo sapiens",
            provider_username="github:curator",
            suggestion_score=0.7,
        )
        if status_set:
            model.update_proposal_status(pid, status_set, "github:admin", "n/a")
        return pid

    def test_filter_by_pending_returns_only_pending(self, reactome_proposal_model):
        self._create(reactome_proposal_model, "KE 200", "R-HSA-1001")
        self._create(reactome_proposal_model, "KE 201", "R-HSA-1002", status_set="approved")
        self._create(reactome_proposal_model, "KE 202", "R-HSA-1003", status_set="rejected")

        pending = reactome_proposal_model.get_all_proposals(status="pending")
        assert len(pending) == 1
        assert pending[0]["ke_id"] == "KE 200"
        assert pending[0]["status"] == "pending"

    def test_status_none_returns_all(self, reactome_proposal_model):
        self._create(reactome_proposal_model, "KE 210", "R-HSA-2001")
        self._create(reactome_proposal_model, "KE 211", "R-HSA-2002", status_set="approved")

        rows = reactome_proposal_model.get_all_proposals(status=None)
        assert len(rows) == 2

    def test_results_left_join_mapping_columns(self, reactome_proposal_model):
        self._create(reactome_proposal_model, "KE 220", "R-HSA-3001")
        rows = reactome_proposal_model.get_all_proposals(status="pending")
        assert len(rows) == 1
        row = rows[0]
        # Joined columns present (mapping_id is NULL → joined values are None)
        for key in (
            "mapping_ke_id",
            "mapping_ke_title",
            "mapping_reactome_id",
            "mapping_pathway_name",
            "current_confidence_level",
            "current_species",
        ):
            assert key in row
            assert row[key] is None

    def test_results_ordered_by_created_at_desc(
        self, reactome_proposal_model, temp_db
    ):
        first = self._create(reactome_proposal_model, "KE 230", "R-HSA-4001")
        second = self._create(reactome_proposal_model, "KE 231", "R-HSA-4002")
        # SQLite CURRENT_TIMESTAMP has 1-second granularity; force a distinct
        # created_at for the second row so DESC ordering is deterministic.
        conn = temp_db.get_connection()
        try:
            conn.execute(
                "UPDATE ke_reactome_proposals SET created_at = "
                "datetime(created_at, '+1 second') WHERE id = ?",
                (second,),
            )
            conn.commit()
        finally:
            conn.close()

        rows = reactome_proposal_model.get_all_proposals()
        # Most-recent first
        assert rows[0]["id"] == second
        assert rows[1]["id"] == first


# ---------------------------------------------------------------------------
# ReactomeMappingModel.update_reactome_mapping (Task 2)
# ---------------------------------------------------------------------------


class TestReactomeMappingUpdate:
    def _create_mapping(self, model):
        return model.create_mapping(
            ke_id="KE 300",
            ke_title="Mapping KE",
            reactome_id="R-HSA-9001",
            pathway_name="Pathway X",
            species="Homo sapiens",
            confidence_level="high",
            suggestion_score=0.6,
            created_by="github:user",
        )

    def test_update_carry_fields(self, reactome_mapping_model, temp_db):
        mapping_id = self._create_mapping(reactome_mapping_model)
        assert mapping_id is not None

        ok = reactome_mapping_model.update_reactome_mapping(
            mapping_id=mapping_id,
            approved_by_curator="github:admin",
            approved_at_curator="2026-05-05T12:00:00",
            suggestion_score=0.85,
            proposed_by="github:curator",
        )
        assert ok is True

        conn = temp_db.get_connection()
        try:
            row = conn.execute(
                "SELECT approved_by_curator, approved_at_curator, suggestion_score, "
                "proposed_by, updated_at FROM ke_reactome_mappings WHERE id = ?",
                (mapping_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["approved_by_curator"] == "github:admin"
        assert row["approved_at_curator"] == "2026-05-05T12:00:00"
        assert row["suggestion_score"] == 0.85
        assert row["proposed_by"] == "github:curator"
        assert row["updated_at"] is not None

    def test_update_with_no_kwargs_returns_false(self, reactome_mapping_model):
        mapping_id = self._create_mapping(reactome_mapping_model)
        ok = reactome_mapping_model.update_reactome_mapping(mapping_id=mapping_id)
        assert ok is False

    def test_unknown_kwarg_silently_dropped(self, reactome_mapping_model, temp_db):
        mapping_id = self._create_mapping(reactome_mapping_model)
        # #245 made confidence_level writable, reversing the half of D-02 that
        # locked the tier at proposal creation — that lock made a wrong tier
        # permanent, correctable only by deleting the mapping and re-creating
        # it. The other two exclusions are schema facts, not decisions, and
        # still hold: ke_reactome_mappings has no connection_type column
        # (Reactome has no connection type at all, #248) and no updated_by
        # column, so attribution rides on approved_by_curator + proposed_by.
        import inspect

        sig = inspect.signature(reactome_mapping_model.update_reactome_mapping)
        assert "confidence_level" in sig.parameters
        assert "connection_type" not in sig.parameters
        assert "updated_by" not in sig.parameters
        # Existing confidence persists unchanged when only approval-time
        # provenance fields are written.
        ok = reactome_mapping_model.update_reactome_mapping(
            mapping_id=mapping_id, approved_by_curator="github:admin"
        )
        assert ok is True
        conn = temp_db.get_connection()
        try:
            row = conn.execute(
                "SELECT confidence_level FROM ke_reactome_mappings WHERE id = ?",
                (mapping_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["confidence_level"] == "high"


# ---------------------------------------------------------------------------
# ReactomeMappingModel.check_reactome_mapping_exists_with_proposals (Task 2)
# ---------------------------------------------------------------------------


class TestReactomeCheckExistsWithProposals:
    def test_empty_db_returns_no_blocker(self, reactome_mapping_model):
        result = reactome_mapping_model.check_reactome_mapping_exists_with_proposals(
            "KE 400", "R-HSA-7001"
        )
        assert result["pair_exists"] is False
        assert result["blocking_type"] is None
        assert result["existing"] is None
        assert "actions" in result

    def test_approved_mapping_blocks(self, reactome_mapping_model):
        reactome_mapping_model.create_mapping(
            ke_id="KE 401",
            ke_title="Curator KE",
            reactome_id="R-HSA-7002",
            pathway_name="Pathway A",
            species="Homo sapiens",
            confidence_level="high",
            suggestion_score=0.9,
            created_by="github:user",
        )
        # Approve carry-fields so existing dict is meaningful
        # (still works without; the mapping row exists)
        result = reactome_mapping_model.check_reactome_mapping_exists_with_proposals(
            "KE 401", "R-HSA-7002"
        )
        assert result["pair_exists"] is True
        assert result["blocking_type"] == "approved_mapping"
        ex = result["existing"]
        assert ex["ke_id"] == "KE 401"
        assert ex["ke_title"] == "Curator KE"
        assert ex["reactome_id"] == "R-HSA-7002"
        assert ex["pathway_name"] == "Pathway A"
        assert ex["confidence_level"] == "high"
        # connection_type must NOT appear (Reactome has no connection_type)
        assert "connection_type" not in ex

    def test_pending_proposal_blocks(
        self, reactome_mapping_model, reactome_proposal_model
    ):
        reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 402",
            ke_title="Pending KE",
            reactome_id="R-HSA-7003",
            pathway_name="Pending Pathway",
            confidence_level="medium",
            species="Homo sapiens",
            provider_username="github:proposer",
            suggestion_score=0.4,
        )
        result = reactome_mapping_model.check_reactome_mapping_exists_with_proposals(
            "KE 402", "R-HSA-7003"
        )
        assert result["pair_exists"] is True
        assert result["blocking_type"] == "pending_proposal"
        ex = result["existing"]
        assert ex["ke_id"] == "KE 402"
        assert ex["ke_title"] == "Pending KE"
        assert ex["reactome_id"] == "R-HSA-7003"
        assert ex["pathway_name"] == "Pending Pathway"
        assert ex["submitted_by"] == "github:proposer"
        assert "submitted_at" in ex

    def test_pending_existing_dict_excludes_admin_notes(
        self, reactome_proposal_model, reactome_mapping_model, temp_db
    ):
        pid = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 403",
            ke_title="No leak KE",
            reactome_id="R-HSA-7004",
            pathway_name="Sensitive Pathway",
            confidence_level="low",
            species="Homo sapiens",
            provider_username="github:p",
            suggestion_score=0.2,
        )
        # Force admin_notes to be present on the row (still pending).
        conn = temp_db.get_connection()
        try:
            conn.execute(
                "UPDATE ke_reactome_proposals SET admin_notes = ? WHERE id = ?",
                ("internal note", pid),
            )
            conn.commit()
        finally:
            conn.close()

        result = reactome_mapping_model.check_reactome_mapping_exists_with_proposals(
            "KE 403", "R-HSA-7004"
        )
        ex = result["existing"]
        assert "admin_notes" not in ex


# ---------------------------------------------------------------------------
# Phase 25 review H-2 — partial-unique index on pending proposals
# ---------------------------------------------------------------------------


class TestReactomeProposalsPendingUniqueIndex:
    """Phase 25 review H-2: a partial-unique index on
    ke_reactome_proposals(ke_id, reactome_id) WHERE status='pending'
    AND mapping_id IS NULL must reject duplicate concurrent submits.

    Pre-fix: two concurrent /submit_reactome_mapping calls could each
    pass the application-level check_reactome_mapping_exists_with_proposals
    TOCTOU window and both INSERT, leaving two pending rows for the same
    (ke_id, reactome_id). Post-fix: the second INSERT raises IntegrityError
    on the partial-unique index, the model layer surfaces it as the
    DUPLICATE_PENDING sentinel, and the route layer maps that to a 409.
    """

    def test_partial_unique_index_exists(self, temp_db):
        """The migration must have created the partial-unique index."""
        conn = temp_db.get_connection()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name = 'idx_reactome_proposals_pending_pair'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, (
            "H-2 regression: partial-unique index "
            "idx_reactome_proposals_pending_pair was not created by "
            "_migrate_reactome_proposals_pending_unique_index"
        )
        sql = row["sql"]
        assert "ke_reactome_proposals" in sql
        assert "(ke_id, reactome_id)" in sql or "(ke_id,reactome_id)" in sql
        assert "status" in sql and "pending" in sql

    def test_duplicate_pending_returns_sentinel(self, reactome_proposal_model):
        """Second concurrent submit for the same pair must return the
        DUPLICATE_PENDING sentinel, NOT silently insert another row."""
        first = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 9301",
            ke_title="Race test",
            reactome_id="R-HSA-9301",
            pathway_name="Some pathway",
            confidence_level="medium",
            species="Homo sapiens",
            provider_username="github:racer1",
            suggestion_score=0.5,
        )
        assert isinstance(first, int) and first > 0

        # Simulate a concurrent insert: same (ke_id, reactome_id), still
        # pending, still mapping_id IS NULL — must hit the partial-unique
        # index and return the sentinel.
        second = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 9301",
            ke_title="Race test",
            reactome_id="R-HSA-9301",
            pathway_name="Some pathway",
            confidence_level="medium",
            species="Homo sapiens",
            provider_username="github:racer2",
            suggestion_score=0.5,
        )
        assert second == ReactomeProposalModel.DUPLICATE_PENDING, (
            f"H-2 regression: expected DUPLICATE_PENDING sentinel, got "
            f"{second!r} — a second pending row was silently inserted."
        )

    def test_duplicate_pending_does_not_insert_second_row(
        self, reactome_proposal_model, temp_db
    ):
        """After a duplicate-rejected submit, only ONE pending row exists."""
        reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 9302",
            ke_title="Race test 2",
            reactome_id="R-HSA-9302",
            pathway_name="Some pathway",
            confidence_level="low",
            species="Homo sapiens",
            provider_username="github:racer1",
        )
        reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 9302",
            ke_title="Race test 2",
            reactome_id="R-HSA-9302",
            pathway_name="Some pathway",
            confidence_level="low",
            species="Homo sapiens",
            provider_username="github:racer2",
        )

        conn = temp_db.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ke_reactome_proposals "
                "WHERE ke_id = ? AND reactome_id = ? "
                "AND status = 'pending' AND mapping_id IS NULL",
                ("KE 9302", "R-HSA-9302"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1, (
            f"H-2 regression: expected 1 pending row for the pair, found "
            f"{count} — partial-unique index did not block the duplicate."
        )

    def test_resubmission_allowed_after_rejection(
        self, reactome_proposal_model
    ):
        """Once a pending proposal is rejected, the partial-unique index
        no longer covers it (status != 'pending'), so a fresh submit for
        the same pair must succeed — the index must NOT block the
        legitimate "resubmit after rejection" workflow."""
        first = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 9303",
            ke_title="Resubmit test",
            reactome_id="R-HSA-9303",
            pathway_name="Some pathway",
            confidence_level="low",
            species="Homo sapiens",
            provider_username="github:user1",
        )
        assert isinstance(first, int)

        # Reject the first proposal
        ok = reactome_proposal_model.update_proposal_status(
            proposal_id=first,
            status="rejected",
            admin_username="github:admin",
            admin_notes="not relevant",
        )
        assert ok is True

        # Resubmit must succeed — partial index ignores rejected rows
        second = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id="KE 9303",
            ke_title="Resubmit test",
            reactome_id="R-HSA-9303",
            pathway_name="Some pathway",
            confidence_level="medium",
            species="Homo sapiens",
            provider_username="github:user2",
        )
        assert isinstance(second, int) and second > 0, (
            f"Resubmission after rejection must be allowed; got {second!r}"
        )
