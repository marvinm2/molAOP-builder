"""
Configuration management for the KE-WP Mapping application
"""
import os
from datetime import timedelta


class Config:
    """Base configuration class"""

    # Security
    @property
    def FLASK_SECRET_KEY(self):
        return os.getenv("FLASK_SECRET_KEY")

    # How long a login lasts, in hours. Applies to both the session and the CSRF
    # token, which must not diverge: the CSRF token is session-bound, so whichever
    # expires first ends the login, and a shorter CSRF limit silently caps the
    # session no matter what the session lifetime says.
    SESSION_LIFETIME_HOURS = float(os.getenv("SESSION_LIFETIME_HOURS", "2"))

    WTF_CSRF_TIME_LIMIT = int(SESSION_LIFETIME_HOURS * 3600)

    # Session Configuration
    PERMANENT_SESSION_LIFETIME = timedelta(hours=SESSION_LIFETIME_HOURS)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Idle timeout, not absolute: Flask re-issues the cookie on each request, so an
    # active curator is never logged out mid-session. Only inactivity expires it.
    SESSION_REFRESH_EACH_REQUEST = True

    # OAuth Configuration
    @property
    def GITHUB_CLIENT_ID(self):
        return os.getenv("GITHUB_CLIENT_ID")

    @property
    def GITHUB_CLIENT_SECRET(self):
        return os.getenv("GITHUB_CLIENT_SECRET")

    # ORCID OAuth Configuration
    @property
    def ORCID_CLIENT_ID(self):
        return os.getenv("ORCID_CLIENT_ID")

    @property
    def ORCID_CLIENT_SECRET(self):
        return os.getenv("ORCID_CLIENT_SECRET")

    @property
    def ORCID_DISCOVERY_URL(self):
        return os.getenv(
            "ORCID_DISCOVERY_URL",
            "https://sandbox.orcid.org/.well-known/openid-configuration",
        )

    # Life Science Login OAuth Configuration
    @property
    def LS_CLIENT_ID(self):
        return os.getenv("LS_CLIENT_ID")

    @property
    def LS_CLIENT_SECRET(self):
        return os.getenv("LS_CLIENT_SECRET")

    @property
    def LS_DISCOVERY_URL(self):
        return os.getenv(
            "LS_DISCOVERY_URL",
            "https://oidc.pilot.lifescienceid.org/oauth2/.well-known/openid-configuration",
        )

    # SURFconext OAuth Configuration
    @property
    def SURF_CLIENT_ID(self):
        return os.getenv("SURF_CLIENT_ID")

    @property
    def SURF_CLIENT_SECRET(self):
        return os.getenv("SURF_CLIENT_SECRET")

    @property
    def SURF_DISCOVERY_URL(self):
        return os.getenv(
            "SURF_DISCOVERY_URL",
            "https://connect.test.surfconext.nl/.well-known/openid-configuration",
        )

    # Database
    @property
    def DATABASE_URL(self):
        return os.getenv("DATABASE_URL", "sqlite:////app/data/ke_wp_mapping.db")

    @property
    def DATABASE_PATH(self):
        return os.getenv("DATABASE_PATH", "/app/data/ke_wp_mapping.db")

    # Admin Configuration
    ADMIN_USERS = [
        user.strip() for user in os.getenv("ADMIN_USERS", "").split(",") if user.strip()
    ]

    # API Configuration
    SPARQL_TIMEOUT = 30
    CACHE_DEFAULT_TIMEOUT = 24 * 3600  # 24 hours in seconds

    # Rate Limiting
    RATELIMIT_STORAGE_URL = os.getenv("RATELIMIT_STORAGE_URL", "memory://")

    def validate_required_config(self):
        """Validate that all required configuration values are set"""
        required_vars = ["FLASK_SECRET_KEY", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"]
        missing_vars = [var for var in required_vars if not getattr(self, var)]

        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}"
            )

        return True


class DevelopmentConfig(Config):
    """Development configuration"""

    DEBUG = True
    FLASK_ENV = "development"
    SESSION_COOKIE_SECURE = False

    # Development-specific settings
    WTF_CSRF_ENABLED = True  # Keep CSRF enabled even in development
    TESTING = False


class ProductionConfig(Config):
    """Production configuration"""

    DEBUG = False
    FLASK_ENV = "production"
    SESSION_COOKIE_SECURE = True

    # Production-specific settings
    WTF_CSRF_ENABLED = True
    TESTING = False

    # NB: production used to override PERMANENT_SESSION_LIFETIME to 30 minutes. That
    # override was inert — nothing set `session.permanent`, so Flask never applied the
    # lifetime at all and the cookie simply lived until the browser closed. Removing it
    # keeps the single, now-effective SESSION_LIFETIME_HOURS from the base config rather
    # than reinstating a stricter number that never actually held. Tune per deployment
    # with the env var if production warrants something shorter.


class TestingConfig(Config):
    """Testing configuration"""

    TESTING = True
    DEBUG = True
    FLASK_ENV = "testing"
    SESSION_COOKIE_SECURE = False

    # Test-specific settings
    DATABASE_PATH = ":memory:"  # Use in-memory database for tests
    WTF_CSRF_ENABLED = False  # Disable CSRF for easier testing

    # Override OAuth settings for testing
    GITHUB_CLIENT_ID = "test_client_id"
    GITHUB_CLIENT_SECRET = "test_client_secret"
    FLASK_SECRET_KEY = "test_secret_key"


# Configuration mapping
config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}


def get_config(config_name: str = None) -> Config:
    """
    Get configuration class based on environment

    Args:
        config_name: Configuration name ('development', 'production', 'testing')
                    If None, uses FLASK_ENV environment variable

    Returns:
        Configuration class instance
    """
    if config_name is None:
        config_name = os.getenv("FLASK_ENV", "development")

    config_class = config.get(config_name, config["default"])
    config_instance = config_class()

    # Validate configuration before returning
    if config_name != "testing":  # Skip validation for testing
        config_instance.validate_required_config()

    return config_instance
