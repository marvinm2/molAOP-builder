"""#246 + #247 — a reviewer can refine any pending assessment, and can see what
the revision replaces.

#235 made a pending **GO** proposal's assessment editable at review, on the
argument that a reviewer who disagreed with one dimension had two options:
approve something they considered wrong, or reject and ask for a resubmit. That
argument is not GO-specific, but the implementation was — WikiPathways and
Reactome reviewers still had exactly the two bad options.

And a reviewer opening a *revision* was shown what is proposed without what it
replaces, so "Medium" arrived with nothing to compare it against. Once a
revision carries four answers (#245) that gets worse rather than better: eight
values in play and no indication which of them moved.
"""
import json
import os
import tempfile

import pytest


@pytest.fixture
def wp_admin():
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    import src.blueprints.api as api_mod
    from src.core.models import CacheModel, Database, MappingModel, ProposalModel

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    mm, pm, cm = MappingModel(db), ProposalModel(db), CacheModel(db)

    originals = (
        api_mod.proposal_model, api_mod.mapping_model, api_mod.cache_model,
        admin_mod.proposal_model, admin_mod.mapping_model,
    )
    api_mod.proposal_model = pm
    api_mod.mapping_model = mm
    api_mod.cache_model = cm
    admin_mod.proposal_model = pm
    admin_mod.mapping_model = mm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    original_db_path = flask_app.config.get("DATABASE_PATH")
    flask_app.config["DATABASE_PATH"] = db_path

    with flask_app.test_client() as client:
        with flask_app.app_context():
            with client.session_transaction() as sess:
                sess["user"] = {"username": "github:testadmin",
                                "email": "admin@example.com"}
            yield client, mm, pm

    (api_mod.proposal_model, api_mod.mapping_model, api_mod.cache_model,
     admin_mod.proposal_model, admin_mod.mapping_model) = originals
    flask_app.config["DATABASE_PATH"] = original_db_path
    os.close(fd)
    os.unlink(db_path)


def _new_pair(pm, **overrides):
    payload = dict(
        ke_id="KE 1097", ke_title="T", wp_id="WP4313", wp_title="P",
        connection_type="causative", confidence_level="low",
        provider_username="github:submitter",
        proposed_relationship="causative", proposed_basis="uncertain",
        proposed_specificity="loose", proposed_coverage="minor",
    )
    payload.update(overrides)
    return pm.create_new_pair_proposal(**payload)


def _approve(client, proposal_id, **fields):
    return client.post(f"/admin/proposals/{proposal_id}/approve", data=fields)


# --------------------------------------------------------------------------
# #246 — the reviewer can refine, on WikiPathways too
# --------------------------------------------------------------------------

def test_reviewer_edits_are_recorded_on_the_mapping(wp_admin):
    """The whole point: a reviewer who disagrees corrects it here."""
    client, mm, pm = wp_admin
    pid = _new_pair(pm)

    resp = _approve(client, pid, step1="causative", step2="known",
                    step3="specific", step4="complete")
    assert resp.status_code == 200

    row = mm.get_all_mappings()[0]
    assert row["proposed_basis"] == "known"
    assert row["proposed_specificity"] == "specific"
    assert row["proposed_coverage"] == "complete"
    assert row["assessment_version"] == "v2"


def test_untouched_approval_equals_a_bare_one(wp_admin):
    """The guarantee the GO path already holds: echoing the submitter's answers
    back must not change what is stored, or an unedited review would silently
    differ from a bulk approval."""
    client, mm, pm = wp_admin

    pid = _new_pair(pm)
    assert _approve(client, pid).status_code == 200
    bare = mm.get_all_mappings()[0]

    pid2 = _new_pair(pm, wp_id="WP999")
    assert _approve(client, pid2, step1="causative", step2="uncertain",
                    step3="loose", step4="minor").status_code == 200
    echoed = [m for m in mm.get_all_mappings() if m["wp_id"] == "WP999"][0]

    for column in ("proposed_relationship", "proposed_basis",
                   "proposed_specificity", "proposed_coverage",
                   "confidence_level", "assessment_version"):
        assert bare[column] == echoed[column], column


def test_a_partial_reviewer_assessment_is_refused(wp_admin):
    """Filling the gaps from the submitter's answers would attribute answers to
    a reviewer who never gave them."""
    client, mm, pm = wp_admin
    pid = _new_pair(pm)

    resp = _approve(client, pid, step1="causative", step2="known")
    assert resp.status_code == 400
    assert "all four" in resp.get_json()["error"]
    assert mm.get_all_mappings() == []


def test_an_out_of_whitelist_answer_is_refused(wp_admin):
    client, mm, pm = wp_admin
    pid = _new_pair(pm)

    resp = _approve(client, pid, step1="causative", step2="banana",
                    step3="loose", step4="minor")
    assert resp.status_code == 400
    assert mm.get_all_mappings() == []


def test_an_explicit_confidence_overrides_the_recompute(wp_admin):
    """A reviewer pinning a tier is a deliberate act and is the last word."""
    client, mm, pm = wp_admin
    pid = _new_pair(pm)

    assert _approve(client, pid, step1="causative", step2="known",
                    step3="specific", step4="complete",
                    confidence_level="low").status_code == 200
    assert mm.get_all_mappings()[0]["confidence_level"] == "low"


