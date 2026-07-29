"""The /admin/exports/publish-zenodo guard must use the shared token resolver.

#191 moved the production token from a service environment variable to a Docker
secret at /run/secrets/zenodo_api_token. The exports page render and
`zenodo_publish` both went through `resolve_zenodo_token`, which checks the
secret; the POST route kept a raw `os.environ.get("ZENODO_API_TOKEN")`.

The two disagreed on exactly the deployed configuration: the page rendered the
Publish button enabled, and every click returned
`503 ZENODO_API_TOKEN not configured`. Publishing was impossible from the UI
while the CLI script worked, because only the route held the stale check.

Nothing here may reach zenodo.org. `zenodo_publish` and the assembly helpers
are stubbed out — an unstubbed run of the happy path would mint a real,
permanent deposit under the production concept DOI.
"""
import os

import pytest

from src.exporters import zenodo_assembly, zenodo_uploader


@pytest.fixture
def admin_client(monkeypatch):
    """Admin-authenticated test client with no Zenodo token from any source."""
    os.environ["ADMIN_USERS"] = "github:testadmin"

    from app import app as flask_app

    for var in ("ZENODO_API_TOKEN", "ZENODO_API_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = {
                "username": "github:testadmin",
                "email": "admin@example.com",
            }
        yield client


def _point_secret_at(monkeypatch, path):
    monkeypatch.setitem(
        zenodo_uploader._SECRET_PATHS, "ZENODO_API_TOKEN", str(path)
    )


@pytest.fixture
def offline_publish(monkeypatch):
    """Stub every outbound step, and give the route one mapping to publish.

    Returns the list the stub appends to, so a test can assert the route
    actually reached the upload rather than bailing earlier for its own reasons.
    """
    import src.blueprints.admin as admin_mod

    calls = []

    def fake_publish(upload_files, metadata, existing_deposition_id=None):
        calls.append((upload_files, metadata, existing_deposition_id))
        return {"deposition_id": 1, "doi": "10.5281/zenodo.test", "concept_doi": "10.5281/zenodo.concept"}

    monkeypatch.setattr(zenodo_uploader, "zenodo_publish", fake_publish)
    monkeypatch.setattr(
        zenodo_assembly, "assemble_deposit_files", lambda *a, **k: {"README.md": b"x"}
    )
    monkeypatch.setattr(
        zenodo_assembly, "build_metadata", lambda *a, **k: {"version": "test"}
    )
    monkeypatch.setattr(
        zenodo_uploader, "persist_meta_with_fallback", lambda path, payload: path
    )

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def get_all_mappings(self):
            return self._rows

    # The testing config runs on an empty :memory: database, so the real GO and
    # Reactome models raise on a missing table before the route ever gets to the
    # part under test.
    monkeypatch.setattr(
        admin_mod,
        "mapping_model",
        _Rows([{"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"}]),
    )
    monkeypatch.setattr(admin_mod, "go_mapping_model", _Rows([]))
    monkeypatch.setattr(admin_mod, "reactome_mapping_model", _Rows([]))
    return calls


def test_publish_route_rejects_when_no_token_from_any_source(
    admin_client, monkeypatch, tmp_path, offline_publish
):
    """With neither secret nor env var, the 503 guard still fires."""
    _point_secret_at(monkeypatch, tmp_path / "absent")

    resp = admin_client.post("/admin/exports/publish-zenodo")

    assert resp.status_code == 503
    assert resp.get_json()["status"] == "error"
    assert offline_publish == [], "guard must reject before attempting an upload"


def test_publish_route_accepts_a_docker_secret(
    admin_client, monkeypatch, tmp_path, offline_publish
):
    """The deployed configuration: token present only as a mounted secret."""
    secret = tmp_path / "zenodo_api_token"
    secret.write_text("secret-token\n")
    _point_secret_at(monkeypatch, secret)

    resp = admin_client.post("/admin/exports/publish-zenodo")

    assert resp.status_code == 200, (
        "publish-zenodo must resolve the token from the Docker secret, not from "
        "os.environ — a secret-only deployment could otherwise never publish "
        f"from the admin UI (got {resp.status_code}: {resp.get_json()})"
    )
    assert len(offline_publish) == 1


def test_publish_route_does_not_read_the_env_var_directly():
    """Guard against the raw read being reintroduced.

    The route and the page render must agree, and the only way to guarantee
    that is for both to call the same resolver.
    """
    import inspect

    from src.blueprints.admin import publish_zenodo

    source = inspect.getsource(publish_zenodo)
    assert 'resolve_zenodo_token("ZENODO_API_TOKEN")' in source
    assert 'os.environ.get("ZENODO_API_TOKEN")' not in source


def test_exports_page_button_state_uses_the_same_resolver():
    """The rendered button and the route must key off one source of truth."""
    import inspect

    from src.blueprints.admin import admin_exports

    source = inspect.getsource(admin_exports)
    assert "resolve_zenodo_token" in source
