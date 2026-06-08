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
