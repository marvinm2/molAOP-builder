"""#245 part 2 — a KE-Reactome revision carries the four assessment answers.

Reactome shares KE-WP's instrument: `ke_reactome_mappings` already holds the
same four `proposed_*` columns, written at creation and covered by
`tests/test_assessment_roundtrip_reactome.py`. What it lacked was any way to
*change* them, because D-02 locked the tier at proposal creation and the approve
route rejected anything that was not a deletion.

That made a wrong Reactome tier permanent. The only remedy was to delete the
mapping and re-create it, which loses its uuid and its provenance — so the
"locked" tier was not actually protected, just expensive to correct, and the
correction destroyed the audit trail the lock existed to preserve.

**What D-02 still guarantees is unchanged and still pinned elsewhere**: approval
consumes no admin-supplied dimension scores, and `ke_reactome_mappings` grows no
`connection_score` / `specificity_score` / `evidence_score` / `connection_type`
columns — see `tests/test_reactome_admin.py::test_approve_no_dimension_score_columns_used`,
which this work leaves passing untouched.
"""
import json
import os
import tempfile

import pytest


@pytest.fixture
def reactome_revision_client():
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    import src.blueprints.api as api_mod
    from src.core.models import (
        CacheModel, Database, ReactomeMappingModel, ReactomeProposalModel,
    )

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    rm = ReactomeMappingModel(db)
    rpm = ReactomeProposalModel(db)
    cm = CacheModel(db)

    originals = (
        api_mod.reactome_mapping_model, api_mod.reactome_proposal_model,
        api_mod.cache_model, api_mod.ke_metadata_index,
        admin_mod.reactome_mapping_model, admin_mod.reactome_proposal_model,
    )

    api_mod.reactome_mapping_model = rm
    api_mod.reactome_proposal_model = rpm
    api_mod.cache_model = cm
    # KE 177 is Cellular, which qualifies for the +1.0 biological-level bonus.
    # KE 999 is deliberately absent, standing in for the Key Events the
    # snapshot lags behind (#239).
    api_mod.ke_metadata_index = {
        "KE 177": {"KElabel": "KE 177", "biolevel": "Cellular"},
    }
    admin_mod.reactome_mapping_model = rm
    admin_mod.reactome_proposal_model = rpm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    # The submission rate limiter is keyed on DATABASE_PATH; without pointing it
    # at this test's own DB it accumulates across the session and the later
    # tests here 429 instead of asserting what they mean to.
    original_db_path = flask_app.config.get("DATABASE_PATH")
    flask_app.config["DATABASE_PATH"] = db_path

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            with test_client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield test_client, rm, rpm

    (api_mod.reactome_mapping_model, api_mod.reactome_proposal_model,
     api_mod.cache_model, api_mod.ke_metadata_index,
     admin_mod.reactome_mapping_model, admin_mod.reactome_proposal_model) = originals
    flask_app.config["DATABASE_PATH"] = original_db_path

    os.close(fd)
    os.unlink(db_path)


def _seed(rm, **overrides):
    payload = dict(
        ke_id="KE 177",
        ke_title="Mitochondrial dysfunction",
        reactome_id="R-HSA-5357801",
        pathway_name="Programmed Cell Death",
        confidence_level="high",
        created_by="github:someone",
        proposed_relationship="causative",
        proposed_basis="known",
        proposed_specificity="specific",
        proposed_coverage="complete",
    )
    payload.update(overrides)
    return rm.create_mapping(**payload)


def _revise(client, ke_id="KE 177", reactome_id="R-HSA-5357801", **overrides):
    form = {
        # The Explore table sends the whole /api/v1 row, which spells the title
        # `ke_name` rather than `ke_title`.
        "entry": json.dumps({
            "ke_id": ke_id, "reactome_id": reactome_id,
            "ke_name": "Mitochondrial dysfunction",
            "pathway_name": "Programmed Cell Death",
        }),
        "userName": "Test Curator",
        "userEmail": "curator@example.com",
        "userAffiliation": "Test Org",
        "step1": "causative",
        "step2": "uncertain",
        "step3": "loose",
        "step4": "minor",
    }
    form.update(overrides)
    form = {k: v for k, v in form.items() if v is not None}
    return client.post("/submit_reactome_proposal", data=form)


def _approve(client, proposal_id):
    return client.post(f"/admin/reactome-proposals/{proposal_id}/approve", data={})


def _only_proposal(rpm):
    proposals = rpm.get_all_proposals()
    assert len(proposals) == 1
    return proposals[0]


# --------------------------------------------------------------------------
# The round trip — previously impossible at all
# --------------------------------------------------------------------------

