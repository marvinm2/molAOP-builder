"""
Tests for how long a login lasts, and for landing somewhere useful when it ends.

Three things were wrong together:

1. Nothing set ``session.permanent``, so Flask never applied
   ``PERMANENT_SESSION_LIFETIME`` at all. Both the 1-hour base value and the
   30-minute production override were inert, and the cookie simply lived until the
   browser closed.
2. What actually ended a working session was the session-bound CSRF token, capped
   independently at 1 hour. So the real login length was a number stated nowhere.
3. When it expired, ``static/js/main.js`` redirected to ``/auth/login`` — a route
   that did not exist, so the user landed on "The requested page was not found".

The lifetime is now a single configured value governing both, applied as an idle
timeout, and ``/auth/login`` resolves.
"""
import importlib
from datetime import timedelta

import pytest


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _reload_config():
    import src.core.config as cfg
    return importlib.reload(cfg)


def test_default_lifetime_is_two_hours(monkeypatch):
    monkeypatch.delenv("SESSION_LIFETIME_HOURS", raising=False)
    cfg = _reload_config()
    assert cfg.Config.PERMANENT_SESSION_LIFETIME == timedelta(hours=2)
    assert cfg.Config.WTF_CSRF_TIME_LIMIT == 7200


def test_session_and_csrf_lifetimes_cannot_diverge(monkeypatch):
    """The CSRF token is session-bound, so a shorter limit silently caps the login."""
    monkeypatch.setenv("SESSION_LIFETIME_HOURS", "4")
    cfg = _reload_config()
    try:
        assert cfg.Config.PERMANENT_SESSION_LIFETIME == timedelta(hours=4)
        assert cfg.Config.WTF_CSRF_TIME_LIMIT == 4 * 3600
    finally:
        monkeypatch.delenv("SESSION_LIFETIME_HOURS", raising=False)
        _reload_config()


def test_production_no_longer_overrides_the_lifetime(monkeypatch):
    """The old 30-minute production override was inert; it must not be reinstated
    as a real 30-minute cap now that the lifetime actually applies."""
    monkeypatch.delenv("SESSION_LIFETIME_HOURS", raising=False)
    cfg = _reload_config()
    assert cfg.ProductionConfig.PERMANENT_SESSION_LIFETIME == cfg.Config.PERMANENT_SESSION_LIFETIME
    assert cfg.ProductionConfig.PERMANENT_SESSION_LIFETIME == timedelta(hours=2)


def test_sessions_refresh_on_each_request(monkeypatch):
    """Idle timeout, not absolute — a curator working continuously stays logged in."""
    monkeypatch.delenv("SESSION_LIFETIME_HOURS", raising=False)
    cfg = _reload_config()
    assert cfg.Config.SESSION_REFRESH_EACH_REQUEST is True


# ---------------------------------------------------------------------------
# The lifetime is actually applied
# ---------------------------------------------------------------------------

def test_login_marks_the_session_permanent():
    """Without this the configured lifetime is dead config: Flask only enforces
    PERMANENT_SESSION_LIFETIME on permanent sessions."""
    from src.blueprints import auth
    from app import app as flask_app

    with flask_app.test_request_context():
        from flask import session
        assert session.permanent is False
        auth._start_session({"username": "github:someone", "email": "a@b.c"})
        assert session.permanent is True
        assert session["user"]["username"] == "github:someone"


def test_both_login_paths_go_through_start_session():
    """OAuth and guest login must not diverge — a guest whose session is not
    permanent would silently get the old until-browser-closes behaviour."""
    import inspect
    from src.blueprints import auth

    src = inspect.getsource(auth)
    assert src.count("_start_session(") >= 3, (
        "expected the helper plus both call sites (OAuth callback and guest login)"
    )
    # No direct assignment should remain, or it would bypass session.permanent.
    assert 'session["user"] = {' not in src, (
        "a login path still assigns session['user'] directly, bypassing _start_session"
    )


# ---------------------------------------------------------------------------
# Landing somewhere useful when the session ends
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_auth_login_route_exists(client):
    """main.js sends every expired session here; it used to 404."""
    r = client.get("/auth/login")
    assert r.status_code in (301, 302), f"expected a redirect, got {r.status_code}"
    assert "/mapper" in r.headers["Location"]


def test_auth_login_opens_the_sign_in_prompt(client):
    r = client.get("/auth/login")
    assert "login=1" in r.headers["Location"], (
        "the mapper must be told to open the login modal, or the user arrives at a "
        "normal-looking page with no explanation"
    )


def test_auth_login_remembers_where_the_user_was(client):
    r = client.get("/auth/login?next=/mapper%3Ftab%3Dgo")
    assert r.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess.get("login_next_url") == "/mapper?tab=go"


def test_auth_login_is_a_no_op_when_already_signed_in(client):
    with client.session_transaction() as sess:
        sess["user"] = {"username": "github:someone"}
    r = client.get("/auth/login")
    assert r.status_code in (301, 302)
    assert "login=1" not in r.headers["Location"]


def test_js_redirect_target_matches_the_route():
    """Guard against the two drifting apart again."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_js = open(os.path.join(root, "static", "js", "main.js"), encoding="utf-8").read()
    auth_py = open(os.path.join(root, "src", "blueprints", "auth.py"), encoding="utf-8").read()

    assert "'/auth/login'" in main_js, "main.js no longer redirects to /auth/login"
    assert '"/auth/login"' in auth_py, (
        "main.js redirects to /auth/login but no such route is registered"
    )


def test_login_modal_autoopens_on_the_query_flag():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nav = open(os.path.join(root, "templates", "components", "navigation.html"),
               encoding="utf-8").read()
    assert "login=1" in nav and "login-modal" in nav, (
        "navigation.html does not open the login modal on ?login=1"
    )
