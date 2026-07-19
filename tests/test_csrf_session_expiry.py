"""
Tests for the CSRF error handler's session-expiry disambiguation (#195).

A session-bound CSRF token becomes invalid the instant the login session expires,
so a CSRF failure with no logged-in user is really an expired session. The handler
must surface that as HTTP 401 {"error": "session_expired"} for AJAX requests, so the
mapper client can prompt re-login instead of showing a generic failure — while a
genuine CSRF mismatch on a live session still returns 400.
"""
import os
import tempfile

import pytest

from app import app
from src.core.models import Database


@pytest.fixture
def csrf_client():
    """Test client with CSRF protection ENABLED (opposite of the default fixture)."""
    db_fd, db_path = tempfile.mkstemp()
    os.environ["DATABASE_PATH"] = db_path
    app.config["TESTING"] = True
    app.config["DATABASE_PATH"] = db_path
    app.config["WTF_CSRF_ENABLED"] = True  # <-- enable for these tests
    try:
        with app.test_client() as client:
            with app.app_context():
                Database(db_path).init_db()
                yield client
    finally:
        app.config["WTF_CSRF_ENABLED"] = False  # restore shared app state
        os.close(db_fd)
        os.unlink(db_path)


AJAX = {"X-Requested-With": "XMLHttpRequest"}


def test_csrf_failure_without_session_is_session_expired_401(csrf_client):
    """No session user + missing CSRF token => 401 session_expired (AJAX)."""
    resp = csrf_client.post(
        "/submit_reactome_mapping",
        data={"ke_id": "123", "reactome_id": "R-HSA-109581"},
        headers=AJAX,
    )
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "session_expired"}


def test_csrf_failure_with_live_session_is_plain_csrf_400(csrf_client):
    """Logged-in user + missing CSRF token => genuine CSRF mismatch, 400 (AJAX)."""
    with csrf_client.session_transaction() as sess:
        sess["user"] = {"username": "testuser", "email": "test@example.com"}
    resp = csrf_client.post(
        "/submit_reactome_mapping",
        data={"ke_id": "123", "reactome_id": "R-HSA-109581"},
        headers=AJAX,
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "CSRF token missing or invalid"}


def test_csrf_failure_non_ajax_renders_html_not_json(csrf_client):
    """A non-AJAX (full-page) form post still gets an HTML error page, not JSON."""
    resp = csrf_client.post(
        "/submit_reactome_mapping",
        data={"ke_id": "123", "reactome_id": "R-HSA-109581"},
    )
    # session-expired branch => 401, HTML (no JSON body)
    assert resp.status_code == 401
    assert resp.get_json(silent=True) is None
    assert b"session has expired" in resp.data.lower()
