"""
Wave 0 test scaffold for Phase 38 bulk-approve backend (Plan 38-01).

Covers:
- Per-resource fixtures: wp_admin_client, go_admin_client, reactome_admin_client
- Seed helpers: _seed_wp_proposal, _seed_go_proposal, _seed_reactome_proposal
- Per-resource test classes: TestWPBulkApprove, TestGOBulkApprove, TestReactomeBulkApprove
- TestBulkApproveShared: static/smoke tests

Route-dependent test bodies are marked with pytest.mark.skip(reason="route lands in 38-02")
so the suite stays green in Wave 0 while the fault-injection seam and fixture infra are
exercisable now.
"""
import logging
import os
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Per-resource fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def wp_admin_client():
    """
    Test client wired with fresh WP models on a temp-file DB and an
    authenticated admin session (github:testadmin).

    Yields (test_client, mapping_model, proposal_model, db).
    """
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    from src.core.models import Database, MappingModel, ProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    mm = MappingModel(db)
    pm = ProposalModel(db)

    orig_mm = admin_mod.mapping_model
    orig_pm = admin_mod.proposal_model

    admin_mod.mapping_model = mm
    admin_mod.proposal_model = pm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            with test_client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield test_client, mm, pm, db

    admin_mod.mapping_model = orig_mm
    admin_mod.proposal_model = orig_pm

    os.close(fd)
    os.unlink(db_path)


@pytest.fixture
def go_admin_client():
    """
    Test client wired with fresh GO models on a temp-file DB and an
    authenticated admin session (github:testadmin).

    Yields (test_client, go_mapping_model, go_proposal_model, db).
    """
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    from src.core.models import Database, GoMappingModel, GoProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    gm = GoMappingModel(db)
    gpm = GoProposalModel(db)

    orig_gm = admin_mod.go_mapping_model
    orig_gpm = admin_mod.go_proposal_model

    admin_mod.go_mapping_model = gm
    admin_mod.go_proposal_model = gpm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            with test_client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield test_client, gm, gpm, db

    admin_mod.go_mapping_model = orig_gm
    admin_mod.go_proposal_model = orig_gpm

    os.close(fd)
    os.unlink(db_path)


@pytest.fixture
def reactome_admin_client():
    """
    Test client wired with fresh Reactome models on a temp-file DB and an
    authenticated admin session (github:testadmin).

    Yields (test_client, reactome_mapping_model, reactome_proposal_model, db).
    """
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    from src.core.models import Database, ReactomeMappingModel, ReactomeProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    rm = ReactomeMappingModel(db)
    rpm = ReactomeProposalModel(db)

    orig_rm = admin_mod.reactome_mapping_model
    orig_rpm = admin_mod.reactome_proposal_model

    admin_mod.reactome_mapping_model = rm
    admin_mod.reactome_proposal_model = rpm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            with test_client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield test_client, rm, rpm, db

    admin_mod.reactome_mapping_model = orig_rm
    admin_mod.reactome_proposal_model = orig_rpm

    os.close(fd)
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Seed helpers (NOT parametrized — D-16)
# ---------------------------------------------------------------------------


def _seed_wp_proposal(
    pm,
    ke_id="KE 1001",
    wp_id="WP1234",
    wp_title="MAPK signaling pathway",
    confidence_level="high",
    provider_username="github:curator",
    suggestion_score=0.75,
):
    """Seed a single new-pair WP proposal and return its id."""
    proposal_id = pm.create_new_pair_proposal(
        ke_id=ke_id,
        ke_title=f"Title for {ke_id}",
        wp_id=wp_id,
        wp_title=wp_title,
        connection_type="causative",
        confidence_level=confidence_level,
        provider_username=provider_username,
        suggestion_score=suggestion_score,
    )
    assert proposal_id is not None
    return proposal_id


def _seed_go_proposal(
    gpm,
    ke_id="KE 2001",
    go_id="GO:0051403",
    go_name="stress-activated MAPK cascade",
    confidence_level="high",
    provider_username="github:curator",
    suggestion_score=0.80,
    go_namespace="biological_process",
):
    """Seed a single new-pair GO proposal and return its id."""
    proposal_id = gpm.create_new_pair_go_proposal(
        ke_id=ke_id,
        ke_title=f"Title for {ke_id}",
        go_id=go_id,
        go_name=go_name,
        connection_type="related",
        confidence_level=confidence_level,
        provider_username=provider_username,
        suggestion_score=suggestion_score,
        go_namespace=go_namespace,
    )
    assert proposal_id is not None
    return proposal_id


