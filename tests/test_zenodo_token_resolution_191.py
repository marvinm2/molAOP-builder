"""
Zenodo API token resolution from Docker secrets or the environment (#191).

The production token was revoked in Zenodo's 2026-05-21 session incident and
had to be reissued. It was stored as a plain service environment variable,
which means it is readable by anyone who can run `docker service inspect` on
the cluster — a wider audience than the people who should hold a publishing
credential. The reissued token is installed as a Docker secret instead.

`resolve_zenodo_token` therefore checks, first hit wins:

  1. <VAR>_FILE          — explicit path to a token file
  2. /run/secrets/<var>  — where Swarm mounts a secret of that name
  3. <VAR>               — the plain env var, kept so existing deploys work

These tests pin that precedence and the failure modes around it.
"""

import pytest

from src.exporters import zenodo_uploader
from src.exporters.zenodo_uploader import resolve_zenodo_token


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Start from no env vars and a secret path that does not exist."""
    for var in ("ZENODO_API_TOKEN", "ZENODO_API_TOKEN_FILE",
                "ZENODO_SANDBOX_API_TOKEN", "ZENODO_SANDBOX_API_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(
        zenodo_uploader._SECRET_PATHS, "ZENODO_API_TOKEN", str(tmp_path / "absent")
    )
    return tmp_path


def test_returns_none_when_nothing_is_configured():
    assert resolve_zenodo_token("ZENODO_API_TOKEN") is None


def test_reads_the_plain_env_var(monkeypatch):
    """Back-compat: an existing env-var deployment keeps working."""
    monkeypatch.setenv("ZENODO_API_TOKEN", "env-token")

    assert resolve_zenodo_token("ZENODO_API_TOKEN") == "env-token"


def test_reads_the_default_swarm_secret_path(monkeypatch, clean_env):
    """A secret mounted at the conventional path needs no configuration."""
    secret = clean_env / "zenodo_api_token"
    secret.write_text("secret-token\n")  # trailing newline is normal for secrets
    monkeypatch.setitem(
        zenodo_uploader._SECRET_PATHS, "ZENODO_API_TOKEN", str(secret)
    )

    assert resolve_zenodo_token("ZENODO_API_TOKEN") == "secret-token"


def test_secret_file_wins_over_env_var(monkeypatch, clean_env):
    """Precedence matters during a migration, when both are briefly present."""
    secret = clean_env / "token"
    secret.write_text("secret-token")
    monkeypatch.setenv("ZENODO_API_TOKEN_FILE", str(secret))
    monkeypatch.setenv("ZENODO_API_TOKEN", "env-token")

    assert resolve_zenodo_token("ZENODO_API_TOKEN") == "secret-token"


def test_explicit_file_var_wins_over_default_secret_path(monkeypatch, clean_env):
    default_secret = clean_env / "default"
    default_secret.write_text("default-token")
    explicit = clean_env / "explicit"
    explicit.write_text("explicit-token")
    monkeypatch.setitem(
        zenodo_uploader._SECRET_PATHS, "ZENODO_API_TOKEN", str(default_secret)
    )
    monkeypatch.setenv("ZENODO_API_TOKEN_FILE", str(explicit))

    assert resolve_zenodo_token("ZENODO_API_TOKEN") == "explicit-token"


def test_empty_secret_file_falls_back_to_env(monkeypatch, clean_env):
    """A half-created secret must not mask a working env var."""
    secret = clean_env / "empty"
    secret.write_text("   \n")
    monkeypatch.setenv("ZENODO_API_TOKEN_FILE", str(secret))
    monkeypatch.setenv("ZENODO_API_TOKEN", "env-token")

    assert resolve_zenodo_token("ZENODO_API_TOKEN") == "env-token"


def test_missing_secret_file_falls_back_to_env(monkeypatch, clean_env):
    monkeypatch.setenv("ZENODO_API_TOKEN_FILE", str(clean_env / "nope"))
    monkeypatch.setenv("ZENODO_API_TOKEN", "env-token")

    assert resolve_zenodo_token("ZENODO_API_TOKEN") == "env-token"


def test_blank_env_var_is_treated_as_unset(monkeypatch):
    """`--env-add ZENODO_API_TOKEN=` should not read as 'configured'."""
    monkeypatch.setenv("ZENODO_API_TOKEN", "   ")

    assert resolve_zenodo_token("ZENODO_API_TOKEN") is None


def test_sandbox_token_resolves_independently(monkeypatch):
    monkeypatch.setenv("ZENODO_SANDBOX_API_TOKEN", "sandbox-token")

    assert resolve_zenodo_token("ZENODO_SANDBOX_API_TOKEN") == "sandbox-token"
    assert resolve_zenodo_token("ZENODO_API_TOKEN") is None


def test_publish_raises_a_actionable_error_when_no_token(monkeypatch):
    """The error should name all three ways to supply the token."""
    with pytest.raises(EnvironmentError) as exc:
        zenodo_uploader.zenodo_publish(files={}, metadata={})

    msg = str(exc.value)
    assert "ZENODO_API_TOKEN" in msg
    assert "/run/secrets/zenodo_api_token" in msg
    assert "ZENODO_API_TOKEN_FILE" in msg
