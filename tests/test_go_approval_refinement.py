"""
Tests for refining a GO assessment at approval time.

A reviewer working the admin queue often disagrees with one dimension, the
connection type, or the resulting tier. Without a way to correct it in place the
only options were to approve something they considered wrong, or reject the
proposal and ask for a resubmit. The approve route now accepts a revised
connection_type and an explicit confidence_level alongside the three dimension
scores it already took, and the review panel posts them.

Validation matters here because this route writes the live ke_go_mappings row.
"""
import os
import tempfile

import pytest


@pytest.fixture
def go_admin_client():
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    from src.core.models import Database, GoMappingModel, GoProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    gm = GoMappingModel(db)
    gpm = GoProposalModel(db)

    orig_gm, orig_gpm = admin_mod.go_mapping_model, admin_mod.go_proposal_model
    admin_mod.go_mapping_model, admin_mod.go_proposal_model = gm, gpm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as c:
        with flask_app.app_context():
            with c.session_transaction() as sess:
                sess["user"] = {"username": "github:testadmin", "email": "a@b.c"}
            yield c, gm, gpm, db

    admin_mod.go_mapping_model, admin_mod.go_proposal_model = orig_gm, orig_gpm
    os.close(fd)
    os.unlink(db_path)


def _seed(gpm, ke_id="KE 3001", connection_type="involves", conf="medium",
          scores=(2, 2, 2)):
    pid = gpm.create_new_pair_go_proposal(
        ke_id=ke_id,
        ke_title=f"Title for {ke_id}",
        go_id="GO:0051403",
        go_name="stress-activated MAPK cascade",
        connection_type=connection_type,
        confidence_level=conf,
        provider_username="github:curator",
        suggestion_score=0.7,
        connection_score=scores[0],
        specificity_score=scores[1],
        evidence_score=scores[2],
        go_namespace="biological_process",
    )
    assert pid is not None
    return pid


def _mapping(db, ke_id):
    conn = db.get_connection()
    try:
        row = conn.execute(
            """SELECT connection_type, confidence_level, connection_score,
                      specificity_score, evidence_score, assessment_version
               FROM ke_go_mappings WHERE ke_id = ?""",
            (ke_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Connection type
# ---------------------------------------------------------------------------

def test_reviewer_can_change_connection_type(go_admin_client):
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, connection_type="involves")

    r = c.post(f"/admin/go-proposals/{pid}/approve",
               data={"admin_notes": "reads as a direct description", "connection_type": "describes"})
    assert r.status_code == 200
    assert _mapping(db, "KE 3001")["connection_type"] == "describes"


def test_connection_type_defaults_to_the_submitted_one(go_admin_client):
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3002", connection_type="related")

    assert c.post(f"/admin/go-proposals/{pid}/approve", data={"admin_notes": ""}).status_code == 200
    assert _mapping(db, "KE 3002")["connection_type"] == "related"


def test_invalid_connection_type_is_rejected(go_admin_client):
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3003")

    r = c.post(f"/admin/go-proposals/{pid}/approve",
               data={"admin_notes": "", "connection_type": "causative"})   # a KE-WP value
    assert r.status_code == 400
    assert "connection_type" in r.get_json()["error"]
    assert _mapping(db, "KE 3003") is None, "nothing may be written on a rejected input"


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def test_reviewer_can_override_confidence(go_admin_client):
    """The tier the weights produce is not always the one a curator would defend."""
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3004", conf="medium", scores=(2, 2, 2))

    r = c.post(f"/admin/go-proposals/{pid}/approve",
               data={"admin_notes": "should be high conf", "confidence_level": "high"})
    assert r.status_code == 200
    assert _mapping(db, "KE 3004")["confidence_level"] == "high"


def test_explicit_confidence_beats_the_recomputed_one(go_admin_client):
    """3/3/3 computes to high; an explicit 'low' must still win."""
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3005", conf="medium")

    r = c.post(f"/admin/go-proposals/{pid}/approve", data={
        "admin_notes": "", "connection_score": "3", "specificity_score": "3",
        "evidence_score": "3", "confidence_level": "low",
    })
    assert r.status_code == 200
    m = _mapping(db, "KE 3005")
    assert m["confidence_level"] == "low"
    assert (m["connection_score"], m["specificity_score"], m["evidence_score"]) == (3, 3, 3)


def test_rescore_without_explicit_confidence_recomputes(go_admin_client):
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3006", conf="low", scores=(1, 1, 1))

    r = c.post(f"/admin/go-proposals/{pid}/approve", data={
        "admin_notes": "", "connection_score": "3", "specificity_score": "3", "evidence_score": "3",
    })
    assert r.status_code == 200
    m = _mapping(db, "KE 3006")
    assert m["confidence_level"] == "high"      # 3/3/3 = 3.0, above the 2.5 threshold
    assert m["assessment_version"] == "v2"


def test_invalid_confidence_is_rejected(go_admin_client):
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3007")

    r = c.post(f"/admin/go-proposals/{pid}/approve",
               data={"admin_notes": "", "confidence_level": "very high"})
    assert r.status_code == 400
    assert "confidence_level" in r.get_json()["error"]
    assert _mapping(db, "KE 3007") is None


# ---------------------------------------------------------------------------
# An untouched review must still be faithful
# ---------------------------------------------------------------------------

def test_untouched_approval_records_the_submitters_values(go_admin_client):
    """The panel posts the assessment even when unedited; the result must match a
    bare approval exactly."""
    c, gm, gpm, db = go_admin_client
    bare = _seed(gpm, ke_id="KE 3008", connection_type="describes", conf="high", scores=(3, 3, 2))
    echoed = _seed(gpm, ke_id="KE 3009", connection_type="describes", conf="high", scores=(3, 3, 2))

    assert c.post(f"/admin/go-proposals/{bare}/approve", data={"admin_notes": ""}).status_code == 200
    assert c.post(f"/admin/go-proposals/{echoed}/approve", data={
        "admin_notes": "", "connection_type": "describes",
        "connection_score": "3", "specificity_score": "3", "evidence_score": "2",
    }).status_code == 200

    assert _mapping(db, "KE 3008") == _mapping(db, "KE 3009")


def test_case_and_whitespace_are_normalised(go_admin_client):
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3010")

    r = c.post(f"/admin/go-proposals/{pid}/approve",
               data={"admin_notes": "", "connection_type": " Describes ", "confidence_level": " HIGH "})
    assert r.status_code == 200
    m = _mapping(db, "KE 3010")
    assert m["connection_type"] == "describes"
    assert m["confidence_level"] == "high"


def test_edits_are_written_to_the_audit_log(go_admin_client, caplog):
    """An edited approval must be distinguishable from a rubber-stamped one."""
    import logging
    c, gm, gpm, db = go_admin_client
    pid = _seed(gpm, ke_id="KE 3011", connection_type="involves", scores=(2, 2, 2))

    with caplog.at_level(logging.INFO):
        c.post(f"/admin/go-proposals/{pid}/approve", data={
            "admin_notes": "", "connection_type": "describes",
            "connection_score": "3", "specificity_score": "3", "evidence_score": "3",
        })
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "edited on approval" in logged
    assert "connection_type involves -> describes" in logged
    assert "2/2/2 -> 3/3/3" in logged
