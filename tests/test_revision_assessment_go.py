"""#245 — a KE-GO revision carries the three-dimension assessment.

GO's instrument is not KE-WP's. Creating a GO mapping asks for a connection
type from GO's own four-term vocabulary plus three dimensions scored
High/Medium/Low, and takes a weighted average; KE-WP asks four questions and
sums them with a biological-level bonus. Aligning the two *interfaces* — a
correction states its grounds, and the tier is derived rather than asserted —
does not mean merging the two *instruments*.

Two defects are pinned here beyond the umbrella's own subject:

1. The GO revision route validated ``changeType`` against the **WikiPathways**
   vocabulary (causative/responsive/undefined), none of which is a valid GO
   connection type. A GO revision could not propose a valid connection type and
   could propose three invalid ones.
2. The GO approve route's revision branch discarded everything but the tier and
   the connection type — no dimension scores, no version stamp, no source
   version fields — so an approved revision left the mapping describing the
   assessment it had just superseded.
"""
import json
import os
import tempfile

import pytest

from src.core.schemas import GO_CONNECTION_TYPES


@pytest.fixture
def go_revision_client():
    """api + admin wired to a shared temp DB, authenticated as an admin."""
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    import src.blueprints.api as api_mod
    from src.core.models import (
        CacheModel, Database, GoMappingModel, GoProposalModel,
    )

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    gm = GoMappingModel(db)
    gpm = GoProposalModel(db)
    cm = CacheModel(db)

    originals = (
        api_mod.go_mapping_model, api_mod.go_proposal_model, api_mod.cache_model,
        admin_mod.go_mapping_model, admin_mod.go_proposal_model,
    )

    api_mod.go_mapping_model = gm
    api_mod.go_proposal_model = gpm
    api_mod.cache_model = cm
    admin_mod.go_mapping_model = gm
    admin_mod.go_proposal_model = gpm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    # The submission rate limiter keys off DATABASE_PATH. Without pointing it
    # at this test's own DB it accumulates across the whole session and the
    # later tests in this file 429 instead of asserting what they mean to.
    original_db_path = flask_app.config.get("DATABASE_PATH")
    flask_app.config["DATABASE_PATH"] = db_path

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            with test_client.session_transaction() as sess:
                sess["user"] = {
                    "username": "github:testadmin",
                    "email": "admin@example.com",
                }
            yield test_client, gm, gpm

    (api_mod.go_mapping_model, api_mod.go_proposal_model, api_mod.cache_model,
     admin_mod.go_mapping_model, admin_mod.go_proposal_model) = originals
    flask_app.config["DATABASE_PATH"] = original_db_path

    os.close(fd)
    os.unlink(db_path)


def _seed(gm, **overrides):
    """An approved GO mapping, weakly assessed, to raise a revision against."""
    payload = dict(
        ke_id="KE 177",
        ke_title="Mitochondrial dysfunction",
        go_id="GO:0140053",
        go_name="mitochondrial gene expression",
        connection_type="related",
        confidence_level="low",
        created_by="github:someone",
        connection_score=1,
        specificity_score=1,
        evidence_score=1,
    )
    payload.update(overrides)
    return gm.create_mapping(**payload)


def _revise(client, **overrides):
    form = {
        "entry": json.dumps({"ke_id": "KE 177", "go_id": "GO:0140053"}),
        "userName": "Test Curator",
        "userEmail": "curator@example.com",
        "userAffiliation": "Test Org",
        "changeType": "describes",
        "connection_score": 3,
        "specificity_score": 3,
        "evidence_score": 3,
    }
    form.update(overrides)
    form = {k: v for k, v in form.items() if v is not None}
    return client.post("/submit_go_proposal", data=form)


def _approve(client, proposal_id):
    return client.post(f"/admin/go-proposals/{proposal_id}/approve", data={})


def _only_proposal(gpm):
    proposals = gpm.get_all_go_proposals()
    assert len(proposals) == 1
    return proposals[0]


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------

def test_revision_roundtrip_carries_the_revised_scores(go_revision_client):
    """The approved mapping holds the revision's reasoning, not the superseded."""
    client, gm, gpm = go_revision_client
    _seed(gm)

    assert _revise(client).status_code == 200
    proposal = _only_proposal(gpm)
    assert proposal["proposed_connection_score"] == 3
    assert proposal["proposed_specificity_score"] == 3
    assert proposal["proposed_evidence_score"] == 3

    assert _approve(client, proposal["id"]).status_code == 200

    row = gm.get_all_mappings()[0]
    assert row["connection_score"] == 3
    assert row["specificity_score"] == 3
    assert row["evidence_score"] == 3
    # Previously the approve branch dropped all three and left 1/1/1 in place.
    assert row["connection_score"] != 1
    assert row["assessment_version"] == "v2"


