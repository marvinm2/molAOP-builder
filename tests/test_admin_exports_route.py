"""Admin /admin/exports* route tests (#158 follow-up — A-arm).

Pins the integration contract between the dashboard, the POST routes, and
the shared `zenodo_assembly` module:

- GET /admin/exports renders for an admin session, shows live counts and
  the existing zenodo_meta block.
- POST /admin/exports/publish-zenodo guards on missing token (503), and
  on a successful publish bundles the v3 deposit shape (three per-resource
  ZIPs + README.md) via `zenodo_assembly`, with the route preserving the
  caller's `existing_deposition_id` semantics through the uploader.

We stub the network-touching `zenodo_publish` to capture the upload
payload — no live Zenodo traffic.
"""
import json
import os
import tempfile
import zipfile
import io
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixture — temp DB, admin session, models wired, no live network
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_app():
    os.environ["ADMIN_USERS"] = "github:exportadmin"

    from app import app as flask_app
    import src.blueprints.admin as admin_mod
    from src.core.models import (
        Database, MappingModel, GoMappingModel, ReactomeMappingModel,
    )

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)

    mm = MappingModel(db)
    gmm = GoMappingModel(db)
    rmm = ReactomeMappingModel(db)

    orig_mm = admin_mod.mapping_model
    orig_gmm = admin_mod.go_mapping_model
    orig_rmm = admin_mod.reactome_mapping_model
    admin_mod.mapping_model = mm
    admin_mod.go_mapping_model = gmm
    admin_mod.reactome_mapping_model = rmm

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        with flask_app.app_context():
            yield {"client": client, "db": db, "mm": mm, "gmm": gmm, "rmm": rmm}

    admin_mod.mapping_model = orig_mm
    admin_mod.go_mapping_model = orig_gmm
    admin_mod.reactome_mapping_model = orig_rmm

    os.close(fd)
    os.unlink(db_path)


def _login_admin(client, username="github:exportadmin"):
    with client.session_transaction() as sess:
        sess["user"] = {"username": username, "email": "admin@example.com"}


def _seed_one_approved_mapping(mm, ke="KE 1", wp="WP1", conf="high"):
    """Insert a single approved KE-WP mapping by raw SQL — the public
    APIs require richer plumbing than the test needs and the assembly
    layer only cares about the shape of the returned dict."""
    import uuid
    with mm.db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO mappings (uuid, ke_id, ke_title, wp_id, wp_title,
                connection_type, confidence_level, created_by,
                approved_by_curator, approved_at_curator)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), ke, f"Title for {ke}", wp, f"Title for {wp}",
                "causative", conf, "github:test-user", "test-admin",
                "2026-05-14T10:00:00",
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# GET /admin/exports
# ---------------------------------------------------------------------------

def test_admin_exports_page_renders_for_admin(admin_app):
    client = admin_app["client"]
    _login_admin(client)
    _seed_one_approved_mapping(admin_app["mm"], ke="KE 42", wp="WP123", conf="high")

    res = client.get("/admin/exports")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    # Page header + section titles
    assert "Exports" in html and "Zenodo" in html
    assert "Current approved mappings" in html
    assert "Last Zenodo deposit" in html
    # Live count for WP is 1, our seeded row.
    assert ">1<" in html


def test_admin_exports_page_requires_admin(admin_app):
    """Non-admin sessions get bounced — admin_required redirects or
    returns a non-2xx."""
    client = admin_app["client"]
    with client.session_transaction() as sess:
        sess["user"] = {"username": "github:randomuser", "email": "x@example.com"}
    res = client.get("/admin/exports")
    assert res.status_code in (302, 403)


# ---------------------------------------------------------------------------
# POST /admin/exports/publish-zenodo — guards
# ---------------------------------------------------------------------------

def test_publish_zenodo_503_without_token(admin_app, monkeypatch):
    """Token absent → 503 with a clear message — never reaches Zenodo."""
    monkeypatch.delenv("ZENODO_API_TOKEN", raising=False)
    client = admin_app["client"]
    _login_admin(client)

    res = client.post("/admin/exports/publish-zenodo")
    assert res.status_code == 503
    body = res.get_json()
    assert "ZENODO_API_TOKEN" in body["message"]


def test_publish_zenodo_400_when_no_mappings(admin_app, monkeypatch):
    """Empty database → 400 with a clear message."""
    monkeypatch.setenv("ZENODO_API_TOKEN", "fake-token")
    client = admin_app["client"]
    _login_admin(client)

    res = client.post("/admin/exports/publish-zenodo")
    assert res.status_code == 400
    body = res.get_json()
    assert "No approved mappings" in body["message"]


