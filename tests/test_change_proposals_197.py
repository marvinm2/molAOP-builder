"""Issue #197: KE-GO and KE-Reactome "Propose Change"/deletion parity with WP.

Covers the three layers added for change/deletion proposals against existing
approved GO / Reactome mappings:

  * API   — /submit_go_proposal, /submit_reactome_proposal
  * Admin — approve delete + revision branches (mapping_id set)
  * Model — GoMappingModel.delete_mapping,
            ReactomeProposalModel.find_mapping_by_details,
            update_reactome_mapping(confidence_level=...)

Fixture patterns mirror tests/test_reactome_admin.py and
tests/test_reactome_submission.py (temp-file DB + model re-wiring + seeded
session).
"""
import json
import os
import tempfile

import pytest

from src.core.models import (
    Database,
    GoMappingModel,
    GoProposalModel,
    ReactomeMappingModel,
    ReactomeProposalModel,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _entry(**kwargs):
    """Return the JSON string the explore-page modal posts as `entry`."""
    return json.dumps(kwargs)


def _seed_go_mapping(gm, ke_id="KE 177", go_id="GO:0140053",
                     go_name="mitochondrial gene expression",
                     confidence_level="high", connection_type="causative"):
    return gm.create_mapping(
        ke_id=ke_id,
        ke_title="Mitochondrial dysfunction",
        go_id=go_id,
        go_name=go_name,
        connection_type=connection_type,
        confidence_level=confidence_level,
        created_by="github:curator",
    )


def _seed_reactome_mapping(rm, ke_id="KE 177", reactome_id="R-HSA-5357801",
                           pathway_name="Programmed Cell Death",
                           confidence_level="high"):
    return rm.create_mapping(
        ke_id=ke_id,
        ke_title="Mitochondrial dysfunction",
        reactome_id=reactome_id,
        pathway_name=pathway_name,
        confidence_level=confidence_level,
        created_by="github:curator",
    )


# ---------------------------------------------------------------------------
# API: /submit_go_proposal
# ---------------------------------------------------------------------------

class TestSubmitGoProposal:
    @pytest.fixture
    def client_models(self):
        from app import app as flask_app
        import src.blueprints.api as api_mod
        from src.core.models import CacheModel

        fd, db_path = tempfile.mkstemp()
        db = Database(db_path)
        gm = GoMappingModel(db)
        gpm = GoProposalModel(db)
        cm = CacheModel(db)

        orig = (api_mod.go_mapping_model, api_mod.go_proposal_model, api_mod.cache_model)
        api_mod.go_mapping_model = gm
        api_mod.go_proposal_model = gpm
        api_mod.cache_model = cm

        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        os.environ["ADMIN_USERS"] = "github:testuser"

        with flask_app.test_client() as c:
            with flask_app.app_context():
                with c.session_transaction() as sess:
                    sess["user"] = {"username": "github:curator",
                                    "email": "curator@example.com"}
                yield c, gm, gpm

        (api_mod.go_mapping_model, api_mod.go_proposal_model, api_mod.cache_model) = orig
        os.close(fd)
        os.unlink(db_path)

    def _base_form(self, entry, **extra):
        form = {
            "entry": entry,
            "userName": "Test Curator",
            "userEmail": "curator@example.com",
            "userAffiliation": "Maastricht University",
        }
        form.update(extra)
        return form

    def test_delete_proposal_created(self, client_models):
        c, gm, gpm = client_models
        mapping_id = _seed_go_mapping(gm)
        entry = _entry(ke_id="KE 177", ke_title="Mitochondrial dysfunction",
                       go_id="GO:0140053", go_name="mitochondrial gene expression")
        resp = c.post("/submit_go_proposal",
                      data=self._base_form(entry, deleteEntry="on"))
        assert resp.status_code == 200
        props = gpm.get_all_go_proposals()
        assert len(props) == 1
        assert props[0]["mapping_id"] == mapping_id
        assert props[0]["proposed_delete"] in (1, True)
        assert props[0]["ke_id"] == "KE 177"

    def test_confidence_change_proposal_created(self, client_models):
        """#245 replaced the bare confidence radio with GO's own instrument.

        A revision now scores the three dimensions and the tier is derived from
        them, so the tier is an outcome of stated reasoning rather than an
        assertion. 1/1/1 is the weakest assessment and yields 'low' — the value
        this test used to set directly.
        """
        c, gm, gpm = client_models
        _seed_go_mapping(gm)
        entry = _entry(ke_id="KE 177", go_id="GO:0140053")
        resp = c.post("/submit_go_proposal",
                      data=self._base_form(
                          entry, changeType="describes",
                          connection_score=1, specificity_score=1,
                          evidence_score=1,
                      ))
        assert resp.status_code == 200
        props = gpm.get_all_go_proposals()
        assert props[0]["proposed_confidence"] == "low"
        assert props[0]["proposed_connection_score"] == 1

    def test_unknown_mapping_returns_404(self, client_models):
        c, gm, gpm = client_models
        entry = _entry(ke_id="KE 999", go_id="GO:9999999")
        resp = c.post("/submit_go_proposal",
                      data=self._base_form(entry, deleteEntry="on"))
        assert resp.status_code == 404

    def test_no_changes_returns_400(self, client_models):
        c, gm, gpm = client_models
        _seed_go_mapping(gm)
        entry = _entry(ke_id="KE 177", go_id="GO:0140053")
        resp = c.post("/submit_go_proposal", data=self._base_form(entry))
        assert resp.status_code == 400

    def test_missing_go_id_rejected(self, client_models):
        c, gm, gpm = client_models
        entry = _entry(ke_id="KE 177")  # no go_id
        resp = c.post("/submit_go_proposal",
                      data=self._base_form(entry, deleteEntry="on"))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# API: /submit_reactome_proposal
# ---------------------------------------------------------------------------

class TestSubmitReactomeProposal:
    @pytest.fixture
    def client_models(self):
        from app import app as flask_app
        import src.blueprints.api as api_mod
        from src.core.models import CacheModel

        fd, db_path = tempfile.mkstemp()
        db = Database(db_path)
        rm = ReactomeMappingModel(db)
        rpm = ReactomeProposalModel(db)
        cm = CacheModel(db)

        orig = (api_mod.reactome_mapping_model, api_mod.reactome_proposal_model,
                api_mod.cache_model)
        api_mod.reactome_mapping_model = rm
        api_mod.reactome_proposal_model = rpm
        api_mod.cache_model = cm

        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        os.environ["ADMIN_USERS"] = "github:testuser"

        with flask_app.test_client() as c:
            with flask_app.app_context():
                with c.session_transaction() as sess:
                    sess["user"] = {"username": "github:curator",
                                    "email": "curator@example.com"}
                yield c, rm, rpm

        (api_mod.reactome_mapping_model, api_mod.reactome_proposal_model,
         api_mod.cache_model) = orig
        os.close(fd)
        os.unlink(db_path)

    def _base_form(self, entry, **extra):
        form = {
            "entry": entry,
            "userName": "Test Curator",
            "userEmail": "curator@example.com",
            "userAffiliation": "Maastricht University",
        }
        form.update(extra)
        return form

    def test_delete_proposal_created(self, client_models):
        c, rm, rpm = client_models
        mapping_id = _seed_reactome_mapping(rm)
        entry = _entry(ke_id="KE 177", ke_title="Mitochondrial dysfunction",
                       reactome_id="R-HSA-5357801",
                       pathway_name="Programmed Cell Death")
        resp = c.post("/submit_reactome_proposal",
                      data=self._base_form(entry, deleteEntry="on"))
        assert resp.status_code == 200
        props = rpm.get_all_proposals()
        assert len(props) == 1
        assert props[0]["mapping_id"] == mapping_id
        assert props[0]["proposed_delete"] in (1, True)
        assert props[0]["reactome_id"] == "R-HSA-5357801"

    def test_bare_confidence_no_longer_sets_a_tier(self, client_models):
        # #245 removed the direct confidence control from every resource. The
        # field is not merely ignored — with no assessment behind it the
        # submission is refused, so a client still posting the old shape fails
        # loudly instead of silently creating a proposal that asserts a tier
        # with no stated grounds.
        c, rm, rpm = client_models
        _seed_reactome_mapping(rm)
        entry = _entry(ke_id="KE 177", reactome_id="R-HSA-5357801")
        resp = c.post("/submit_reactome_proposal",
                      data=self._base_form(entry, changeConfidence="medium"))
        assert resp.status_code == 400
        assert "four assessment questions" in resp.get_json()["error"]
        assert rpm.get_all_proposals() == []

    def test_unknown_mapping_returns_404(self, client_models):
        c, rm, rpm = client_models
        entry = _entry(ke_id="KE 999", reactome_id="R-HSA-0000000")
        resp = c.post("/submit_reactome_proposal",
                      data=self._base_form(entry, deleteEntry="on"))
        assert resp.status_code == 404

    def test_no_changes_returns_400(self, client_models):
        c, rm, rpm = client_models
        _seed_reactome_mapping(rm)
        entry = _entry(ke_id="KE 177", reactome_id="R-HSA-5357801")
        resp = c.post("/submit_reactome_proposal", data=self._base_form(entry))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin approve: GO delete + revision branches
# ---------------------------------------------------------------------------

class TestApproveGoChangeProposal:
    @pytest.fixture
    def admin(self):
        os.environ["ADMIN_USERS"] = "github:testadmin"
        from app import app as flask_app
        import src.blueprints.admin as admin_mod

        fd, db_path = tempfile.mkstemp()
        db = Database(db_path)
        gm = GoMappingModel(db)
        gpm = GoProposalModel(db)

        orig = (admin_mod.go_mapping_model, admin_mod.go_proposal_model)
        admin_mod.go_mapping_model = gm
        admin_mod.go_proposal_model = gpm

        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False

        with flask_app.test_client() as c:
            with flask_app.app_context():
                with c.session_transaction() as sess:
                    sess["user"] = {"username": "github:testadmin",
                                    "email": "admin@example.com"}
                yield c, gm, gpm, db

        (admin_mod.go_mapping_model, admin_mod.go_proposal_model) = orig
        os.close(fd)
        os.unlink(db_path)

    def test_approve_deletion_removes_mapping(self, admin):
        c, gm, gpm, db = admin
        mapping_id = _seed_go_mapping(gm)
        pid = gpm.create_proposal(
            mapping_id=mapping_id, user_name="C", user_email="c@example.com",
            user_affiliation="MU", provider_username="github:curator",
            proposed_delete=True, ke_id="KE 177", go_id="GO:0140053",
        )
        resp = c.post(f"/admin/go-proposals/{pid}/approve",
                      data={"admin_notes": "wrong process"})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "deleted"
        assert gpm.find_mapping_by_details("KE 177", "GO:0140053") is None
        assert gpm.get_go_proposal_by_id(pid)["status"] == "approved"

    def test_approve_revision_updates_mapping(self, admin):
        c, gm, gpm, db = admin
        mapping_id = _seed_go_mapping(gm, confidence_level="high",
                                      connection_type="causative")
        pid = gpm.create_proposal(
            mapping_id=mapping_id, user_name="C", user_email="c@example.com",
            user_affiliation="MU", provider_username="github:curator",
            proposed_confidence="low", proposed_connection_type="responsive",
            ke_id="KE 177", go_id="GO:0140053",
        )
        resp = c.post(f"/admin/go-proposals/{pid}/approve", data={"admin_notes": ""})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "updated"
        row = db.get_connection().execute(
            "SELECT confidence_level, connection_type FROM ke_go_mappings WHERE id = ?",
            (mapping_id,),
        ).fetchone()
        assert row["confidence_level"] == "low"
        assert row["connection_type"] == "responsive"
        assert gpm.get_go_proposal_by_id(pid)["status"] == "approved"


# ---------------------------------------------------------------------------
# Admin approve: Reactome delete + revision branches
# ---------------------------------------------------------------------------

class TestApproveReactomeChangeProposal:
    @pytest.fixture
    def admin(self):
        os.environ["ADMIN_USERS"] = "github:testadmin"
        from app import app as flask_app
        import src.blueprints.admin as admin_mod

        fd, db_path = tempfile.mkstemp()
        db = Database(db_path)
        rm = ReactomeMappingModel(db)
        rpm = ReactomeProposalModel(db)

        orig = (admin_mod.reactome_mapping_model, admin_mod.reactome_proposal_model)
        admin_mod.reactome_mapping_model = rm
        admin_mod.reactome_proposal_model = rpm

        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False

        with flask_app.test_client() as c:
            with flask_app.app_context():
                with c.session_transaction() as sess:
                    sess["user"] = {"username": "github:testadmin",
                                    "email": "admin@example.com"}
                yield c, rm, rpm, db

        (admin_mod.reactome_mapping_model, admin_mod.reactome_proposal_model) = orig
        os.close(fd)
        os.unlink(db_path)

    def test_approve_deletion_removes_mapping(self, admin):
        c, rm, rpm, db = admin
        mapping_id = _seed_reactome_mapping(rm)
        pid = rpm.create_proposal(
            mapping_id=mapping_id, user_name="C", user_email="c@example.com",
            user_affiliation="MU", provider_username="github:curator",
            proposed_delete=True, ke_id="KE 177", reactome_id="R-HSA-5357801",
        )
        resp = c.post(f"/admin/reactome-proposals/{pid}/approve",
                      data={"admin_notes": "umbrella retired"})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "deleted"
        assert rpm.find_mapping_by_details("KE 177", "R-HSA-5357801") is None
        assert rpm.get_proposal_by_id(pid)["status"] == "approved"

    def test_approve_nondelete_change_applies_the_revision(self, admin):
        # #245 reversed the deletion-only half of D-02, so a revision against an
        # existing Reactome mapping is now applied rather than rejected. This
        # proposal is the pre-#245 shape — a tier with no answers behind it —
        # which still applies, but must not claim a 'v2' assessment it cannot
        # show.
        c, rm, rpm, db = admin
        mapping_id = _seed_reactome_mapping(rm, confidence_level="high")
        pid = rpm.create_proposal(
            mapping_id=mapping_id, user_name="C", user_email="c@example.com",
            user_affiliation="MU", provider_username="github:curator",
            proposed_confidence="low", ke_id="KE 177", reactome_id="R-HSA-5357801",
        )
        resp = c.post(f"/admin/reactome-proposals/{pid}/approve", data={"admin_notes": ""})
        assert resp.status_code == 200
        assert resp.get_json()["action"] == "updated"
        row = db.get_connection().execute(
            "SELECT confidence_level, assessment_version "
            "FROM ke_reactome_mappings WHERE id = ?",
            (mapping_id,),
        ).fetchone()
        assert row["confidence_level"] == "low"
        assert row["assessment_version"] == "v1"
        assert rpm.get_proposal_by_id(pid)["status"] == "approved"