def test_tier_is_derived_from_the_dimensions(go_revision_client):
    """3/3/3 is the strongest assessment, so the mapping moves low -> high with
    nothing in the request naming a tier."""
    client, gm, gpm = go_revision_client
    _seed(gm, confidence_level="low")

    assert _revise(client).status_code == 200
    assert _only_proposal(gpm)["proposed_confidence"] == "high"

    assert _approve(client, _only_proposal(gpm)["id"]).status_code == 200
    assert gm.get_all_mappings()[0]["confidence_level"] == "high"


def test_a_weaker_assessment_lowers_the_tier(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm, confidence_level="high", connection_score=3,
          specificity_score=3, evidence_score=3)

    resp = _revise(client, connection_score=1, specificity_score=1,
                   evidence_score=1)
    assert resp.status_code == 200
    assert _only_proposal(gpm)["proposed_confidence"] == "low"

    assert _approve(client, _only_proposal(gpm)["id"]).status_code == 200
    assert gm.get_all_mappings()[0]["confidence_level"] == "low"


def test_connection_type_is_revisable(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm, connection_type="related")

    assert _revise(client, changeType="describes").status_code == 200
    assert _approve(client, _only_proposal(gpm)["id"]).status_code == 200
    assert gm.get_all_mappings()[0]["connection_type"] == "describes"


# --------------------------------------------------------------------------
# The vocabulary bug
# --------------------------------------------------------------------------

@pytest.mark.parametrize("go_type", GO_CONNECTION_TYPES)
def test_every_go_connection_type_is_accepted(go_revision_client, go_type):
    """All four were rejected before: the field validated against the KE-WP
    vocabulary, so a GO revision could not name a valid GO connection type."""
    client, gm, gpm = go_revision_client
    _seed(gm)

    assert _revise(client, changeType=go_type).status_code == 200
    assert _only_proposal(gpm)["proposed_connection_type"] == go_type


@pytest.mark.parametrize("wp_type", ["causative", "responsive", "undefined"])
def test_wikipathways_connection_types_are_rejected(go_revision_client, wp_type):
    """The mirror image: these three were the *only* accepted values, and none
    of them means anything to a GO mapping."""
    client, gm, gpm = go_revision_client
    _seed(gm)

    resp = _revise(client, changeType=wp_type)
    assert resp.status_code == 400
    assert gpm.get_all_go_proposals() == []


# --------------------------------------------------------------------------
# Deletion, and partial assessments
# --------------------------------------------------------------------------

def test_deletion_needs_no_assessment(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm)

    resp = _revise(
        client, deleteEntry="on", changeType=None,
        connection_score=None, specificity_score=None, evidence_score=None,
    )
    assert resp.status_code == 200

    proposal = _only_proposal(gpm)
    assert proposal["proposed_delete"] in (1, True)
    assert proposal["proposed_confidence"] is None

    assert _approve(client, proposal["id"]).status_code == 200
    assert gm.get_all_mappings() == []


def test_deletion_rejects_an_assessment(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm)

    resp = _revise(client, deleteEntry="on")
    assert resp.status_code == 400
    assert gpm.get_all_go_proposals() == []


@pytest.mark.parametrize(
    "omitted",
    ["changeType", "connection_score", "specificity_score", "evidence_score"],
)
def test_partial_assessment_is_refused(go_revision_client, omitted):
    """A tier derived from an incomplete score is not defensible."""
    client, gm, gpm = go_revision_client
    _seed(gm)

    resp = _revise(client, **{omitted: None})
    assert resp.status_code == 400
    assert gpm.get_all_go_proposals() == []


@pytest.mark.parametrize("bad_score", [0, 4, 99, -1])
def test_out_of_range_dimension_is_rejected(go_revision_client, bad_score):
    """The form offers 3/2/1 only. The new-pair GO route still parses these
    with a bare int cast and no whitelist; the revision path does not inherit
    that.
    """
    client, gm, gpm = go_revision_client
    _seed(gm)

    resp = _revise(client, connection_score=bad_score)
    assert resp.status_code == 400
    assert gpm.get_all_go_proposals() == []


def test_no_changes_is_refused(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm)

    resp = _revise(
        client, changeType=None, connection_score=None,
        specificity_score=None, evidence_score=None,
    )
    assert resp.status_code == 400
    assert gpm.get_all_go_proposals() == []