def test_revision_roundtrip_carries_the_revised_answers(reactome_revision_client):
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    assert _revise(client).status_code == 200
    proposal = _only_proposal(rpm)
    assert proposal["proposed_basis"] == "uncertain"
    assert proposal["proposed_specificity"] == "loose"
    assert proposal["proposed_coverage"] == "minor"

    assert _approve(client, proposal["id"]).status_code == 200

    row = rm.get_all_mappings()[0]
    assert row["proposed_basis"] == "uncertain"
    assert row["proposed_specificity"] == "loose"
    assert row["proposed_coverage"] == "minor"
    # The mapping no longer describes the state it was revised away from.
    assert row["proposed_basis"] != "known"
    assert row["assessment_version"] == "v2"


def test_tier_is_derived_and_can_move(reactome_revision_client):
    """The thing D-02 prevented: correcting a wrong tier in place.

    uncertain/loose/minor scores 0 + 0 + 0.5 = 0.5, +1.0 for Cellular = 1.5,
    which is below the 2.5 medium threshold — so 'low'.
    """
    client, rm, rpm = reactome_revision_client
    _seed(rm, confidence_level="high")

    assert _revise(client).status_code == 200
    assert _only_proposal(rpm)["proposed_confidence"] == "low"

    assert _approve(client, _only_proposal(rpm)["id"]).status_code == 200
    assert rm.get_all_mappings()[0]["confidence_level"] == "low"


def test_the_mapping_keeps_its_identity_across_a_revision(reactome_revision_client):
    """Before this, correcting a tier meant delete-and-recreate, which loses the
    uuid every downstream citation depends on."""
    client, rm, rpm = reactome_revision_client
    _seed(rm)
    original_uuid = rm.get_all_mappings()[0]["uuid"]

    assert _revise(client).status_code == 200
    assert _approve(client, _only_proposal(rpm)["id"]).status_code == 200

    rows = rm.get_all_mappings()
    assert len(rows) == 1
    assert rows[0]["uuid"] == original_uuid


def test_approval_records_the_curator(reactome_revision_client):
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    assert _revise(client).status_code == 200
    assert _approve(client, _only_proposal(rpm)["id"]).status_code == 200

    row = rm.get_all_mappings()[0]
    assert row["approved_by_curator"] == "github:testadmin"
    assert row["proposed_by"] == "github:testadmin"


# --------------------------------------------------------------------------
# Deletion still asks nothing
# --------------------------------------------------------------------------

def test_deletion_still_works_and_needs_no_assessment(reactome_revision_client):
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    resp = _revise(
        client, deleteEntry="on",
        step1=None, step2=None, step3=None, step4=None,
    )
    assert resp.status_code == 200

    proposal = _only_proposal(rpm)
    assert proposal["proposed_delete"] in (1, True)
    assert proposal["proposed_confidence"] is None

    assert _approve(client, proposal["id"]).status_code == 200
    assert rm.get_all_mappings() == []


def test_deletion_rejects_an_assessment(reactome_revision_client):
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    resp = _revise(client, deleteEntry="on")
    assert resp.status_code == 400
    assert rpm.get_all_proposals() == []


# --------------------------------------------------------------------------
# The same refusals as KE-WP, because it is the same resolver
# --------------------------------------------------------------------------

@pytest.mark.parametrize("omitted", ["step1", "step2", "step3", "step4"])
def test_partial_assessment_is_refused(reactome_revision_client, omitted):
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    resp = _revise(client, **{omitted: None})
    assert resp.status_code == 400
    assert rpm.get_all_proposals() == []


def test_invalid_answer_is_rejected(reactome_revision_client):
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    resp = _revise(client, step3="banana")
    assert resp.status_code == 400
    assert "step3" in json.dumps(resp.get_json())
    assert rpm.get_all_proposals() == []


def test_unscorable_ke_is_refused_loudly(reactome_revision_client):
    client, rm, rpm = reactome_revision_client
    _seed(rm, ke_id="KE 999", reactome_id="R-HSA-999")

    resp = _revise(client, ke_id="KE 999", reactome_id="R-HSA-999")
    assert resp.status_code == 409
    assert rpm.get_all_proposals() == []
    assert rm.get_all_mappings()[0]["confidence_level"] == "high"


def test_ke_title_falls_back_to_ke_name(reactome_revision_client):
    """The Explore row spells it `ke_name`; the queue's display column must not
    silently store NULL because of that."""
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    assert _revise(client).status_code == 200
    assert _only_proposal(rpm)["ke_title"] == "Mitochondrial dysfunction"


# --------------------------------------------------------------------------
# What D-02 still guarantees
# --------------------------------------------------------------------------

def test_no_dimension_scores_reach_the_mapping(reactome_revision_client):
    """Reactome's assessment is the four answers, not GO's three scores.

    Posting the GO shape at the revision route must not smuggle scores onto a
    Reactome mapping — there is no column for them, and there must not be one.
    """
    client, rm, rpm = reactome_revision_client
    _seed(rm)

    assert _revise(
        client, connection_score=3, specificity_score=3, evidence_score=3,
    ).status_code == 200
    assert _approve(client, _only_proposal(rpm)["id"]).status_code == 200

    row = rm.get_all_mappings()[0]
    for absent in ("connection_score", "specificity_score", "evidence_score",
                   "connection_type"):
        assert absent not in row