def test_an_invalid_confidence_is_refused(wp_admin):
    client, mm, pm = wp_admin
    pid = _new_pair(pm)

    resp = _approve(client, pid, confidence_level="excellent")
    assert resp.status_code == 400
    assert mm.get_all_mappings() == []


def test_edits_apply_to_a_revision_too(wp_admin):
    """#246 asks for both shapes. A revision is where a reviewer is overriding
    someone else's judgement, so it needs this at least as much."""
    client, mm, pm = wp_admin
    mapping_id = mm.create_mapping(
        ke_id="KE 1097", ke_title="T", wp_id="WP4313", wp_title="P",
        connection_type="causative", confidence_level="high",
        created_by="github:someone", proposed_relationship="causative",
        proposed_basis="known", proposed_specificity="specific",
        proposed_coverage="complete",
    )
    pid = pm.create_proposal(
        mapping_id=mapping_id, user_name="U", user_email="u@example.com",
        user_affiliation="A", provider_username="github:submitter",
        proposed_confidence="medium",
        proposed_relationship="causative", proposed_basis="likely",
        proposed_specificity="includes", proposed_coverage="keysteps",
    )

    assert _approve(client, pid, step1="causative", step2="uncertain",
                    step3="loose", step4="minor").status_code == 200

    row = mm.get_all_mappings()[0]
    assert row["proposed_basis"] == "uncertain"      # the reviewer's, not the submitter's
    assert row["proposed_coverage"] == "minor"


# --------------------------------------------------------------------------
# #247 — the panel can show what a revision replaces
# --------------------------------------------------------------------------

def test_detail_exposes_the_current_assessment_for_a_revision(wp_admin):
    client, mm, pm = wp_admin
    mapping_id = mm.create_mapping(
        ke_id="KE 1097", ke_title="T", wp_id="WP4313", wp_title="P",
        connection_type="causative", confidence_level="high",
        created_by="github:someone", proposed_relationship="causative",
        proposed_basis="known", proposed_specificity="specific",
        proposed_coverage="complete",
    )
    pid = pm.create_proposal(
        mapping_id=mapping_id, user_name="U", user_email="u@example.com",
        user_affiliation="A", proposed_relationship="causative",
        proposed_basis="uncertain", proposed_specificity="loose",
        proposed_coverage="minor",
    )

    detail = client.get(f"/admin/proposals/{pid}").get_json()
    current = detail["current_assessment"]
    assert current["proposed_basis"] == "known"
    assert current["confidence_level"] == "high"
    assert current["assessment_version"] == "v2"


def test_new_pair_has_no_current_assessment(wp_admin):
    """An empty column would imply something was lost."""
    client, mm, pm = wp_admin
    pid = _new_pair(pm)

    detail = client.get(f"/admin/proposals/{pid}").get_json()
    assert detail["current_assessment"] is None


def test_a_v1_mapping_reports_its_version_rather_than_four_blanks(wp_admin):
    """The case worth pinning hardest: a revision against a legacy mapping is
    exactly where a reviewer most needs to be told there is nothing to compare
    against, because four NULLs read as 'no answers given' when the truth is
    'answers were never collectable'."""
    client, mm, pm = wp_admin
    mapping_id = mm.create_mapping(
        ke_id="KE 55", ke_title="T", wp_id="WP100", wp_title="P",
        connection_type="other", confidence_level="low",
        created_by="github:someone",
    )
    pid = pm.create_proposal(
        mapping_id=mapping_id, user_name="U", user_email="u@example.com",
        user_affiliation="A", proposed_relationship="causative",
        proposed_basis="known", proposed_specificity="specific",
        proposed_coverage="complete",
    )

    current = client.get(f"/admin/proposals/{pid}").get_json()["current_assessment"]
    assert current["assessment_version"] == "v1"
    assert current["proposed_basis"] is None


# --------------------------------------------------------------------------
# The panel itself
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def panel_js():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "js", "admin_proposals.js",
    )
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_the_step_editor_exists_and_is_wired(panel_js):
    for marker in ("_renderStepAssessmentEditor", "stepReviewEditor",
                   "step-review-select", "stepReviewConfidence",
                   "stepReviewReset", "_initStepReviewEditor"):
        assert marker in panel_js, marker


def test_the_editor_is_not_gated_on_go(panel_js):
    """#235's implementation was GO-only; the gate is now per-instrument."""
    assert "_usesStepAssessment()" in panel_js
    assert "resource === 'wp' || _config.resource === 'reactome'" in panel_js


def test_the_step_editor_posts_all_four_or_none(panel_js):
    approve = panel_js[panel_js.index("function _singleApprove"):]
    assert "stepQuestions.every" in approve[:2500]
    assert "formData.append(q.field" in approve[:2500]


def test_no_client_side_wp_tier_computation(panel_js):
    """The KE-WP score depends on the Key Event's biological level, which this
    panel does not hold — and the browser getting that wrong is exactly what
    #237 was about. The label must promise a server-side calculation."""
    assert "Auto (calculated on approval)" in panel_js
    assert "_computeStepConfidence" not in panel_js


def test_changed_answers_are_marked(panel_js):
    assert "assessment-changed" in panel_js
    assert "changed from " in panel_js


def test_deletion_proposals_get_no_editor(panel_js):
    """A deletion asserts nothing about confidence, so there is nothing to edit."""
    assert "!_isDeletionProposal(p)" in panel_js
