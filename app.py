"""
KE-WP Mapping Application
Refactored Flask application using modular blueprint architecture
"""
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_limiter.errors import RateLimitExceeded

from src.utils.timezone import format_admin_timestamp
from src.utils.text import sanitize_log

# Import blueprints
from src.blueprints import admin_bp, api_bp, auth_bp, main_bp, v1_api_bp
from src.blueprints.admin import set_models as set_admin_models
from src.blueprints.api import set_models as set_api_models

# Import blueprint model setters
from src.blueprints.auth import set_models as set_auth_models
from src.blueprints.main import set_models as set_main_models
from src.blueprints.v1_api import set_models as set_v1_api_models

# Import configuration and services
from src.core.config import get_config
from src.core.error_handlers import register_error_handlers

# Import monitoring
from src.services.rate_limiter import general_rate_limit
from src.services.container import ServiceContainer

# Load environment variables
load_dotenv(".env")  # Explicitly specify .env file

# Debug: Check if environment variables are loaded
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

required_vars = ["FLASK_SECRET_KEY", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"]
for var in required_vars:
    value = os.getenv(var)
    logger.info(
        "%s: %s (%s)", var, 'SET' if value else 'NOT SET', '*' * min(len(value) if value else 0, 5) if value else 'None'
    )

# Flask-Limiter instance — initialized against app in create_app() via init_app pattern.
# storage_uri defaults to in-memory. With Gunicorn 2 workers (gunicorn.conf.py),
# each worker tracks limits independently, giving an effective 200 req/hour/IP under load.
# Redis-backed storage is a post-v1.0 upgrade path.
limiter = Limiter(key_func=get_remote_address, default_limits=[])


def _load_reactome_metadata(app):
    """Load data/reactome_pathway_metadata.json into a {reactome_id: {...}} dict.

    Used by the v1 Reactome serializer to populate pathway_description.
    Returns {} on missing/malformed file (graceful fallback for dev/test envs).
    """
    import json as _json
    path = os.path.join(os.path.dirname(__file__), "data", "reactome_pathway_metadata.json")
    try:
        with open(path) as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as exc:
        app.logger.warning(
            "Could not load Reactome pathway metadata from %s: %s", path, exc
        )
        return {}


def _load_reactome_gene_counts(app):
    """Compute {reactome_id: gene_count} from data/reactome_gene_annotations.json.

    Used by the v1 Reactome serializer to populate reactome_gene_count.
    Returns {} on missing/malformed file (graceful fallback for dev/test envs).
    """
    import json as _json
    path = os.path.join(os.path.dirname(__file__), "data", "reactome_gene_annotations.json")
    try:
        with open(path) as f:
            ann = _json.load(f)
        return {rid: len(genes) for rid, genes in ann.items()}
    except (OSError, _json.JSONDecodeError) as exc:
        app.logger.warning(
            "Could not load Reactome gene annotations from %s: %s", path, exc
        )
        return {}


def create_app(config_name: str = None):
    """
    Application factory function
    Creates and configures Flask application with blueprints

    Args:
        config_name: Configuration environment ('development', 'production', 'testing')

    Returns:
        Configured Flask application instance
    """
    app = Flask(__name__)

    # Trust proxy headers from Traefik (X-Forwarded-For, X-Forwarded-Proto, etc.)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Load configuration
    config = get_config(config_name)
    app.config.from_object(config)

    # Ensure SECRET_KEY is set for Flask-WTF
    app.secret_key = config.FLASK_SECRET_KEY

    # Configure logging
    logging.basicConfig(
        level=logging.INFO if not config.DEBUG else logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Initialize service container
    services = ServiceContainer(config)

    # Initialize CSRF protection
    csrf = CSRFProtect(app)
    csrf.exempt(v1_api_bp)

    # Flask-Limiter — init must occur after csrf.exempt and before blueprint registration
    app.config["RATELIMIT_HEADERS_ENABLED"] = True
    app.config["RATELIMIT_HEADER_RETRY_AFTER_VALUE"] = "delta-seconds"
    limiter.init_app(app)
    limiter.limit("100 per hour")(v1_api_bp)

    # Register error handlers
    register_error_handlers(app)

    # CSRF error handler
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        logger.warning("CSRF error: %s from %s", sanitize_log(str(e.description)), sanitize_log(request.remote_addr))
        # A session-bound CSRF token becomes invalid the moment the login session
        # expires, so a CSRF failure with no logged-in user is really an expired
        # session — surface it as 401 so the client can prompt re-login and preserve
        # the in-progress assessment, instead of a misleading generic error (#195).
        session_expired = "user" not in session
        # The mapper submits form-encoded data via jQuery ($.post), so request.is_json
        # is False; detect AJAX via X-Requested-With so these posts still get a JSON
        # body the client can key on (rather than an HTML error page).
        wants_json = (
            request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            if session_expired:
                return jsonify({"error": "session_expired"}), 401
            return jsonify({"error": "CSRF token missing or invalid"}), 400
        if session_expired:
            return (
                render_template(
                    "error.html",
                    error="Your session has expired. Please log in again.",
                ),
                401,
            )
        return (
            render_template(
                "error.html", error="Security token expired. Please refresh the page."
            ),
            400,
        )

    @app.errorhandler(RateLimitExceeded)
    def handle_rate_limit_exceeded(e):
        from flask import jsonify as _jsonify
        return _jsonify({
            "error": "Rate limit exceeded. Retry after the number of seconds in the Retry-After header.",
            "limit": "100 requests per hour per IP",
        }), 429

    # Initialize OAuth
    services.init_oauth(app)

    # Set up models for blueprints
    set_auth_models(services.provider_clients, guest_code=services.guest_code_model)
    set_api_models(
        services.mapping_model, services.proposal_model, services.cache_model,
        services.pathway_suggestion_service,
        go_suggestion_svc=services.go_suggestion_service,
        go_mapping=services.go_mapping_model,
        go_proposal=services.go_proposal_model,
        ke_meta=services.ke_metadata,
        pathway_meta=services.pathway_metadata,
        ke_aop_membership_data=services.ke_aop_membership,
        reactome_suggestion_svc=services.reactome_suggestion_service,
        reactome_mapping=services.reactome_mapping_model,
        reactome_proposal=services.reactome_proposal_model,
    )
    set_admin_models(services.proposal_model, services.mapping_model,
                     guest_code=services.guest_code_model,
                     go_mapping=services.go_mapping_model, go_proposal=services.go_proposal_model,
                     cache_model=services.cache_model, ke_override=services.ke_override_model,
                     reactome_mapping=services.reactome_mapping_model,
                     reactome_proposal=services.reactome_proposal_model)
    # Phase 26 D-06/D-10: load Reactome enrichment data once at startup so
    # the v1 Reactome serializer can resolve pathway_description and
    # reactome_gene_count in O(1) per row.
    reactome_metadata_dict = _load_reactome_metadata(app)
    reactome_gene_counts_dict = _load_reactome_gene_counts(app)

    set_main_models(
        services.mapping_model,
        go_mapping=services.go_mapping_model,
        cache_model=services.cache_model,
        ker_adjacency_data=services.ker_adjacency,
        reactome_mapping=services.reactome_mapping_model,
        reactome_meta=reactome_metadata_dict,
    )

    set_v1_api_models(
        mapping=services.mapping_model,
        go_mapping=services.go_mapping_model,
        cache=services.cache_model,
        ke_meta_index=services.ke_metadata_index,
        ke_aop_data=services.ke_aop_membership,
        go_hier=services.go_hierarchy,
        go_bp_meta=services.go_bp_metadata,
        go_mf_meta=services.go_mf_metadata,
        reactome_mapping=services.reactome_mapping_model,
        reactome_meta=reactome_metadata_dict,
        reactome_counts=reactome_gene_counts_dict,
    )

    # Context processor to make is_admin available to all templates
    @app.context_processor
    def inject_user_context():
        """Inject user context including admin, guest, and provider availability to all templates"""
        from flask import session
        from src.blueprints.admin import _get_admin_users

        user_data = session.get("user", {})
        current_user = user_data.get("username")
        is_guest = user_data.get("is_guest", False)

        if current_user:
            is_admin = current_user in _get_admin_users()
        else:
            is_admin = False

        # Provider availability flags for login modal
        orcid_configured = bool(os.getenv("ORCID_CLIENT_ID") and os.getenv("ORCID_CLIENT_SECRET"))
        ls_configured = bool(os.getenv("LS_CLIENT_ID") and os.getenv("LS_CLIENT_SECRET"))
        surf_configured = bool(os.getenv("SURF_CLIENT_ID") and os.getenv("SURF_CLIENT_SECRET"))
        # Feature flag: SURFconext is gated until the production tenant is approved.
        # Set SURF_ENABLED=true on tgx1 only after the SURFconext production tenant is live.
        surf_enabled = os.getenv("SURF_ENABLED", "false").lower() == "true"

        return dict(
            is_admin=is_admin,
            is_guest=is_guest,
            orcid_configured=orcid_configured,
            ls_configured=ls_configured,
            surf_configured=surf_configured,
            surf_enabled=surf_enabled,
        )

    @app.template_filter("display_username")
    def display_username_filter(username):
        """Strip provider prefix for display: 'github:mmartens' -> 'mmartens'"""
        if username and ":" in username:
            return username.split(":", 1)[1]
        return username or ""

    @app.context_processor
    def inject_zenodo_meta():
        """Inject Zenodo DOI metadata globally so navbar can display citation."""
        import json as _json
        from pathlib import Path as _Path
        try:
            meta_path = _Path("data/zenodo_meta.json")
            if meta_path.exists():
                return {"zenodo_meta": _json.loads(meta_path.read_text())}
        except Exception as e:
            # Best-effort load — missing or malformed file just means the
            # footer Zenodo DOI badge stays empty.
            app.logger.debug("zenodo_meta context load failed: %s", e)
        return {"zenodo_meta": {}}

    @app.context_processor
    def inject_source_versions():
        """
        Inject the upstream source-versions manifest globally so templates
        can display the snapshot the running container is serving.

        Surfaced as two template variables:
          - ``source_versions`` — static snapshot from data/source_versions.json
            (used for per-approval stamping; kept unchanged).
          - ``live_versions``   — live upstream release identifiers from
            SourceVersionService.snapshot() with a 24 h in-process TTL; each
            entry is {"version": str, "unavailable": bool}.

        Reading source_versions.json directly here keeps the context processor
        a pure read with no service-graph dependency. live_versions failure is
        caught so a service outage never breaks any page render.
        """
        import json as _json
        from pathlib import Path as _Path
        ctx: dict = {}

        # Static file read (source_versions — unchanged)
        try:
            path = _Path("data/source_versions.json")
            if path.exists():
                ctx["source_versions"] = _json.loads(path.read_text())
        except Exception as e:
            # Best-effort load — missing or malformed manifest just means the
            # footer snapshot block stays empty (acceptable on first deploy).
            app.logger.debug("source_versions context load failed: %s", e)
        if "source_versions" not in ctx:
            ctx["source_versions"] = {}

        # Live version service (live_versions — new, wraps SourceVersionService)
        try:
            from src.services.source_versions import snapshot as _sv_snapshot
            ctx["live_versions"] = _sv_snapshot()
        except Exception as e:
            app.logger.debug("live_versions service call failed: %s", e)
            ctx["live_versions"] = {}

        return ctx

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(v1_api_bp)

    # Health check and monitoring endpoints
    @app.route("/health")
    def health_check():
        """Simple health check endpoint"""
        try:
            health_status = services.get_health_status()
            return jsonify(
                {
                    "status": "healthy" if all(health_status.values()) else "degraded",
                    "timestamp": format_admin_timestamp(),
                    "version": "2.7.0",
                    "services": health_status,
                }
            )
        except Exception as e:
            logger.error("Health check failed: %s", e)
            return (
                jsonify(
                    {
                        "status": "unhealthy",
                        "timestamp": format_admin_timestamp(),
                        "error": "Health check failed",
                    }
                ),
                500,
            )

    @app.route("/metrics")
    @general_rate_limit
    def metrics():
        """Get system metrics (JSON API)"""
        return jsonify(services.metrics_collector.get_system_health())

    @app.route("/metrics/<endpoint_name>")
    @general_rate_limit
    def endpoint_metrics(endpoint_name):
        """Get metrics for a specific endpoint"""
        hours = request.args.get("hours", 24, type=int)
        return jsonify(services.metrics_collector.get_endpoint_stats(endpoint_name, hours))

    # Application teardown
    @app.teardown_appcontext
    def cleanup_services(error):
        """Cleanup services on app context teardown"""
        if error:
            logger.error("Application context error: %s", error)

    # Store service container for access by other modules
    app.service_container = services

    logger.info("Application initialized successfully with blueprint architecture")
    return app


# Create application instance for gunicorn/uwsgi
app = create_app()

# Warm up embedding service for Gunicorn preload_app=True.
# With preload_app, Gunicorn imports this module in the master process before forking workers.
# ServiceContainer.embedding_service is lazy — accessing it here forces BioBERT to load
# in the master, so workers inherit the loaded model via Linux fork copy-on-write.
# Guard: only fire in production to avoid loading BioBERT during tests or dev server startup.
if os.getenv("FLASK_ENV") == "production":
    try:
        _svc = app.service_container.embedding_service
        if _svc is not None:
            logger.info("Embedding service pre-loaded for Gunicorn worker fork (preload_app=True)")
        else:
            logger.info("Embedding service disabled by config — skipping preload warm-up")
    except Exception as _e:
        logger.warning("Embedding service warm-up failed (non-fatal): %s", _e)

if __name__ == "__main__":
    # Development server configuration
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    port = int(os.getenv("PORT", 5000))
    host = os.getenv("HOST", "127.0.0.1")

    app.run(debug=debug_mode, host=host, port=port)
