"""
Test configuration and fixtures
"""
import os
import tempfile

import pytest

# Set testing environment before importing app so module-level create_app()
# uses TestingConfig (DATABASE_PATH=':memory:') rather than the default
# production path (/app/data/ke_wp_mapping.db).
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
os.environ.setdefault("GITHUB_CLIENT_ID", "dummy")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "dummy")

from app import app
from src.core.models import Database


@pytest.fixture
def client():
    """Create a test client"""
    # Create a temporary database
    db_fd, db_path = tempfile.mkstemp()

    # Set up environment variables for testing
    os.environ["FLASK_SECRET_KEY"] = "test-secret-key"
    os.environ["GITHUB_CLIENT_ID"] = "dummy"
    os.environ["GITHUB_CLIENT_SECRET"] = "dummy"
    os.environ["ADMIN_USERS"] = "testuser"
    os.environ["DATABASE_PATH"] = db_path

    # Configure app for testing
    app.config["TESTING"] = True
    app.config["DATABASE_PATH"] = db_path
    app.config["WTF_CSRF_ENABLED"] = False  # Disable CSRF for testing

    with app.test_client() as client:
        with app.app_context():
            # Initialize test database
            test_db = Database(db_path)
            # Initialize database tables
            test_db.init_db()
            yield client

    # Cleanup
    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture
def auth_client(client):
    """Create an authenticated test client"""
    with client.session_transaction() as sess:
        sess["user"] = {"username": "testuser", "email": "test@example.com"}
    return client


@pytest.fixture
def guest_client(client):
    """Create a guest-authenticated test client"""
    with client.session_transaction() as sess:
        # Matches what /guest-login issues: a provider-style "guest:" prefix,
        # which the identity trigger requires, and no email — the old
        # "guest-<label>" / "workshop-guest" pair was an identity the database
        # rejected and the email field could never accept (#266).
        sess["user"] = {
            "username": "guest:test-participant",
            "email": "",
            "is_guest": True,
        }
    return client
