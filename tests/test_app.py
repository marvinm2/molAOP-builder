"""
Tests for Flask application routes
"""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


class TestRoutes:
    def test_index_route(self, client):
        """Test the index route"""
        response = client.get("/")
        assert response.status_code == 200
        assert b"Molecular AOP Builder" in response.data

    def test_explore_route(self, client):
        """Test the explore route"""
        response = client.get("/explore")
        assert response.status_code == 200
        assert b"Explore" in response.data

    def test_login_redirect(self, client):
        """Test login route redirects (multi-provider; Phase 14)."""
        # Phase 14 moved /login → /login/<provider>. Hitting /login/github
        # must return 302 — either to github.com/login/oauth/authorize when
        # the OAuth client is wired, or to main.index when it isn't.
        # Either outcome proves the route exists and dispatched.
        response = client.get("/login/github")
        assert response.status_code == 302
        location = response.headers.get("Location", "")
        assert (
            "github.com/login/oauth/authorize" in location
            or location.endswith("/")
            or location.endswith("/index")
        ), f"Unexpected Location after /login/github redirect: {location!r}"

    def test_logout(self, auth_client):
        """Test logout functionality"""
        response = auth_client.get("/logout")
        assert response.status_code == 302
        # Should redirect to index


class TestMappingAPI:
    def test_check_entry_missing_params(self, client):
        """Test check entry with missing parameters"""
        response = client.post("/check", data={})
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_submit_requires_auth(self, client):
        """Test that submit requires authentication"""
        response = client.post("/submit", data={})
        assert response.status_code == 401
        data = json.loads(response.data)
        assert "error" in data

    def test_submit_missing_params(self, auth_client):
        """Test submit with missing parameters"""
        response = auth_client.post("/submit", data={})
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_submit_invalid_confidence(self, auth_client):
        """Test submit with invalid confidence level"""
        response = auth_client.post(
            "/submit",
            data={
                "ke_id": "KE:1",
                "wp_id": "WP:1",
                "ke_title": "Test KE",
                "wp_title": "Test WP",
                "confidence_level": "invalid",
                "connection_type": "causative",
            },
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_submit_invalid_connection_type(self, auth_client):
        """Test submit with invalid connection type"""
        response = auth_client.post(
            "/submit",
            data={
                "ke_id": "KE:1",
                "wp_id": "WP:1",
                "ke_title": "Test KE",
                "wp_title": "Test WP",
                "confidence_level": "high",
                "connection_type": "invalid",
            },
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data


class TestSPARQLEndpoints:
    @patch("src.blueprints.api.ke_metadata", [
        {
            "KEtitle": "Test KE Title",
            "KElabel": "KE:1",
            "KEpage": "http://example.com",
        }
    ])
    def test_get_ke_options_success(self, client):
        """Test successful KE options retrieval from pre-computed metadata"""
        response = client.get("/get_ke_options")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert len(data) == 1
        assert data[0]["KEtitle"] == "Test KE Title"

    @patch("src.blueprints.api.pathway_metadata", [
        {
            "pathwayID": "WP:1",
            "pathwayTitle": "Test Pathway",
            "pathwayLink": "http://example.com",
        }
    ])
    def test_get_pathway_options_success(self, client):
        """Test successful pathway options retrieval from pre-computed metadata"""
        response = client.get("/get_pathway_options")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert len(data) == 1
        assert data[0]["pathwayTitle"] == "Test Pathway"

    @patch("requests.post")
    def test_sparql_timeout(self, mock_post, client):
        """Test SPARQL timeout handling"""
        mock_post.side_effect = Exception("Timeout")

        response = client.get("/get_ke_options")
        # Endpoint may return cached data (200) or error (500)
        assert response.status_code in [200, 500]


class TestRateLimiting:
    def test_rate_limiting(self, client):
        """Test that rate limiting works"""
        # This test would need to make many requests quickly
        # For now, just test that the endpoint responds normally
        response = client.get("/get_ke_options")
        assert response.status_code in [
            200,
            500,
            503,
        ]  # Could be 500 due to mocked SPARQL


class TestAuthentication:
    def test_login_required_submit(self, client):
        """Test that submit requires login (returns 401)"""
        response = client.post("/submit", data={"ke_id": "KE:1"})
        assert response.status_code == 401

    def test_login_required_submit_proposal(self, client):
        """Test that submit_proposal requires login (returns 401)"""
        response = client.post(
            "/submit_proposal",
            data={
                "entry": "test",
                "userName": "Test",
                "userEmail": "test@example.com",
                "userAffiliation": "Test Org",
            },
        )
        assert response.status_code == 401

    def test_authenticated_submit_proposal(self, auth_client):
        """Test submit proposal with authentication but missing mapping_id"""
        response = auth_client.post(
            "/submit_proposal",
            data={
                "entry": "test",
                "userName": "Test User",
                "userEmail": "test@example.com",
                "userAffiliation": "Test Org",
            },
        )
        # Returns 400 because mapping_id is required but missing
        assert response.status_code == 400


class TestKEContext:
    @patch("src.blueprints.api.cache_model")
    @patch("src.blueprints.api.mapping_model")
    @patch("src.blueprints.api.go_mapping_model")
    @patch("requests.post")
    def test_ke_context_returns_json_structure(self, mock_post, mock_go_model, mock_mapping, mock_cache, client):
        """Test KE context endpoint returns expected JSON structure"""
        # Mock SPARQL response for AOP membership
        mock_sparql_response = MagicMock()
        mock_sparql_response.status_code = 200
        mock_sparql_response.json.return_value = {
            "results": {
                "bindings": [
                    {"aopId": {"value": "AOP 1"}, "aopTitle": {"value": "Test AOP"}}
                ]
            }
        }
        mock_post.return_value = mock_sparql_response

        # Mock database models
        mock_cache.get_cached_response.return_value = None
        mock_mapping.get_mappings_by_ke.return_value = [
            {"wp_id": "WP100", "wp_title": "Test Pathway", "confidence_level": "high"}
        ]
        mock_go_model.get_mappings_by_ke.return_value = []

        response = client.get("/api/ke_context/KE%2055")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "ke_id" in data
        assert "aop_membership" in data
        assert "wp_mappings" in data
        assert "go_mappings" in data
        assert "summary" in data
        assert data["summary"]["aop_count"] == 1
        assert data["summary"]["wp_mapping_count"] == 1
        assert data["summary"]["go_mapping_count"] == 0

    def test_ke_context_empty_id(self, client):
        """Test KE context with empty ID returns 400"""
        response = client.get("/api/ke_context/%20")
        assert response.status_code == 400

    @patch("src.blueprints.api.cache_model")
    @patch("src.blueprints.api.mapping_model")
    @patch("src.blueprints.api.go_mapping_model")
    @patch("requests.post")
    def test_ke_context_no_results(self, mock_post, mock_go_model, mock_mapping, mock_cache, client):
        """Test KE context with no matching data"""
        mock_sparql_response = MagicMock()
        mock_sparql_response.status_code = 200
        mock_sparql_response.json.return_value = {"results": {"bindings": []}}
        mock_post.return_value = mock_sparql_response

        mock_cache.get_cached_response.return_value = None
        mock_mapping.get_mappings_by_ke.return_value = []
        mock_go_model.get_mappings_by_ke.return_value = []

        response = client.get("/api/ke_context/KE%20999")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["summary"]["aop_count"] == 0
        assert data["summary"]["wp_mapping_count"] == 0
        assert data["summary"]["go_mapping_count"] == 0


class TestGuestAuth:
    def test_guest_login_page_renders(self, client):
        """Test guest login affordance is reachable and renders correctly.

        Post-Phase-14, GET /guest-login redirects to the index page where
        the login modal uses different copy ("Sign in" / "Workshop code"),
        so a GET+follow_redirects no longer carries the original assertion
        strings. The standalone templates/guest_login.html — which still
        carries verbatim 'Workshop Login' + 'access code' — is now reached
        via the form-error render path (POST without a valid code). That is
        the live code path that serves the workshop login affordance page,
        so the test exercises it directly. Diagnostic: GET-follow-redirect
        landed on / (200) but body lacked both strings; POST with empty code
        renders guest_login.html and carries both verbatim. Both original
        assertions are preserved without weakening.
        """
        response = client.post("/guest-login", data={"code": ""})
        assert response.status_code == 200
        assert b"Workshop Login" in response.data
        assert b"access code" in response.data

    def test_guest_login_invalid_code(self, client):
        """Test guest login with invalid code shows error"""
        response = client.post("/guest-login", data={"code": "invalid-code"})
        assert response.status_code == 200
        assert b"Invalid" in response.data or b"expired" in response.data

    def test_guest_login_redirects_if_logged_in(self, auth_client):
        """Test guest login redirects if already authenticated"""
        response = auth_client.get("/guest-login")
        assert response.status_code == 302

    def test_guest_can_submit_mapping(self, guest_client):
        """Test that a guest user can submit a mapping.

        This asserted only `!= 401` and so passed throughout #266, when a guest's
        every submission failed with a 500 from the identity trigger. Asserting
        that it is not one particular failure is not asserting that it worked.

        This fixture's blueprint models are bound to `:memory:` and cannot reach
        the proposals table, so the strongest claim available here is that the
        guest identity is accepted. The end-to-end proof is
        test_guest_proposal_submission_266.py, which runs against a real file.
        """
        response = guest_client.post(
            "/submit",
            data={
                "ke_id": "KE 123",
                "ke_title": "Test Key Event",
                "wp_id": "WP1234",
                "wp_title": "Test Pathway",
                "confidence_level": "low",
                "connection_type": "undefined",
            },
        )
        assert response.status_code != 401, response.get_data(as_text=True)

    def test_guest_cannot_access_admin(self, guest_client):
        """Test that guest users cannot access admin routes"""
        response = guest_client.get("/admin/proposals")
        assert response.status_code == 403


class TestSubmitCreatesProposal:
    """Verify /submit creates a pending proposal, not a direct mapping."""

    @pytest.fixture
    def submit_client(self):
        """
        Test client that re-wires the api blueprint to a fresh temp-file DB so
        proposal_model.create_new_pair_proposal() can persist rows. The
        default TestingConfig uses :memory: which creates a separate DB per
        sqlite3.connect() call, causing 'no such table: proposals' at runtime.
        """
        from app import app as flask_app
        import src.blueprints.api as api_mod
        from src.core.models import Database, MappingModel, ProposalModel, CacheModel

        fd, db_path = tempfile.mkstemp()
        db = Database(db_path)
        mm = MappingModel(db)
        pm = ProposalModel(db)
        cm = CacheModel(db)

        # Save originals
        orig_pm = api_mod.proposal_model
        orig_mm = api_mod.mapping_model
        orig_cm = api_mod.cache_model

        # Inject fresh models (keep other models as-is via keyword args)
        api_mod.proposal_model = pm
        api_mod.mapping_model = mm
        api_mod.cache_model = cm

        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        os.environ["ADMIN_USERS"] = "testuser"

        with flask_app.test_client() as test_client:
            with flask_app.app_context():
                with test_client.session_transaction() as sess:
                    sess["user"] = {"username": "github:testuser", "email": "test@example.com"}
                yield test_client

        # Restore originals
        api_mod.proposal_model = orig_pm
        api_mod.mapping_model = orig_mm
        api_mod.cache_model = orig_cm

        os.close(fd)
        os.unlink(db_path)

    def test_submit_creates_proposal_not_mapping(self, submit_client):
        """POST /submit returns 200 with proposal_id; no mapping in DB."""
        response = submit_client.post(
            "/submit",
            data={
                "ke_id": "KE 999",
                "ke_title": "Test KE",
                "wp_id": "WP999",
                "wp_title": "Test Pathway",
                "connection_type": "causative",
                "confidence_level": "high",
                "suggestion_score": "0.75",
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "proposal_id" in data
        assert data["proposal_id"] is not None

    def test_submit_response_mentions_pending_review(self, submit_client):
        """Response message indicates pending admin review."""
        response = submit_client.post(
            "/submit",
            data={
                "ke_id": "KE 998",
                "ke_title": "Test KE 2",
                "wp_id": "WP998",
                "wp_title": "Test Pathway 2",
                "connection_type": "causative",
                "confidence_level": "medium",
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "pending" in data.get("message", "").lower() or "proposal" in data.get("message", "").lower()


class TestSubmitGoCreatesProposal:
    """Verify /submit_go_mapping creates a pending proposal, not a direct mapping."""

    @pytest.fixture
    def submit_go_client(self):
        """
        Test client that re-wires the api blueprint to a fresh temp-file DB so
        go_proposal_model.create_new_pair_go_proposal() can persist rows. The
        default TestingConfig uses :memory: which creates a separate DB per
        sqlite3.connect() call, causing 'no such table' at runtime.
        """
        from app import app as flask_app
        import src.blueprints.api as api_mod
        from src.core.models import Database, GoMappingModel, GoProposalModel, CacheModel

        fd, db_path = tempfile.mkstemp()
        db = Database(db_path)
        gm = GoMappingModel(db)
        gpm = GoProposalModel(db)
        cm = CacheModel(db)

        # Save originals
        orig_gm = api_mod.go_mapping_model
        orig_gpm = api_mod.go_proposal_model
        orig_cm = api_mod.cache_model

        # Inject fresh models
        api_mod.go_mapping_model = gm
        api_mod.go_proposal_model = gpm
        api_mod.cache_model = cm

        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        os.environ["ADMIN_USERS"] = "testuser"

        with flask_app.test_client() as test_client:
            with flask_app.app_context():
                with test_client.session_transaction() as sess:
                    sess["user"] = {"username": "github:testuser", "email": "test@example.com"}
                yield test_client

        # Restore originals
        api_mod.go_mapping_model = orig_gm
        api_mod.go_proposal_model = orig_gpm
        api_mod.cache_model = orig_cm

        os.close(fd)
        os.unlink(db_path)

    def test_submit_go_creates_proposal_not_mapping(self, submit_go_client):
        """POST /submit_go_mapping returns 200 with proposal_id; no live mapping in DB."""
        response = submit_go_client.post(
            "/submit_go_mapping",
            data={
                "ke_id": "KE 999",
                "ke_title": "Test KE for GO",
                "go_id": "GO:0099999",
                "go_name": "test biological process",
                "connection_type": "describes",
                "confidence_level": "high",
                "suggestion_score": "0.80",
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "proposal_id" in data, f"Expected proposal_id in response, got: {data}"
        assert data["proposal_id"] is not None

    def test_submit_go_response_mentions_pending_review(self, submit_go_client):
        """Response message indicates pending admin review, not immediate creation."""
        response = submit_go_client.post(
            "/submit_go_mapping",
            data={
                "ke_id": "KE 998",
                "ke_title": "Test KE 2 for GO",
                "go_id": "GO:0098888",
                "go_name": "another test process",
                "connection_type": "involves",
                "confidence_level": "medium",
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        msg = data.get("message", "").lower()
        assert "pending" in msg or "proposal" in msg, (
            f"Expected 'pending' or 'proposal' in message, got: {data.get('message')}"
        )

    def test_submit_go_no_live_mapping_created(self, submit_go_client):
        """After submit, ke_go_mappings table must remain empty (proposal only)."""
        import src.blueprints.api as api_mod
        response = submit_go_client.post(
            "/submit_go_mapping",
            data={
                "ke_id": "KE 997",
                "ke_title": "Test KE 3 for GO",
                "go_id": "GO:0097777",
                "go_name": "third test process",
                "connection_type": "related",
                "confidence_level": "low",
            },
        )
        assert response.status_code == 200
        # Verify no row was inserted into ke_go_mappings
        conn = api_mod.go_mapping_model.db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM ke_go_mappings").fetchone()[0]
        conn.close()
        assert count == 0, f"Expected 0 live mappings after submit, found {count}"
