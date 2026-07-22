"""
Tests for Phase 38-03: shared admin_proposals.js IIFE and template integration.

Covers:
- test_static_js_served: GET /static/js/admin_proposals.js returns 200 and
  body contains 'AdminProposals'
- test_templates_load_shared_js: static-file grep asserting all three templates
  contain the <script src=...admin_proposals.js> tag and AdminProposals.init,
  and that none retain inline 'function escapeHtml'
"""
import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Fixture: basic app test client (uses reactome fixture pattern from
# test_admin_bulk_approve.py — ADMIN_USERS set so /static/* is accessible)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client():
    """Minimal Flask test client without model injection, just for serving statics."""
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        with flask_app.app_context():
            yield client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminProposalsJs:
    def test_static_js_served(self, app_client):
        """GET /static/js/admin_proposals.js returns 200 and contains the IIFE."""
        response = app_client.get("/static/js/admin_proposals.js")
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}"
        )
        body = response.data.decode("utf-8")
        assert "AdminProposals" in body, "Response body missing 'AdminProposals'"
        assert "var AdminProposals = (function" in body, (
            "Response body missing IIFE var declaration"
        )

    def test_templates_load_shared_js(self):
        """All three admin templates load the shared IIFE and dropped inline handlers."""
        templates = [
            "admin_proposals",
            "admin_go_proposals",
            "admin_reactome_proposals",
        ]
        # Locate templates directory relative to this test file
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        templates_dir = os.path.join(project_root, "templates")

        for tpl_name in templates:
            path = os.path.join(templates_dir, f"{tpl_name}.html")
            assert os.path.exists(path), f"Template not found: {path}"
            content = open(path, encoding="utf-8").read()

            # Each template must load the shared JS via <script src>
            assert "js/admin_proposals.js" in content, (
                f"{tpl_name}: missing <script src=.../admin_proposals.js>"
            )

            # Each template must call AdminProposals.init(
            assert "AdminProposals.init" in content, (
                f"{tpl_name}: missing AdminProposals.init call"
            )

            # No inline function escapeHtml (moved to the IIFE)
            assert "function escapeHtml" not in content, (
                f"{tpl_name}: inline 'function escapeHtml' not removed"
            )

            # No inline stepLabels definition (moved to the IIFE)
            # Only check for the inline const/let/var stepLabels pattern
            import re
            inline_steplabels = re.search(
                r'\b(const|let|var)\s+stepLabels\s*=', content
            )
            assert inline_steplabels is None, (
                f"{tpl_name}: inline stepLabels definition not removed"
            )

    def test_templates_have_selectall(self):
        """All three templates contain id='selectAll' for the header checkbox."""
        templates = [
            "admin_proposals",
            "admin_go_proposals",
            "admin_reactome_proposals",
        ]
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        templates_dir = os.path.join(project_root, "templates")

        for tpl_name in templates:
            path = os.path.join(templates_dir, f"{tpl_name}.html")
            content = open(path, encoding="utf-8").read()
            assert 'id="selectAll"' in content, (
                f"{tpl_name}: missing id='selectAll' header checkbox"
            )

    def test_templates_have_review_panel(self):
        """All three templates contain the docked side panel div."""
        templates = [
            "admin_proposals",
            "admin_go_proposals",
            "admin_reactome_proposals",
        ]
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        templates_dir = os.path.join(project_root, "templates")

        for tpl_name in templates:
            path = os.path.join(templates_dir, f"{tpl_name}.html")
            content = open(path, encoding="utf-8").read()
            assert 'id="reviewPanel"' in content, (
                f"{tpl_name}: missing id='reviewPanel' side panel"
            )
            assert 'id="reviewPanelContent"' in content, (
                f"{tpl_name}: missing id='reviewPanelContent'"
            )

    def test_templates_have_cheatsheet_modal(self):
        """All three templates contain the cheat-sheet modal."""
        templates = [
            "admin_proposals",
            "admin_go_proposals",
            "admin_reactome_proposals",
        ]
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        templates_dir = os.path.join(project_root, "templates")

        for tpl_name in templates:
            path = os.path.join(templates_dir, f"{tpl_name}.html")
            content = open(path, encoding="utf-8").read()
            assert "cheatSheetModal" in content, (
                f"{tpl_name}: missing cheatSheetModal"
            )
            assert "cheatSheetOverlay" in content, (
                f"{tpl_name}: missing cheatSheetOverlay"
            )

    def test_old_modal_removed(self):
        """Old per-proposal details modals are removed from all three templates."""
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        templates_dir = os.path.join(project_root, "templates")

        for tpl_name in [
            "admin_proposals",
            "admin_go_proposals",
            "admin_reactome_proposals",
        ]:
            path = os.path.join(templates_dir, f"{tpl_name}.html")
            content = open(path, encoding="utf-8").read()
            # Old modal IDs should be gone
            for old_id in [
                '"proposalModal"',
                '"goProposalModal"',
                '"reactomeProposalModal"',
            ]:
                assert old_id not in content, (
                    f"{tpl_name}: old modal id {old_id} still present"
                )

    def test_templates_have_grid_layout(self):
        """All three templates contain the two-column CSS grid layout."""
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        templates_dir = os.path.join(project_root, "templates")

        for tpl_name in [
            "admin_proposals",
            "admin_go_proposals",
            "admin_reactome_proposals",
        ]:
            path = os.path.join(templates_dir, f"{tpl_name}.html")
            content = open(path, encoding="utf-8").read()
            assert "grid-template-columns" in content, (
                f"{tpl_name}: missing grid-template-columns two-column layout"
            )

    def test_go_assessment_rendered_in_panel(self):
        """Issue #213: the shared review panel must render GO's three-dimension
        assessment, not just the WP four-answer one.

        The panel used to test only proposed_relationship/_basis/_specificity/
        _coverage — columns that exist on `proposals` but not on
        `ke_go_proposals` — so every GO proposal fell through to the
        "legacy proposal" branch even when its three answers were stored.
        """
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        js_path = os.path.join(project_root, "static", "js", "admin_proposals.js")
        content = open(js_path, encoding="utf-8").read()

        for field in [
            "proposed_connection_score",
            "proposed_specificity_score",
            "proposed_evidence_score",
        ]:
            assert field in content, (
                f"admin_proposals.js: GO assessment field {field} never read — "
                "GO proposals will render as 'No assessment submitted'"
            )

        # The GO branch must be evaluated before the legacy fallback.
        assert "hasGoAssessment" in content, (
            "admin_proposals.js: no GO-specific assessment branch"
        )
        # Anchor on the branch statement and the fallback's rendered markup so
        # the explanatory comments above them do not satisfy the assertion.
        go_branch = content.index("if (hasGoAssessment)")
        legacy = content.index("No assessment submitted (legacy proposal)</div>")
        assert go_branch < legacy, (
            "admin_proposals.js: GO assessment branch must precede the "
            "legacy-proposal fallback"
        )

    def test_go_template_emits_go_assessment_attrs(self):
        """Issue #213: the GO queue's rows must carry GO's assessment columns.

        The row attributes feed _rowToProposal, which backs the panel's fast
        path. admin_go_proposals.html previously copied the WP four-answer
        attributes verbatim; those reference columns ke_go_proposals does not
        have, so they rendered as empty strings on every row.
        """
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(tests_dir)
        path = os.path.join(project_root, "templates", "admin_go_proposals.html")
        content = open(path, encoding="utf-8").read()

        # Match on `attr=` so prose in comments cannot satisfy the assertion.
        for attr in [
            "data-proposed-connection-score=",
            "data-proposed-specificity-score=",
            "data-proposed-evidence-score=",
        ]:
            assert attr in content, (
                f"admin_go_proposals.html: missing {attr}"
            )

        # The WP-shaped attributes are structurally always empty here.
        for attr in [
            "data-proposed-relationship=",
            "data-proposed-basis=",
            "data-proposed-coverage=",
        ]:
            assert attr not in content, (
                f"admin_go_proposals.html: {attr} references a column "
                "ke_go_proposals does not have"
            )
