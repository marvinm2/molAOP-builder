"""#245 — a KE-WP revision carries the four assessment answers.

Creating a mapping and correcting one are the same judgement. Creation asked
four structured questions and derived the tier; a revision asked for a tier
directly, as a High/Medium/Low radio, and recorded no reasoning at all. So a
revision was the one path that could move a mapping between tiers with nothing
stated to justify it — on the mappings curated longest ago and understood least
well — and after approval the mapping's stored assessment described the state
it had just been revised away from.

These tests pin the revision path end to end: submit -> approve -> the mapping
holds the *revised* answers, and a tier derived from them rather than asserted.

`/submit_proposal` had no behavioural test coverage before this file; the WP
revision route was covered only by a login check and an invalid-JSON smoke test
(`tests/test_app.py`), which is why the bare-radio path survived the assessment
work that replaced it everywhere else.

Worked example, reused from tests/test_confidence_recompute_wp.py — KE 1097 is
Tissue level, so it qualifies for the +1.0 bonus:

    likely 2.0 + includes 1.0 + keysteps 1.0 = 4.0   -> medium
                                       +1.0 bonus    -> 5.0 -> high
"""
import json
import os
import tempfile

import pytest


@pytest.fixture
def revision_client():
    """Client with api + admin wired to a shared temp DB, plus a KE metadata
    index the revision route can resolve a biological level from."""
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

    originals = (
        api_mod.proposal_model, api_mod.mapping_model, api_mod.cache_model,
        api_mod.ke_metadata_index, admin_mod.proposal_model, admin_mod.mapping_model,
    )

    api_mod.proposal_model = pm
    api_mod.mapping_model = mm
    api_mod.cache_model = cm
    # KE 1097 is Tissue (qualifies for the bonus). KE 999 is deliberately
    # absent, standing in for the Key Events the snapshot lags behind (#239).
    api_mod.ke_metadata_index = {
        "KE 1097": {"KElabel": "KE 1097", "biolevel": "Tissue"},
    }
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
            yield test_client, mm, pm

    (api_mod.proposal_model, api_mod.mapping_model, api_mod.cache_model,
     api_mod.ke_metadata_index, admin_mod.proposal_model,
     admin_mod.mapping_model) = originals

    os.close(fd)
    os.unlink(db_path)


def _seed_mapping(mm, ke_id="KE 1097", wp_id="WP4313", **overrides):
    """An approved mapping to raise a revision against."""
    payload = dict(
        ke_id=ke_id,
        ke_title="Occurrence, renal proximal tubular necrosis",
        wp_id=wp_id,
        wp_title="Ferroptosis",
        connection_type="causative",
        confidence_level="low",
        created_by="github:someone",
        proposed_relationship="causative",
        proposed_basis="uncertain",
        proposed_specificity="loose",
        proposed_coverage="minor",
    )
    payload.update(overrides)
    return mm.create_mapping(**payload)


def _revise(client, ke_id="KE 1097", wp_id="WP4313", **overrides):
    form = {
        "entry": json.dumps({"ke_id": ke_id, "wp_id": wp_id}),
        "userName": "Test Curator",
        "userEmail": "curator@example.com",
        "userAffiliation": "Test Org",
        "step1": "causative",
        "step2": "likely",
        "step3": "includes",
        "step4": "keysteps",
    }
    form.update(overrides)
    form = {k: v for k, v in form.items() if v is not None}
    return client.post("/submit_proposal", data=form)


def _approve(client, proposal_id):
    return client.post(f"/admin/proposals/{proposal_id}/approve", data={})


def _only_proposal(pm):
    proposals = pm.get_all_proposals()
    assert len(proposals) == 1
    return proposals[0]


# --------------------------------------------------------------------------
# The round trip — the thing the umbrella is actually about
# --------------------------------------------------------------------------

def test_revision_roundtrip_carries_the_revised_answers(revision_client):
    """Submit a revision with four answers, approve, and the mapping holds the
    revised ones — not the superseded ones it was created with."""
    client, mm, pm = revision_client
    _seed_mapping(mm)

    assert _revise(client).status_code == 200
    proposal = _only_proposal(pm)

    # The proposal records the reasoning, not just an outcome.
    assert proposal["proposed_relationship"] == "causative"
    assert proposal["proposed_basis"] == "likely"
    assert proposal["proposed_specificity"] == "includes"
    assert proposal["proposed_coverage"] == "keysteps"

    assert _approve(client, proposal["id"]).status_code == 200

    row = mm.get_all_mappings()[0]
    assert row["proposed_basis"] == "likely"
    assert row["proposed_specificity"] == "includes"
    assert row["proposed_coverage"] == "keysteps"
    # Previously the mapping kept describing the state it was revised away from.
    assert row["proposed_basis"] != "uncertain"
    assert row["assessment_version"] == "v2"


