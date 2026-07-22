"""Issue #213 — KE-GO HTTP end-to-end assessment round-trip.

HTTP path: POST /submit_go_mapping (with connection_score / specificity_score /
evidence_score) -> GET /admin/go-proposals/<id>. Asserts the three GO dimension
answers survive the form -> DB -> review-panel handoff.

GO uses a DIFFERENT assessment schema from WP and Reactome: three High/Medium/Low
questions stored as 3/2/1 integers in ke_go_proposals.proposed_connection_score /
_specificity_score / _evidence_score, versus the four categorical proposed_*
columns on `proposals`. The two roundtrip tests that existed (WP, Reactome) both
covered the four-column schema, so nothing checked that GO's three answers reach
the reviewer — and they did not: the shared review panel tested only the WP
columns and reported every GO proposal as "No assessment submitted (legacy
proposal)" (issue #213). The storage path was correct throughout; only the read
side was broken. These tests pin the read side.

Fixture pattern mirrors tests/test_assessment_roundtrip_wp.py::wp_admin_client.
"""
import os
import tempfile

import pytest


@pytest.fixture
def go_admin_client():
    """Test client with both api + admin blueprints wired to a shared temp-file
    GO DB, authenticated as a github:testadmin user (in ADMIN_USERS)."""
    # Set ADMIN_USERS BEFORE importing app so admin_required honors the override.
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    import src.blueprints.api as api_mod
    from src.core.models import Database, GoMappingModel, GoProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    gmm = GoMappingModel(db)
    gpm = GoProposalModel(db)

    orig_api_gpm = api_mod.go_proposal_model
    orig_api_gmm = api_mod.go_mapping_model
    orig_admin_gpm = admin_mod.go_proposal_model
    orig_admin_gmm = admin_mod.go_mapping_model

    # Both blueprints hold their own module globals — wiring only api would
    # leave the admin detail route reading the production DB.
    api_mod.go_proposal_model = gpm
    api_mod.go_mapping_model = gmm
    admin_mod.go_proposal_model = gpm
    admin_mod.go_mapping_model = gmm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            with test_client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield test_client, gpm, gmm, db

    api_mod.go_proposal_model = orig_api_gpm
    api_mod.go_mapping_model = orig_api_gmm
    admin_mod.go_proposal_model = orig_admin_gpm
    admin_mod.go_mapping_model = orig_admin_gmm

    os.close(fd)
    os.unlink(db_path)


def _submit(client, **overrides):
    """POST a new-pair GO mapping, returning the created proposal id."""
    data = {
        "ke_id": "KE 149",
        "ke_title": "Test KE 149",
        "go_id": "GO:0006954",
        "go_name": "inflammatory response",
        "connection_type": "describes",
        "confidence_level": "high",
        "go_namespace": "biological_process",
        "connection_score": "3",
        "specificity_score": "3",
        "evidence_score": "3",
        "suggestion_score": "3.0",
    }
    data.update(overrides)
    resp = client.post("/submit_go_mapping", data=data)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["proposal_id"]


def test_assessment_roundtrip_go(go_admin_client):
    """Happy path: submit with all three dimension answers, assert they are
    stored AND exposed by the detail endpoint the review panel reads."""
    client, gpm, gmm, db = go_admin_client

    proposal_id = _submit(client)

    # 1. Stored on the proposal row.
    row = gpm.get_go_proposal_by_id(proposal_id)
    assert row["proposed_connection_score"] == 3
    assert row["proposed_specificity_score"] == 3
    assert row["proposed_evidence_score"] == 3

    # 2. Reaching the reviewer. This is the assertion that fails for #213 —
    #    the panel renders from this payload, so a missing key here is what
    #    produced "No assessment submitted (legacy proposal)".
    resp = client.get(f"/admin/go-proposals/{proposal_id}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["proposed_connection_score"] == 3
    assert payload["proposed_specificity_score"] == 3
    assert payload["proposed_evidence_score"] == 3


def test_assessment_mixed_scores_go(go_admin_client):
    """Distinct per-dimension values survive — guards against a fix that
    renders one score three times, or collapses them into the derived
    confidence level."""
    client, gpm, gmm, db = go_admin_client

    proposal_id = _submit(
        client,
        connection_score="3",
        specificity_score="2",
        evidence_score="1",
        confidence_level="medium",
        suggestion_score="2.0",
    )

    payload = client.get(f"/admin/go-proposals/{proposal_id}").get_json()
    assert payload["proposed_connection_score"] == 3
    assert payload["proposed_specificity_score"] == 2
    assert payload["proposed_evidence_score"] == 1


def test_assessment_legacy_go(go_admin_client):
    """Backward-compat: a submission with no dimension answers leaves the three
    columns NULL, so the panel's genuine "legacy proposal" branch still applies
    to the pre-dimension GO proposals already in the database."""
    client, gpm, gmm, db = go_admin_client

    proposal_id = _submit(
        client,
        connection_score="",
        specificity_score="",
        evidence_score="",
    )

    payload = client.get(f"/admin/go-proposals/{proposal_id}").get_json()
    assert payload["proposed_connection_score"] is None
    assert payload["proposed_specificity_score"] is None
    assert payload["proposed_evidence_score"] is None
