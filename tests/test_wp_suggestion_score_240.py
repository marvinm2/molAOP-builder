"""#240 — the WikiPathways suggestion_score must survive from the suggestion card
to the RDF export.

The field was null for all 127 approved WP mappings because of two independent
gaps, either of which is sufficient to lose it:

1. The client never sent it. `handleFormSubmission()` builds the /submit payload
   by hand rather than serialising #mapping-form, so the hidden #suggestion_score
   input it populates was silently dropped. GO and Reactome include the key.
2. `MappingModel.get_all_mappings()` did not select the column. That is the query
   behind the RDF/GMT bulk export, so even a correctly stored score could not
   reach a triple. The paginated query used by /api/v1 always selected it.

Tests below cover both: a grep-style assertion on the client payload (the JS is
not executed here, matching tests/test_mapper_js_fixes.py) and an HTTP round-trip
through submit -> approve -> bulk-export SELECT.

Fixture mirrors tests/test_assessment_roundtrip_wp.py::wp_admin_client.
"""
import os
import re
import tempfile

import pytest

HERE = os.path.dirname(__file__)
MAIN_JS = os.path.join(HERE, "..", "static", "js", "main.js")


def _read_main_js():
    with open(MAIN_JS, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def wp_admin_client():
    """Test client with the api + admin blueprints wired to a shared temp-file WP
    DB, authenticated as a github:testadmin user (in ADMIN_USERS)."""
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    import src.blueprints.api as api_mod
    from src.core.models import CacheModel, Database, MappingModel, ProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    mm = MappingModel(db)
    pm = ProposalModel(db)
    cm = CacheModel(db)

    orig_api_pm = api_mod.proposal_model
    orig_api_mm = api_mod.mapping_model
    orig_api_cm = api_mod.cache_model
    orig_admin_pm = admin_mod.proposal_model
    orig_admin_mm = admin_mod.mapping_model

    api_mod.proposal_model = pm
    api_mod.mapping_model = mm
    api_mod.cache_model = cm
    admin_mod.proposal_model = pm
    admin_mod.mapping_model = mm

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

    api_mod.proposal_model = orig_api_pm
    api_mod.mapping_model = orig_api_mm
    api_mod.cache_model = orig_api_cm
    admin_mod.proposal_model = orig_admin_pm
    admin_mod.mapping_model = orig_admin_mm

    os.close(fd)
    os.unlink(db_path)


def test_wp_submit_payload_includes_suggestion_score():
    """Gap 1: the hand-built WP payload must read the hidden input.

    Scoped to handleFormSubmission's object literal — a bare occurrence of the
    string elsewhere in the file (the GO and Reactome payloads both contain one)
    would otherwise satisfy a whole-file search.
    """
    body = _read_main_js()
    start = body.index("const formData = {", body.index("handleFormSubmission(event) {"))
    literal = body[start:body.index("};", start)]
    assert "suggestion_score:" in literal, (
        "handleFormSubmission() must put suggestion_score in the /submit payload; "
        "it builds formData by hand, so the hidden #suggestion_score input is "
        "otherwise dropped (#240)"
    )
    assert re.search(r"suggestion_score:\s*\$\(.#suggestion_score.\)\.val\(\)", literal), (
        "suggestion_score must be read from the hidden #suggestion_score input, "
        "which selectSuggestedPathway() sets and the search/browse handlers clear"
    )


def test_wp_bulk_export_select_includes_suggestion_score():
    """Gap 2: the bulk-export SELECT must carry the column.

    Asserted against the query text because the failure is a silent absence —
    rdf_exporter guards on `if row.get("suggestion_score") is not None`, so a
    missing column produces valid RDF with no score rather than an error.
    """
    import inspect

    from src.core.models import MappingModel

    source = inspect.getsource(MappingModel.get_all_mappings)
    assert "suggestion_score" in source, (
        "MappingModel.get_all_mappings() must select suggestion_score; the WP RDF "
        "export reads its rows from here (#240)"
    )


def test_wp_suggestion_score_survives_submit_approve_export(wp_admin_client):
    """End-to-end: a score posted with the proposal reaches the export rows."""
    client, mm, pm, db = wp_admin_client

    resp = client.post(
        "/submit",
        data={
            "ke_id": "KE 400",
            "ke_title": "Scored KE 400",
            "wp_id": "WP200",
            "wp_title": "Scored Pathway 200",
            "connection_type": "causative",
            "confidence_level": "high",
            "suggestion_score": "0.8123",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    proposal_id = resp.get_json()["proposal_id"]

    proposal = pm.get_proposal_by_id(proposal_id)
    assert proposal["suggestion_score"] == pytest.approx(0.8123)

    resp = client.post(
        f"/admin/proposals/{proposal_id}/approve",
        data={"admin_notes": "scored"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    row = next(r for r in mm.get_all_mappings() if r["ke_id"] == "KE 400")
    assert row["suggestion_score"] == pytest.approx(0.8123)


def test_wp_manual_pick_records_no_suggestion_score(wp_admin_client):
    """A pathway chosen from search or the browse dropdown must stay null.

    The distinction between "accepted the ranker's suggestion" and "found it
    myself" is the entire point of the field, so an absent score has to remain
    absent rather than defaulting to 0.
    """
    client, mm, pm, db = wp_admin_client

    resp = client.post(
        "/submit",
        data={
            "ke_id": "KE 401",
            "ke_title": "Manual KE 401",
            "wp_id": "WP201",
            "wp_title": "Manual Pathway 201",
            "connection_type": "causative",
            "confidence_level": "medium",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    proposal_id = resp.get_json()["proposal_id"]

    resp = client.post(
        f"/admin/proposals/{proposal_id}/approve",
        data={"admin_notes": "manual"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    row = next(r for r in mm.get_all_mappings() if r["ke_id"] == "KE 401")
    assert row["suggestion_score"] is None
