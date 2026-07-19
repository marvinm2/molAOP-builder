"""
Tests for Phase 38 bulk-approve backend (Plans 38-01 and 38-02).

Covers:
- Per-resource fixtures: wp_admin_client, go_admin_client, reactome_admin_client
- Seed helpers: _seed_wp_proposal, _seed_go_proposal, _seed_reactome_proposal
- Per-resource test classes: TestWPBulkApprove, TestGOBulkApprove, TestReactomeBulkApprove
- TestBulkApproveShared: static/smoke tests

Per-resource test classes are NOT parametrized (D-16 sibling-parity rule).
"""
import logging
import os
import sqlite3
import tempfile
import uuid

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
    """Tests for POST /admin/proposals/bulk-approve (WP resource)."""

    def test_fault_injection(self, wp_admin_client):
        """Seed 5 pending WP proposals, force 3rd _approve_on_conn to raise IntegrityError.

        All 5 proposals must remain pending after the POST (whole-batch rollback).
        0 mapping rows must exist for the seeded ke_id/wp_id pairs.
        response["approved"] must equal [].
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
            response = client.post(
                "/admin/proposals/bulk-approve",
                json={"ids": pids, "admin_notes": ""},
            )
        finally:
            mm._approve_on_conn = original

        assert response.status_code == 500
        data = response.get_json()
        assert data["approved"] == []

        # 0 mappings in DB — rollback reverted all writes
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

    def test_preflight_not_found(self, wp_admin_client):
        """Not-found ID appears in failed[] with reason 'not found'; valid ID is approved."""
        client, mm, pm, db = wp_admin_client

        pid = _seed_wp_proposal(pm, ke_id="KE 1010", wp_id="WP1010")
        nonexistent_id = 99999

        response = client.post(
            "/admin/proposals/bulk-approve",
            json={"ids": [nonexistent_id, pid], "admin_notes": ""},
        )

        assert response.status_code == 200
        data = response.get_json()

        # The nonexistent id is in failed[]
        failed_ids = [f["id"] for f in data["failed"]]
        assert nonexistent_id in failed_ids
        not_found_entry = next(f for f in data["failed"] if f["id"] == nonexistent_id)
        assert not_found_entry["reason"] == "not found"

        # The valid id is approved
        assert len(data["approved"]) == 1

    def test_preflight_not_pending(self, wp_admin_client):
        """Already-approved ID appears in failed[] with reason containing the status."""
        client, mm, pm, db = wp_admin_client

        # Approve the first proposal via the single-approve route to set its status
        pid1 = _seed_wp_proposal(pm, ke_id="KE 1020", wp_id="WP1020")
        pid2 = _seed_wp_proposal(pm, ke_id="KE 1021", wp_id="WP1021")

        # Approve pid1 first via bulk (single item)
        r1 = client.post(
            "/admin/proposals/bulk-approve",
            json={"ids": [pid1], "admin_notes": ""},
        )
        assert r1.status_code == 200
        assert len(r1.get_json()["approved"]) == 1

        # Now try to bulk-approve both — pid1 is no longer pending
        response = client.post(
            "/admin/proposals/bulk-approve",
            json={"ids": [pid1, pid2], "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        failed_ids = [f["id"] for f in data["failed"]]
        assert pid1 in failed_ids
        not_pending_entry = next(f for f in data["failed"] if f["id"] == pid1)
        assert "already" in not_pending_entry["reason"]

        # pid2 was still pending and should now be approved
        assert len(data["approved"]) == 1

    def test_returns_mapping_uuids(self, wp_admin_client):
        """approved[] entries are UUID-shaped strings, not integer proposal IDs."""
        client, mm, pm, db = wp_admin_client

        pids = [
            _seed_wp_proposal(pm, ke_id=f"KE 103{i}", wp_id=f"WP103{i}")
            for i in range(1, 4)
        ]

        response = client.post(
            "/admin/proposals/bulk-approve",
            json={"ids": pids, "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        assert len(data["approved"]) == 3
        for entry in data["approved"]:
            # Must be a string, not an integer
            assert isinstance(entry, str)
            # Must parse as a valid UUID (UUID-shaped: 8-4-4-4-12 hex)
            parsed = uuid.UUID(entry)
            assert str(parsed) == entry.lower()

    def test_audit_log_emitted(self, wp_admin_client, caplog):
        """AUDIT bulk-approve wp log line is emitted and contains the approved UUID."""
        client, mm, pm, db = wp_admin_client

        pid = _seed_wp_proposal(pm, ke_id="KE 1040", wp_id="WP1040")

        with caplog.at_level(logging.INFO, logger="src.blueprints.admin"):
            response = client.post(
                "/admin/proposals/bulk-approve",
                json={"ids": [pid], "admin_notes": ""},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert len(data["approved"]) == 1

        assert "AUDIT bulk-approve wp" in caplog.text
        approved_uuid = data["approved"][0]
        assert approved_uuid in caplog.text


# ---------------------------------------------------------------------------
# TestGOBulkApprove
# ---------------------------------------------------------------------------


class TestGOBulkApprove:
    """Tests for POST /admin/go-proposals/bulk-approve (GO resource)."""

    def test_fault_injection(self, go_admin_client):
        """Seed 5 pending GO proposals, force 3rd _approve_on_conn to raise IntegrityError.

        All 5 proposals must remain pending after the POST (whole-batch rollback).
        0 mapping rows must exist for the seeded ke_id/go_id pairs.
        response["approved"] must equal [].
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
            response = client.post(
                "/admin/go-proposals/bulk-approve",
                json={"ids": pids, "admin_notes": ""},
            )
        finally:
            gm._approve_on_conn = original

        assert response.status_code == 500
        data = response.get_json()
        assert data["approved"] == []

        # 0 mappings in DB — rollback reverted all writes
        conn = db.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ke_go_mappings WHERE ke_id IN (?, ?, ?, ?, ?)",
                ("KE 2001", "KE 2002", "KE 2003", "KE 2004", "KE 2005"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

        # All proposals remain pending
        for pid in pids:
            p = gpm.get_go_proposal_by_id(pid)
            assert p["status"] == "pending"

    def test_preflight_not_found(self, go_admin_client):
        """Not-found ID appears in failed[] with reason 'not found'; valid ID is approved."""
        client, gm, gpm, db = go_admin_client

        pid = _seed_go_proposal(gpm, ke_id="KE 2010", go_id="GO:0010010")
        nonexistent_id = 99998

        response = client.post(
            "/admin/go-proposals/bulk-approve",
            json={"ids": [nonexistent_id, pid], "admin_notes": ""},
        )

        assert response.status_code == 200
        data = response.get_json()

        failed_ids = [f["id"] for f in data["failed"]]
        assert nonexistent_id in failed_ids
        not_found_entry = next(f for f in data["failed"] if f["id"] == nonexistent_id)
        assert not_found_entry["reason"] == "not found"

        assert len(data["approved"]) == 1

    def test_preflight_not_pending(self, go_admin_client):
        """Already-approved ID appears in failed[] with reason containing the status."""
        client, gm, gpm, db = go_admin_client

        pid1 = _seed_go_proposal(gpm, ke_id="KE 2020", go_id="GO:0020020")
        pid2 = _seed_go_proposal(gpm, ke_id="KE 2021", go_id="GO:0020021")

        # Approve pid1 first
        r1 = client.post(
            "/admin/go-proposals/bulk-approve",
            json={"ids": [pid1], "admin_notes": ""},
        )
        assert r1.status_code == 200
        assert len(r1.get_json()["approved"]) == 1

        # Now try both — pid1 is no longer pending
        response = client.post(
            "/admin/go-proposals/bulk-approve",
            json={"ids": [pid1, pid2], "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        failed_ids = [f["id"] for f in data["failed"]]
        assert pid1 in failed_ids
        not_pending_entry = next(f for f in data["failed"] if f["id"] == pid1)
        assert "already" in not_pending_entry["reason"]

        assert len(data["approved"]) == 1

    def test_returns_mapping_uuids(self, go_admin_client):
        """approved[] entries are UUID-shaped strings, not integer proposal IDs."""
        client, gm, gpm, db = go_admin_client

        pids = [
            _seed_go_proposal(gpm, ke_id=f"KE 203{i}", go_id=f"GO:0030{i:03d}")
            for i in range(1, 4)
        ]

        response = client.post(
            "/admin/go-proposals/bulk-approve",
            json={"ids": pids, "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        assert len(data["approved"]) == 3
        for entry in data["approved"]:
            assert isinstance(entry, str)
            parsed = uuid.UUID(entry)
            assert str(parsed) == entry.lower()

    def test_audit_log_emitted(self, go_admin_client, caplog):
        """AUDIT bulk-approve go log line is emitted and contains the approved UUID."""
        client, gm, gpm, db = go_admin_client

        pid = _seed_go_proposal(gpm, ke_id="KE 2040", go_id="GO:0040040")

        with caplog.at_level(logging.INFO, logger="src.blueprints.admin"):
            response = client.post(
                "/admin/go-proposals/bulk-approve",
                json={"ids": [pid], "admin_notes": ""},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert len(data["approved"]) == 1

        assert "AUDIT bulk-approve go" in caplog.text
        approved_uuid = data["approved"][0]
        assert approved_uuid in caplog.text


# ---------------------------------------------------------------------------
# TestReactomeBulkApprove
# ---------------------------------------------------------------------------


class TestReactomeBulkApprove:
    """Tests for POST /admin/reactome-proposals/bulk-approve (Reactome resource)."""

    def test_fault_injection(self, reactome_admin_client):
        """Seed 5 pending Reactome proposals, force 3rd _create_approved_on_conn to raise.

        All 5 proposals must remain pending after the POST (whole-batch rollback).
        0 mapping rows must exist for the seeded ke_id/reactome_id pairs.
        response["approved"] must equal [].
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
            response = client.post(
                "/admin/reactome-proposals/bulk-approve",
                json={"ids": pids, "admin_notes": ""},
            )
        finally:
            rm._create_approved_on_conn = original

        assert response.status_code == 500
        data = response.get_json()
        assert data["approved"] == []

        # 0 mappings in DB — rollback reverted all writes
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

        # All proposals remain pending
        for pid in pids:
            p = rpm.get_proposal_by_id(pid)
            assert p["status"] == "pending"

    def test_preflight_not_found(self, reactome_admin_client):
        """Not-found ID appears in failed[] with reason 'not found'; valid ID is approved."""
        client, rm, rpm, db = reactome_admin_client

        pid = _seed_reactome_proposal(rpm, ke_id="KE 3010", reactome_id="R-HSA-30100")
        nonexistent_id = 99997

        response = client.post(
            "/admin/reactome-proposals/bulk-approve",
            json={"ids": [nonexistent_id, pid], "admin_notes": ""},
        )

        assert response.status_code == 200
        data = response.get_json()

        failed_ids = [f["id"] for f in data["failed"]]
        assert nonexistent_id in failed_ids
        not_found_entry = next(f for f in data["failed"] if f["id"] == nonexistent_id)
        assert not_found_entry["reason"] == "not found"

        assert len(data["approved"]) == 1

    def test_preflight_not_pending(self, reactome_admin_client):
        """Already-approved ID appears in failed[] with reason containing the status."""
        client, rm, rpm, db = reactome_admin_client

        pid1 = _seed_reactome_proposal(rpm, ke_id="KE 3020", reactome_id="R-HSA-30200")
        pid2 = _seed_reactome_proposal(rpm, ke_id="KE 3021", reactome_id="R-HSA-30210")

        # Approve pid1 first
        r1 = client.post(
            "/admin/reactome-proposals/bulk-approve",
            json={"ids": [pid1], "admin_notes": ""},
        )
        assert r1.status_code == 200
        assert len(r1.get_json()["approved"]) == 1

        # Now try both — pid1 is no longer pending
        response = client.post(
            "/admin/reactome-proposals/bulk-approve",
            json={"ids": [pid1, pid2], "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        failed_ids = [f["id"] for f in data["failed"]]
        assert pid1 in failed_ids
        not_pending_entry = next(f for f in data["failed"] if f["id"] == pid1)
        assert "already" in not_pending_entry["reason"]

        assert len(data["approved"]) == 1

    def test_returns_mapping_uuids(self, reactome_admin_client):
        """approved[] entries are UUID-shaped strings, not integer proposal IDs."""
        client, rm, rpm, db = reactome_admin_client

        pids = [
            _seed_reactome_proposal(
                rpm, ke_id=f"KE 303{i}", reactome_id=f"R-HSA-3030{i}"
            )
            for i in range(1, 4)
        ]

        response = client.post(
            "/admin/reactome-proposals/bulk-approve",
            json={"ids": pids, "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        assert len(data["approved"]) == 3
        for entry in data["approved"]:
            assert isinstance(entry, str)
            parsed = uuid.UUID(entry)
            assert str(parsed) == entry.lower()

    def test_audit_log_emitted(self, reactome_admin_client, caplog):
        """AUDIT bulk-approve reactome log line is emitted and contains the approved UUID."""
        client, rm, rpm, db = reactome_admin_client

        pid = _seed_reactome_proposal(rpm, ke_id="KE 3040", reactome_id="R-HSA-30400")

        with caplog.at_level(logging.INFO, logger="src.blueprints.admin"):
            response = client.post(
                "/admin/reactome-proposals/bulk-approve",
                json={"ids": [pid], "admin_notes": ""},
            )

        assert response.status_code == 200
        data = response.get_json()
        assert len(data["approved"]) == 1

        assert "AUDIT bulk-approve reactome" in caplog.text
        approved_uuid = data["approved"][0]
        assert approved_uuid in caplog.text


# ---------------------------------------------------------------------------
# TestBulkApproveShared
# ---------------------------------------------------------------------------


class TestBulkApproveShared:
    """Shared tests that apply to all three bulk-approve resources."""

    def test_invalid_id_type_rejected(self, reactome_admin_client):
        """Non-integer IDs (strings) land in failed[] with reason 'invalid id'."""
        client, rm, rpm, db = reactome_admin_client

        pid = _seed_reactome_proposal(rpm, ke_id="KE 3050", reactome_id="R-HSA-30500")

        response = client.post(
            "/admin/reactome-proposals/bulk-approve",
            json={"ids": ["not-an-int", pid], "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()

        failed_reasons = {f["id"]: f["reason"] for f in data["failed"]}
        assert "not-an-int" in failed_reasons
        assert failed_reasons["not-an-int"] == "invalid id"

        # valid integer pid still approved
        assert len(data["approved"]) == 1

    def test_empty_ids_returns_empty_approved(self, reactome_admin_client):
        """Empty ids list returns {"approved": [], "failed": []} with 200."""
        client, rm, rpm, db = reactome_admin_client

        response = client.post(
            "/admin/reactome-proposals/bulk-approve",
            json={"ids": [], "admin_notes": ""},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["approved"] == []
        assert data["failed"] == []