def _seed_reactome_proposal(
    rpm,
    ke_id="KE 3001",
    reactome_id="R-HSA-1234567",
    pathway_name="MAPK signaling",
    confidence_level="high",
    provider_username="github:curator",
    suggestion_score=0.75,
):
    """Seed a single new-pair Reactome proposal and return its id."""
    proposal_id = rpm.create_new_pair_reactome_proposal(
        ke_id=ke_id,
        ke_title=f"Title for {ke_id}",
        reactome_id=reactome_id,
        pathway_name=pathway_name,
        confidence_level=confidence_level,
        species="Homo sapiens",
        provider_username=provider_username,
        suggestion_score=suggestion_score,
    )
    assert proposal_id is not None
    return proposal_id


# ---------------------------------------------------------------------------
# TestWPBulkApprove
# ---------------------------------------------------------------------------


class TestWPBulkApprove:
    """Tests for POST /admin/proposals/bulk-approve (WP resource).

    The bulk-approve route lands in Plan 38-02.  Route-dependent assertions
    are skipped here; the fault-injection seam and fixture/seed infra are
    fully exercisable in Wave 0.
    """

    def test_fault_injection_seam_wp(self, wp_admin_client):
        """Verify the monkeypatch fault-injection seam is exercisable.

        Seeds 5 WP proposals and confirms that patching _approve_on_conn to
        raise on the 3rd call prevents any mapping from being committed —
        proving the seam is in place for the 38-02 route test.

        Route-dependent assertions (HTTP response, DB count after POST) are
        deferred to 38-02 where the route exists.
        """
        client, mm, pm, db = wp_admin_client

        pids = [
            _seed_wp_proposal(pm, ke_id=f"KE 100{i}", wp_id=f"WP100{i}")
            for i in range(1, 6)
        ]

        call_count = [0]
        original = mm._approve_on_conn

        def failing_helper(conn, proposal, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise sqlite3.IntegrityError("forced failure on call 3")
            return original(conn, proposal, *args, **kwargs)

        mm._approve_on_conn = failing_helper
        try:
            conn = mm.db.get_connection()
            approved_uuids = []
            proposals = [pm.get_proposal_by_id(pid) for pid in pids]
            try:
                for proposal in proposals:
                    uuid = mm._approve_on_conn(conn, proposal, "github:testadmin")
                    approved_uuids.append(uuid)
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                conn.close()
        finally:
            mm._approve_on_conn = original

        # Fault injection fired on call 3; entire batch rolled back
        assert call_count[0] == 3, "expected failure on 3rd call"
        assert len(approved_uuids) == 2, "first two succeeded before rollback"

        # Verify 0 mappings in DB (rollback worked)
        conn = db.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM mappings WHERE ke_id IN (?, ?, ?, ?, ?)",
                ("KE 1001", "KE 1002", "KE 1003", "KE 1004", "KE 1005"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0, "rollback must revert all writes including the first two"

        # All proposals remain pending
        for pid in pids:
            p = pm.get_proposal_by_id(pid)
            assert p["status"] == "pending"

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_bulk_approve_returns_uuids(self, wp_admin_client):
        """POST /admin/proposals/bulk-approve returns mapping UUIDs in approved[]."""
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_preflight_not_found(self, wp_admin_client):
        """Not-found IDs appear in failed[], valid IDs are processed."""
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_preflight_not_pending(self, wp_admin_client):
        """Already-approved/rejected IDs appear in failed[]."""
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_audit_log_emitted(self, wp_admin_client, caplog):
        """AUDIT bulk-approve wp log line contains approved UUIDs."""
        pass


# ---------------------------------------------------------------------------
# TestGOBulkApprove
# ---------------------------------------------------------------------------


class TestGOBulkApprove:
    """Tests for POST /admin/go-proposals/bulk-approve (GO resource)."""

    def test_fault_injection_seam_go(self, go_admin_client):
        """Verify the monkeypatch fault-injection seam is exercisable for GO.

        Seeds 5 GO proposals and confirms that patching _approve_on_conn to
        raise on the 3rd call rolls back all writes.
        """
        client, gm, gpm, db = go_admin_client

        pids = [
            _seed_go_proposal(gpm, ke_id=f"KE 200{i}", go_id=f"GO:0000{i:03d}")
            for i in range(1, 6)
        ]

        call_count = [0]
        original = gm._approve_on_conn

        def failing_helper(conn, proposal, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise sqlite3.IntegrityError("forced failure on call 3")
            return original(conn, proposal, *args, **kwargs)

        gm._approve_on_conn = failing_helper
        try:
            conn = gm.db.get_connection()
            approved_uuids = []
            proposals = [gpm.get_go_proposal_by_id(pid) for pid in pids]
            try:
                for proposal in proposals:
                    uuid = gm._approve_on_conn(conn, proposal, "github:testadmin")
                    approved_uuids.append(uuid)
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                conn.close()
        finally:
            gm._approve_on_conn = original

        assert call_count[0] == 3
        assert len(approved_uuids) == 2

        conn = db.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ke_go_mappings WHERE ke_id IN (?, ?, ?, ?, ?)",
                ("KE 2001", "KE 2002", "KE 2003", "KE 2004", "KE 2005"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

        for pid in pids:
            p = gpm.get_go_proposal_by_id(pid)
            assert p["status"] == "pending"

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_bulk_approve_returns_uuids(self, go_admin_client):
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_preflight_not_found(self, go_admin_client):
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_preflight_not_pending(self, go_admin_client):
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_audit_log_emitted(self, go_admin_client, caplog):
        pass


# ---------------------------------------------------------------------------
# TestReactomeBulkApprove
# ---------------------------------------------------------------------------


class TestReactomeBulkApprove:
    """Tests for POST /admin/reactome-proposals/bulk-approve (Reactome resource)."""

    def test_fault_injection_seam_reactome(self, reactome_admin_client):
        """Verify the monkeypatch fault-injection seam is exercisable for Reactome.

        Seeds 5 Reactome proposals and confirms that patching
        _create_approved_on_conn to raise on the 3rd call rolls back all writes.
        """
        client, rm, rpm, db = reactome_admin_client

        pids = [
            _seed_reactome_proposal(rpm, ke_id=f"KE 300{i}", reactome_id=f"R-HSA-300{i}")
            for i in range(1, 6)
        ]

        call_count = [0]
        original = rm._create_approved_on_conn

        def failing_helper(conn, proposal_id, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise sqlite3.IntegrityError("forced failure on call 3")
            return original(conn, proposal_id, *args, **kwargs)

        rm._create_approved_on_conn = failing_helper
        try:
            conn = rm.db.get_connection()
            approved_uuids = []
            try:
                for pid in pids:
                    uuid = rm._create_approved_on_conn(conn, pid, "github:testadmin")
                    approved_uuids.append(uuid)
                conn.commit()
            except Exception:
                conn.rollback()
            finally:
                conn.close()
        finally:
            rm._create_approved_on_conn = original

        assert call_count[0] == 3
        assert len(approved_uuids) == 2

        conn = db.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ke_reactome_mappings "
                "WHERE ke_id IN (?, ?, ?, ?, ?)",
                ("KE 3001", "KE 3002", "KE 3003", "KE 3004", "KE 3005"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

        for pid in pids:
            p = rpm.get_proposal_by_id(pid)
            assert p["status"] == "pending"

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_bulk_approve_returns_uuids(self, reactome_admin_client):
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_preflight_not_found(self, reactome_admin_client):
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_preflight_not_pending(self, reactome_admin_client):
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_audit_log_emitted(self, reactome_admin_client, caplog):
        pass


# ---------------------------------------------------------------------------
# TestBulkApproveShared
# ---------------------------------------------------------------------------


class TestBulkApproveShared:
    """Shared tests that apply to all three bulk-approve resources."""

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_static_js_served(self, reactome_admin_client):
        """GET /static/js/admin_proposals.js returns 200 (smoke)."""
        pass

    @pytest.mark.skip(reason="route lands in 38-02")
    def test_templates_load_shared_js(self):
        """Three admin templates contain admin_proposals.js script tag."""
        pass