# --------------------------------------------------------------------------
# A legacy revision must not claim an assessment it has no answers for
# --------------------------------------------------------------------------

def test_legacy_revision_leaves_the_stored_assessment_alone(go_revision_client):
    """A revision submitted before #245 has no dimension scores.

    Approving it must not blank the mapping's existing scores, nor stamp a v2
    it has no answers to back — the mapping keeps what it had, and only the
    provenance moves.
    """
    client, gm, gpm = go_revision_client
    mapping_id = _seed(gm, connection_score=2, specificity_score=2,
                       evidence_score=2)

    # Write the pre-#245 shape directly: a tier and a type, no dimensions.
    proposal_id = gpm.create_proposal(
        mapping_id=mapping_id,
        user_name="U", user_email="u@example.com", user_affiliation="A",
        provider_username="github:someone",
        proposed_confidence="high",
        proposed_connection_type="involves",
    )

    assert _approve(client, proposal_id).status_code == 200

    row = gm.get_all_mappings()[0]
    assert row["connection_score"] == 2
    assert row["specificity_score"] == 2
    assert row["evidence_score"] == 2
    assert row["confidence_level"] == "high"
    assert row["connection_type"] == "involves"


# --------------------------------------------------------------------------
# #246 — the reviewer can refine a GO revision, not only a new pair
# --------------------------------------------------------------------------

def test_reviewer_edits_are_applied_to_a_revision(go_revision_client):
    """#235 gave the review panel editable GO controls but wired only the
    new-pair approve path to read them, so for a change proposal the panel
    posted the reviewer's values and the server discarded them."""
    client, gm, gpm = go_revision_client
    _seed(gm, confidence_level="low")

    assert _revise(client, connection_score=1, specificity_score=1,
                   evidence_score=1).status_code == 200
    proposal = _only_proposal(gpm)
    assert proposal["proposed_confidence"] == "low"

    # The reviewer disagrees and scores it up before approving.
    resp = client.post(
        f"/admin/go-proposals/{proposal['id']}/approve",
        data={"connection_score": 3, "specificity_score": 3, "evidence_score": 3},
    )
    assert resp.status_code == 200

    row = gm.get_all_mappings()[0]
    assert row["connection_score"] == 3
    assert row["evidence_score"] == 3
    # The tier follows the reviewer's scores, not the submitter's.
    assert row["confidence_level"] == "high"


def test_reviewer_can_change_the_connection_type_on_a_revision(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm, connection_type="related")

    assert _revise(client, changeType="describes").status_code == 200
    proposal = _only_proposal(gpm)

    assert client.post(
        f"/admin/go-proposals/{proposal['id']}/approve",
        data={"connection_type": "involves"},
    ).status_code == 200
    assert gm.get_all_mappings()[0]["connection_type"] == "involves"


def test_reviewer_confidence_override_wins_on_a_revision(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm, confidence_level="low")

    assert _revise(client).status_code == 200
    proposal = _only_proposal(gpm)

    assert client.post(
        f"/admin/go-proposals/{proposal['id']}/approve",
        data={"connection_score": 3, "specificity_score": 3,
              "evidence_score": 3, "confidence_level": "medium"},
    ).status_code == 200
    assert gm.get_all_mappings()[0]["confidence_level"] == "medium"


def test_invalid_reviewer_values_are_refused_on_a_revision(go_revision_client):
    client, gm, gpm = go_revision_client
    _seed(gm, confidence_level="low")

    assert _revise(client).status_code == 200
    proposal = _only_proposal(gpm)

    for payload in ({"connection_type": "causative"},
                    {"confidence_level": "excellent"}):
        resp = client.post(
            f"/admin/go-proposals/{proposal['id']}/approve", data=payload
        )
        assert resp.status_code == 400
    # Nothing written, proposal still pending.
    assert gm.get_all_mappings()[0]["confidence_level"] == "low"


def test_untouched_revision_approval_keeps_the_submitters_values(go_revision_client):
    """A bare approval must still store exactly what the submitter recorded."""
    client, gm, gpm = go_revision_client
    _seed(gm, confidence_level="low", connection_score=1,
          specificity_score=1, evidence_score=1)

    assert _revise(client, connection_score=2, specificity_score=2,
                   evidence_score=2).status_code == 200
    proposal = _only_proposal(gpm)

    assert _approve(client, proposal["id"]).status_code == 200
    row = gm.get_all_mappings()[0]
    assert (row["connection_score"], row["specificity_score"],
            row["evidence_score"]) == (2, 2, 2)
