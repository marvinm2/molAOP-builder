"""Phase 37 (#237) — the server scores the assessment, the browser does not.

`evaluateConfidence()` adds the +1.0 biological-level bonus from a client
instance variable that a restored form can lose, and `/submit` used to store
whatever tier the client sent. These tests pin the server-side recompute: the
stored tier follows from the four answers plus the Key Event's own biological
level, and the submitted tier is only honoured when the server genuinely
cannot score.

The worked example is the one from the issue — KE 1097 -> WP4313,
`likely` / `includes` / `keysteps` at Tissue level:

    evidence_quality.likely       2.0
    pathway_specificity.includes  1.0
    ke_coverage.keysteps          1.0
                                  ---
                                  4.0  -> medium
    biological_level.bonus       +1.0  (Tissue qualifies)
                                  ---
                                  5.0  -> high
"""
import os
import tempfile

import pytest

from src.core.assessment_scoring import (
    compute_ke_pathway_confidence,
    recompute_confidence_level,
    score_ke_pathway_assessment,
)
from src.core.config_loader import ConfigLoader


@pytest.fixture
def assessment_config():
    return ConfigLoader.load_config().ke_pathway_assessment


@pytest.fixture
def recompute_client():
    """Client wired to a temp DB with a KE metadata index the submit route
    can resolve a biological level from."""
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
    # KE 1097 is Tissue (qualifies for the bonus); KE 500 is Individual
    # (does not). KE 999 is deliberately absent, standing in for the KEs the
    # snapshot lags behind (#239).
    api_mod.ke_metadata_index = {
        "KE 1097": {"KElabel": "KE 1097", "biolevel": "Tissue"},
        "KE 500": {"KElabel": "KE 500", "biolevel": "Individual"},
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
            yield test_client, pm

    (api_mod.proposal_model, api_mod.mapping_model, api_mod.cache_model,
     api_mod.ke_metadata_index, admin_mod.proposal_model,
     admin_mod.mapping_model) = originals

    os.close(fd)
    os.unlink(db_path)


def _submit(client, ke_id="KE 1097", confidence_level="medium", **overrides):
    payload = {
        "ke_id": ke_id,
        "ke_title": "Occurrence, renal proximal tubular necrosis",
        "wp_id": "WP4313",
        "wp_title": "Ferroptosis",
        "connection_type": "causative",
        "confidence_level": confidence_level,
        "step1": "causative",
        "step2": "likely",
        "step3": "includes",
        "step4": "keysteps",
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return client.post("/submit", data=payload)


def _stored_confidence(pm, proposal_id):
    row = next(
        p for p in pm.get_all_proposals() if p["id"] == proposal_id
    )
    return row["new_pair_confidence_level"] or row.get("proposed_confidence")


# --- the scorer itself ------------------------------------------------------

def test_bonus_lifts_the_tier(assessment_config):
    """The worked example from the issue: 4.0 without the bonus, 5.0 with."""
    without = score_ke_pathway_assessment(
        "likely", "includes", "keysteps", None, assessment_config
    )
    with_bonus = score_ke_pathway_assessment(
        "likely", "includes", "keysteps", "Tissue", assessment_config
    )
    assert without == 4.0
    assert with_bonus == 5.0
    assert compute_ke_pathway_confidence(
        "likely", "includes", "keysteps", None, assessment_config
    ) == "medium"
    assert compute_ke_pathway_confidence(
        "likely", "includes", "keysteps", "Tissue", assessment_config
    ) == "high"


@pytest.mark.parametrize("biolevel", ["Molecular", "cellular", "TISSUE"])
def test_qualifying_levels_are_case_insensitive(biolevel, assessment_config):
    """The JS uses bioLevel.includes(level) on a lowercased string, so the
    port must match case-insensitively rather than by equality."""
    assert compute_ke_pathway_confidence(
        "likely", "includes", "keysteps", biolevel, assessment_config
    ) == "high"


def test_non_qualifying_level_gets_no_bonus(assessment_config):
    assert compute_ke_pathway_confidence(
        "likely", "includes", "keysteps", "Individual", assessment_config
    ) == "medium"


def test_unknown_answer_scores_zero_rather_than_raising(assessment_config):
    """Mirrors the `|| 0` in the JS — an answer outside the whitelist must
    not abort scoring."""
    assert score_ke_pathway_assessment(
        "banana", "includes", "keysteps", None, assessment_config
    ) == 2.0


def test_thresholds_are_inclusive(assessment_config):
    """5.0 is high, not medium; 2.5 is medium, not low."""
    from src.core.assessment_scoring import tier_for_score
    assert tier_for_score(5.0, assessment_config) == "high"
    assert tier_for_score(4.999, assessment_config) == "medium"
    assert tier_for_score(2.5, assessment_config) == "medium"
    assert tier_for_score(2.499, assessment_config) == "low"


# --- the fallback contract --------------------------------------------------

def test_incomplete_assessment_keeps_the_submitted_tier(assessment_config):
    """A legacy v1 submission answers none of the four questions. Scoring it
    would return 'low' for everything, so the submitted tier must stand."""
    assert recompute_confidence_level(
        ke_id="KE 1097", basis=None, specificity=None, coverage=None,
        submitted_level="high", ke_meta_index={"KE 1097": {"biolevel": "Tissue"}},
        config=assessment_config,
    ) == "high"


def test_unknown_ke_keeps_the_submitted_tier(assessment_config):
    """Scoring a KE the snapshot does not carry would drop the bonus and
    reproduce #237 on the server. Leave the submitted tier alone instead."""
    assert recompute_confidence_level(
        ke_id="KE 9999", basis="likely", specificity="includes",
        coverage="keysteps", submitted_level="high", ke_meta_index={},
        config=assessment_config,
    ) == "high"


def test_missing_index_keeps_the_submitted_tier(assessment_config):
    assert recompute_confidence_level(
        ke_id="KE 1097", basis="likely", specificity="includes",
        coverage="keysteps", submitted_level="high", ke_meta_index=None,
        config=assessment_config,
    ) == "high"


# --- end to end through /submit --------------------------------------------

def test_submit_upgrades_a_downgraded_tier(recompute_client):
    """The #237 failure mode: the browser lost the biological level and sent
    'medium'. The server holds the level and stores 'high'."""
    client, pm = recompute_client
    resp = _submit(client, confidence_level="medium")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    proposal_id = resp.get_json()["proposal_id"]
    assert _stored_confidence(pm, proposal_id) == "high"


def test_submit_does_not_invent_a_bonus_for_a_non_qualifying_ke(recompute_client):
    """The recompute must be able to correct downward too, not only upward."""
    client, pm = recompute_client
    resp = _submit(client, ke_id="KE 500", confidence_level="high")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    proposal_id = resp.get_json()["proposal_id"]
    assert _stored_confidence(pm, proposal_id) == "medium"


def test_submit_for_an_unknown_ke_stores_what_was_submitted(recompute_client):
    """KE 999 is not in the index — the snapshot lag case from #239."""
    client, pm = recompute_client
    resp = _submit(client, ke_id="KE 999", confidence_level="high")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    proposal_id = resp.get_json()["proposal_id"]
    assert _stored_confidence(pm, proposal_id) == "high"