# ---------------------------------------------------------------------------
# POST /admin/exports/publish-zenodo — happy path
# ---------------------------------------------------------------------------

def _gmt_stub(rows, min_confidence=None, confidence=None, **kw):
    # Delegates to the production filters. Naming `confidence` explicitly
    # matters: regenerate_exports switched to it for the exact-tier partition
    # in #206, and a stub that absorbed it into **kw would silently stop
    # filtering while still passing.
    from src.exporters.gmt_exporter import _apply_confidence

    rows = _apply_confidence(rows, min_confidence, confidence)
    if not rows:
        return ""
    return "\n".join(
        f"{r['ke_id']}\tdesc\t{r.get('wp_id') or r.get('go_id') or r.get('reactome_id')}"
        for r in rows
    ) + "\n"


def _ttl_stub(rows):
    if not rows:
        return ""
    return "@prefix : <x:> .\n" + "\n".join(f":r{i} a :Row ." for i, _ in enumerate(rows))


def test_publish_zenodo_bundles_v3_shape(admin_app, monkeypatch, tmp_path):
    """
    Successful publish must:
      - call zenodo_publish with a files dict containing the v3 keys
        (KE-WikiPathways.zip, KE-GO.zip, KE-Reactome.zip, README.md)
      - persist the returned DOI + counts to data/zenodo_meta.json
      - return the DOI + counts in the JSON response

    The real WP/GO/Reactome GMT + Turtle generators are stubbed because
    they pull from WikiPathways SPARQL / require richer fixtures than the
    shape-check needs. Per-generator content is covered by their own test
    suites; here we only assert the orchestration is wired correctly.
    """
    monkeypatch.setenv("ZENODO_API_TOKEN", "fake-token")
    monkeypatch.setattr("src.exporters.gmt_exporter.generate_ke_wp_gmt", _gmt_stub)
    monkeypatch.setattr("src.exporters.gmt_exporter.generate_ke_go_gmt", _gmt_stub)
    monkeypatch.setattr("src.exporters.gmt_exporter.generate_ke_reactome_gmt", _gmt_stub)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_wp_turtle", _ttl_stub)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_go_turtle", _ttl_stub)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_reactome_turtle", _ttl_stub)
    client = admin_app["client"]
    _login_admin(client)
    _seed_one_approved_mapping(admin_app["mm"], ke="KE 1", wp="WP1", conf="high")

    captured = {}

    def fake_publish(files, metadata, existing_deposition_id=None):
        captured["files"] = files
        captured["metadata"] = metadata
        captured["existing_id"] = existing_deposition_id
        return {
            "doi": "10.5281/zenodo.99999999",
            "deposition_id": 99999999,
            "concept_doi": "10.5281/zenodo.88888888",
        }

    # Route writes meta_path = Path("data/zenodo_meta.json"); we want that
    # to land somewhere we can inspect, not the real file. Run the request
    # from a tmp cwd so the relative path resolves under it.
    orig_cwd = os.getcwd()
    workdir = tmp_path / "appdir"
    (workdir / "data").mkdir(parents=True)
    os.chdir(workdir)
    try:
        with patch("src.exporters.zenodo_uploader.zenodo_publish", side_effect=fake_publish):
            res = client.post("/admin/exports/publish-zenodo")
    finally:
        os.chdir(orig_cwd)

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["doi"] == "10.5281/zenodo.99999999"
    assert body["concept_doi"] == "10.5281/zenodo.88888888"
    assert body["counts"]["wp"]["All"] == 1

    # Pin the v3 shape on the uploaded files dict.
    assert set(captured["files"].keys()) == {
        "KE-WikiPathways.zip", "KE-GO.zip", "KE-Reactome.zip", "README.md",
    }
    # And confirm one ZIP looks structurally right (Turtle + GMT-by-tier).
    wp_zip = zipfile.ZipFile(io.BytesIO(captured["files"]["KE-WikiPathways.zip"]))
    names = set(wp_zip.namelist())
    assert "KE-WikiPathways/ke-wikipathways-mappings.ttl" in names
    assert any(n.startswith("KE-WikiPathways/KE-WikiPathways_") and n.endswith("_High.gmt")
               for n in names)

    # Meta file persisted with the returned DOI + counts.
    meta = json.loads((workdir / "data" / "zenodo_meta.json").read_text())
    assert meta["doi"] == "10.5281/zenodo.99999999"
    assert meta["concept_doi"] == "10.5281/zenodo.88888888"
    assert meta["counts"]["wp"]["All"] == 1