def test_tier_is_derived_from_the_answers_not_asserted(revision_client):
    """likely/includes/keysteps at Tissue level scores 5.0 -> high, even though
    the mapping was low and nothing in the request named a tier."""
    client, mm, pm = revision_client
    _seed_mapping(mm, confidence_level="low")

    assert _revise(client).status_code == 200
    assert _only_proposal(pm)["proposed_confidence"] == "high"

    assert _approve(client, _only_proposal(pm)["id"]).status_code == 200
    assert mm.get_all_mappings()[0]["confidence_level"] == "high"


def test_a_weaker_assessment_lowers_the_tier(revision_client):
    """The instrument works in both directions — a correction that finds less
    evidence must be able to move a mapping down."""
    client, mm, pm = revision_client
    _seed_mapping(mm, confidence_level="high")

    resp = _revise(client, step2="uncertain", step3="loose", step4="minor")
    assert resp.status_code == 200
    assert _only_proposal(pm)["proposed_confidence"] == "low"

    assert _approve(client, _only_proposal(pm)["id"]).status_code == 200
    assert mm.get_all_mappings()[0]["confidence_level"] == "low"


def test_relationship_reaches_connection_type_through_the_map(revision_client):
    """A revision must not reintroduce #264: `bidirectional` is an assessment
    answer, not a connection type."""
    client, mm, pm = revision_client
    _seed_mapping(mm)

    assert _revise(client, step1="bidirectional").status_code == 200
    assert _approve(client, _only_proposal(pm)["id"]).status_code == 200

    row = mm.get_all_mappings()[0]
    assert row["connection_type"] == "other"
    assert row["proposed_relationship"] == "bidirectional"


# --------------------------------------------------------------------------
# Deletion asks nothing
# --------------------------------------------------------------------------

def test_deletion_needs_no_assessment(revision_client):
    """A deletion proposal makes no assertion about confidence."""
    client, mm, pm = revision_client
    _seed_mapping(mm)

    resp = _revise(
        client, deleteEntry="on",
        step1=None, step2=None, step3=None, step4=None,
    )
    assert resp.status_code == 200

    proposal = _only_proposal(pm)
    assert proposal["proposed_delete"] in (1, True)
    assert proposal["proposed_confidence"] is None
    assert proposal["proposed_relationship"] is None

    assert _approve(client, proposal["id"]).status_code == 200
    assert mm.get_all_mappings() == []


def test_deletion_rejects_an_assessment(revision_client):
    """Answers alongside a deletion would record reasoning for a mapping about
    to vanish — a contradiction worth refusing rather than silently dropping."""
    client, mm, pm = revision_client
    _seed_mapping(mm)

    resp = _revise(client, deleteEntry="on")
    assert resp.status_code == 400
    assert pm.get_all_proposals() == []


# --------------------------------------------------------------------------
# A revision cannot be partial
# --------------------------------------------------------------------------

@pytest.mark.parametrize("omitted", ["step1", "step2", "step3", "step4"])
def test_partial_assessment_is_refused(revision_client, omitted):
    """A tier derived from an incomplete score is not a tier anyone can defend.

    The new-pair path tolerates a partial assessment for backward-compatibility
    with pre-Phase-34 form-posters; a revision has no such legacy and must not
    inherit the tolerance.
    """
    client, mm, pm = revision_client
    _seed_mapping(mm)

    resp = _revise(client, **{omitted: None})
    assert resp.status_code == 400
    assert pm.get_all_proposals() == []


def test_no_assessment_and_no_deletion_is_refused(revision_client):
    """The old modal could submit a proposal that changed nothing."""
    client, mm, pm = revision_client
    _seed_mapping(mm)

    resp = _revise(client, step1=None, step2=None, step3=None, step4=None)
    assert resp.status_code == 400
    assert pm.get_all_proposals() == []


def test_invalid_answer_is_rejected(revision_client):
    """Same whitelists as creation — the mixin is shared for exactly this."""
    client, mm, pm = revision_client
    _seed_mapping(mm)

    resp = _revise(client, step2="banana")
    assert resp.status_code == 400
    assert "step2" in json.dumps(resp.get_json())
    assert pm.get_all_proposals() == []


# --------------------------------------------------------------------------
# The tier must never be silently absent
# --------------------------------------------------------------------------

def test_unscorable_ke_is_refused_loudly(revision_client):
    """A Key Event missing from the metadata snapshot has no biological level,
    so the assessment cannot be scored.

    On the new-pair path the browser's own tier stands in. A revision has no
    submitted tier to fall back to, so storing the assessment anyway would
    record four answers against a tier that never moves — the silent no-op this
    codebase keeps rediscovering. Refuse instead.
    """
    client, mm, pm = revision_client
    _seed_mapping(mm, ke_id="KE 999", wp_id="WP999")

    resp = _revise(client, ke_id="KE 999", wp_id="WP999")
    assert resp.status_code == 409
    assert pm.get_all_proposals() == []
    # The mapping is untouched, and the curator is told why.
    assert mm.get_all_mappings()[0]["confidence_level"] == "low"
    assert "snapshot" in resp.get_json()["error"]


def test_unknown_mapping_returns_404(revision_client):
    client, mm, pm = revision_client
    resp = _revise(client, ke_id="KE 1097", wp_id="WP4313")
    assert resp.status_code == 404
