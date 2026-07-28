"""
Tests that approving a GO proposal keeps the submitter's dimension scores.

Both approval paths used to drop them. The admin UI's approve button posts only
admin_notes + csrf_token, so single-approve fell into its legacy branch and wrote
connection/specificity/evidence as NULL with assessment_version="v1"; bulk-approve
hardcoded the same. Every approved mapping therefore looked like an unscored legacy
row, even though the curator had recorded all three at submission time and they were
sitting in ke_go_proposals.proposed_*_score.

Also covers the bulk pre-flight: _approve_on_conn unconditionally INSERTs a mapping,
so revision and deletion proposals must never reach it.
"""
import os
import tempfile

import pytest


@pytest.fixture
def go_admin_client():
    """Test client with fresh GO models on a temp DB and an admin session."""
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


def _seed_scored_proposal(gpm, ke_id="KE 2001", go_id="GO:0051403",
                          connection=3, specificity=3, evidence=2):
    """Seed a new-pair GO proposal carrying the curator's three dimension scores."""
    proposal_id = gpm.create_new_pair_go_proposal(
        ke_id=ke_id,
        ke_title=f"Title for {ke_id}",
        go_id=go_id,
        go_name="stress-activated MAPK cascade",
        connection_type="describes",
        confidence_level="high",
        provider_username="github:curator",
        suggestion_score=0.87,
        connection_score=connection,
        specificity_score=specificity,
        evidence_score=evidence,
        go_namespace="biological_process",
    )
    assert proposal_id is not None
    return proposal_id


def _mapping_row(db, ke_id, go_id):
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT connection_score, specificity_score, evidence_score,
                      assessment_version, confidence_level
               FROM ke_go_mappings WHERE ke_id = ? AND go_id = ?""",
            (ke_id, go_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Single approve
# ---------------------------------------------------------------------------

def test_single_approve_carries_stored_scores(go_admin_client):
    """The real UI posts no scores; the stored ones must still land."""
    client, gm, gpm, db = go_admin_client
    pid = _seed_scored_proposal(gpm)

    resp = client.post(f"/admin/go-proposals/{pid}/approve", data={"admin_notes": ""})
    assert resp.status_code == 200

    row = _mapping_row(db, "KE 2001", "GO:0051403")
    assert row is not None
    assert (row["connection_score"], row["specificity_score"], row["evidence_score"]) == (3, 3, 2)
    assert row["assessment_version"] == "v2"
    assert row["confidence_level"] == "high"


def test_single_approve_admin_rescore_still_wins(go_admin_client):
    """An admin who does supply a full re-score overrides the stored values."""
    client, gm, gpm, db = go_admin_client
    pid = _seed_scored_proposal(gpm, ke_id="KE 2002")

    resp = client.post(
        f"/admin/go-proposals/{pid}/approve",
        data={
            "admin_notes": "downgraded on review",
            "connection_score": "1",
            "specificity_score": "1",
            "evidence_score": "1",
        },
    )
    assert resp.status_code == 200

    row = _mapping_row(db, "KE 2002", "GO:0051403")
    assert (row["connection_score"], row["specificity_score"], row["evidence_score"]) == (1, 1, 1)
    assert row["assessment_version"] == "v2"


def test_single_approve_unscored_proposal_stays_v1(go_admin_client):
    """A proposal predating the dimension form has nothing to carry."""
    client, gm, gpm, db = go_admin_client
    pid = gpm.create_new_pair_go_proposal(
        ke_id="KE 2003",
        ke_title="Title for KE 2003",
        go_id="GO:0051403",
        go_name="stress-activated MAPK cascade",
        connection_type="related",
        confidence_level="medium",
        provider_username="github:curator",
    )

    resp = client.post(f"/admin/go-proposals/{pid}/approve", data={"admin_notes": ""})
    assert resp.status_code == 200

    row = _mapping_row(db, "KE 2003", "GO:0051403")
    assert (row["connection_score"], row["specificity_score"], row["evidence_score"]) == (None, None, None)
    assert row["assessment_version"] == "v1"
    assert row["confidence_level"] == "medium"


# ---------------------------------------------------------------------------
# Bulk approve
# ---------------------------------------------------------------------------

def test_bulk_approve_carries_stored_scores(go_admin_client):
    client, gm, gpm, db = go_admin_client
    pids = [
        _seed_scored_proposal(gpm, ke_id=f"KE 21{i:02d}", connection=3, specificity=2, evidence=2)
        for i in range(3)
    ]

    resp = client.post("/admin/go-proposals/bulk-approve", json={"ids": pids, "admin_notes": ""})
    assert resp.status_code == 200
    assert len(resp.get_json()["approved"]) == 3

    for i in range(3):
        row = _mapping_row(db, f"KE 21{i:02d}", "GO:0051403")
        assert (row["connection_score"], row["specificity_score"], row["evidence_score"]) == (3, 2, 2)
        assert row["assessment_version"] == "v2"


def test_bulk_and_single_approve_agree(go_admin_client):
    """The two paths must produce the same mapping row for the same proposal."""
    client, gm, gpm, db = go_admin_client
    single = _seed_scored_proposal(gpm, ke_id="KE 2201", connection=2, specificity=3, evidence=1)
    bulk = _seed_scored_proposal(gpm, ke_id="KE 2202", connection=2, specificity=3, evidence=1)

    assert client.post(f"/admin/go-proposals/{single}/approve", data={"admin_notes": ""}).status_code == 200
    assert client.post("/admin/go-proposals/bulk-approve", json={"ids": [bulk], "admin_notes": ""}).status_code == 200

    a = _mapping_row(db, "KE 2201", "GO:0051403")
    b = _mapping_row(db, "KE 2202", "GO:0051403")
    assert a == b


def test_bulk_approve_refuses_non_new_pair_proposals(go_admin_client):
    """_approve_on_conn always INSERTs, so a revision proposal would duplicate the
    mapping instead of updating it."""
    client, gm, gpm, db = go_admin_client

    mapping_id = gm.create_mapping(
        ke_id="KE 2300",
        ke_title="Title for KE 2300",
        go_id="GO:0051403",
        go_name="stress-activated MAPK cascade",
        connection_type="describes",
        confidence_level="high",
        created_by="github:curator",
    )
    assert mapping_id

    revision_id = gpm.create_proposal(
        mapping_id=mapping_id,
        user_name="Test Curator",
        user_email="curator@example.com",
        user_affiliation="Maastricht University",
        provider_username="github:curator",
        proposed_confidence="medium",
        proposed_connection_type="involves",
        ke_id="KE 2300",
        ke_title="Title for KE 2300",
        go_id="GO:0051403",
        go_name="stress-activated MAPK cascade",
    )
    assert revision_id

    resp = client.post("/admin/go-proposals/bulk-approve", json={"ids": [revision_id], "admin_notes": ""})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["approved"] == []
    assert body["failed"] == [{"id": revision_id, "reason": "not a new-pair proposal"}]

    conn = db.get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM ke_go_mappings WHERE ke_id = ? AND go_id = ?",
            ("KE 2300", "GO:0051403"),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1, "the revision must not have inserted a duplicate mapping"
