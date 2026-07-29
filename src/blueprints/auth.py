"""
Authentication Blueprint
Handles user login, logout, and multi-provider OAuth flows
"""
import logging

from flask import Blueprint, redirect, render_template, request, session, url_for

from src.services.rate_limiter import submission_rate_limit
from src.utils.text import sanitize_log

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

# Provider clients dict (set by app initialization via set_models)
provider_clients = {}
guest_code_model = None


def set_models(clients: dict, guest_code=None):
    """Set the OAuth provider clients dict and guest code model"""
    global provider_clients, guest_code_model
    provider_clients = clients
    guest_code_model = guest_code


def _start_session(user: dict):
    """Record a logged-in user and put the session under PERMANENT_SESSION_LIFETIME.

    Flask only applies that lifetime to sessions marked permanent. Nothing marked
    them, so the configured lifetime never took effect and the cookie lived until
    the browser closed — while the session-bound CSRF token still expired on its own
    schedule, which is what actually ended a working session. Marking the session
    permanent makes the configured lifetime the single thing that governs a login,
    and because Flask re-issues the cookie on every request it is an idle timeout:
    a curator working continuously is not logged out.
    """
    session.permanent = True
    session["user"] = user


@auth_bp.route("/login/<provider>")
def login_provider(provider):
    """Initiate OAuth login flow for the given provider"""
    client = provider_clients.get(provider)
    if not client:
        logger.error("OAuth client not found for provider: %s", sanitize_log(provider))
        return redirect(url_for("main.landing"))

    next_url = request.args.get("next") or request.referrer
    if next_url:
        session["login_next_url"] = next_url

    redirect_uri = url_for("auth.oauth_callback", provider=provider, _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route("/callback/<provider>")
def oauth_callback(provider):
    """Handle OAuth callback for the given provider"""
    client = provider_clients.get(provider)
    if not client:
        logger.error("OAuth callback for unknown provider: %s", sanitize_log(provider))
        return redirect(url_for("main.landing"))

    try:
        token = client.authorize_access_token()

        if provider == "github":
            user_info = client.get("user").json()
            user_emails = client.get("user/emails").json()
            sub = user_info["login"]
            email = (
                user_emails[0]["email"]
                if user_emails and len(user_emails) > 0
                else "No public email"
            )
        else:
            # OIDC providers: authlib auto-parses id_token
            userinfo = token.get("userinfo", {})
            sub = userinfo.get("sub", "")
            email = userinfo.get("email", "")

        prefixed_username = f"{provider}:{sub}"
        _start_session({
            "username": prefixed_username,
            "email": email,
            "provider": provider,
        })
        logger.info("User %s logged in via %s", sanitize_log(prefixed_username), sanitize_log(provider))
        next_url = session.pop("login_next_url", None) or url_for("main.landing")
        return redirect(next_url)
    except Exception as e:
        logger.error("OAuth callback error for %s: %s", sanitize_log(provider), sanitize_log(str(e)))
        session.pop("login_next_url", None)
        return redirect(url_for("main.landing"))


@auth_bp.route("/auth/login")
def login_landing():
    """Where the client sends a user whose session expired.

    static/js/main.js redirects to `/auth/login` on a 401 from every submit path,
    and no such route existed — so the one moment a curator most needs to get back
    in, they landed on "The requested page was not found". Send them to the mapper
    with the login modal open, preserving where they were so the post-login redirect
    returns them there.
    """
    if session.get("user"):
        return redirect(url_for("main.mapper"))
    next_url = request.args.get("next") or request.referrer
    if next_url:
        session["login_next_url"] = next_url
    return redirect(url_for("main.mapper", login="1"))


@auth_bp.route("/logout")
def logout():
    """Log out the current user"""
    username = session.get("user", {}).get("username", "unknown")
    session.pop("user", None)
    logger.info("User %s logged out", username)
    return redirect(url_for("main.landing"))


@auth_bp.route("/guest-login")
def guest_login():
    """Redirect to index -- guest login form is now inside the login modal"""
    if session.get("user"):
        return redirect(url_for("main.landing"))
    next_url = request.args.get("next") or request.referrer
    if next_url:
        session["login_next_url"] = next_url
    return redirect(url_for("main.landing"))


@auth_bp.route("/guest-login", methods=["POST"])
@submission_rate_limit
def guest_login_submit():
    """Validate guest access code and create session"""
    if session.get("user"):
        return redirect(url_for("main.landing"))

    code = request.form.get("code", "").strip()
    if not code or not guest_code_model:
        return render_template("guest_login.html", error="Invalid access code.")

    result = guest_code_model.validate_code(code)
    if not result:
        logger.warning("Failed guest login attempt")
        return render_template("guest_login.html", error="Invalid, expired, or exhausted access code.")

    _start_session({
        "username": f"guest-{result['label']}",
        "email": "workshop-guest",
        "is_guest": True,
    })
    logger.info("Guest user logged in with label=%s", result["label"])
    next_url = session.pop("login_next_url", None) or url_for("main.landing")
    return redirect(next_url)
