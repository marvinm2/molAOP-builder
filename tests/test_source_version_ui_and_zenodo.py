"""Source-data versioning Phase E.2 — UI + Zenodo metadata tests.

Covers:

1. The `inject_source_versions` context processor — makes the manifest
   available on every template, gracefully degrades when the file is
   missing or unreadable.

2. The footer snapshot block — renders only when the manifest has 'ok'
   sources; uses the same `<a href="/stats#source-versions">` anchor
   that the Stats-page table is keyed by.

3. The Stats page snapshot table — renders the four-row resource table
   under `#source-versions`.

4. The Zenodo publish-script helpers — README snapshot table, metadata
   description snapshot line, per-resource ZIP slice, and the sidecar
   `source_versions.json` inside each ZIP.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest


# ---------- Context processor + UI surfacing ----------

@pytest.fixture
def app_with_manifest(tmp_path, monkeypatch):
    """Boot the app with a tmp `data/source_versions.json` in place."""
    manifest = {
        "captured_at": "2026-05-14T21:00:00Z",
        "sources": {
            "wikipathways": {
                "status": "ok",
                "release_date": "2026-05-10",
                "captured_at": "2026-05-14T21:00:00Z",
            },
            "gene_ontology": {
                "status": "ok",
                "release_date": "2026-01-23",
                "captured_at": "2026-05-14T21:00:00Z",
            },
            "reactome": {
                "status": "ok",
                "release_version": "96",
                "release_date": "2026-03-25",
                "captured_at": "2026-05-14T21:00:00Z",
            },
            "aopwiki": {
                "status": "ok",
                "snapshot_date": "2026-05-06",
                "captured_at": "2026-05-14T21:00:00Z",
            },
        },
    }
    # The context processor reads "data/source_versions.json" relative to
    # CWD, so chdir into a tmp dir that has our manifest in place.
    Path(__file__).resolve().parent.parent
    work = tmp_path / "work"
    work.mkdir()
    (work / "data").mkdir()
    (work / "data" / "source_versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.chdir(work)

    # Symlink the data dir's other files (templates, etc. are referenced
    # by absolute path inside the app, so chdir alone is enough for the
    # context processor read).
    from app import create_app
    return create_app()


def test_context_processor_injects_source_versions(app_with_manifest):
    """The injected `source_versions` dict is available in every template."""
    with app_with_manifest.test_request_context("/"):
        from flask import render_template_string
        rendered = render_template_string(
            "{{ source_versions.sources.wikipathways.release_date }}"
        )
        assert rendered == "2026-05-10"


def test_footer_renders_snapshot_block(app_with_manifest):
    client = app_with_manifest.test_client()
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Snapshot:" in html
    assert "WP&nbsp;2026-05-10" in html
    assert "GO&nbsp;2026-01-23" in html
    assert "Reactome&nbsp;v96" in html
    assert "AOP-Wiki&nbsp;2026-05-06" in html
    # Footer links to /stats#source-versions
    assert 'href="/stats#source-versions"' in html


def test_stats_page_has_source_versions_table(app_with_manifest):
    client = app_with_manifest.test_client()
    r = client.get("/stats")
    assert r.status_code == 200
    html = r.data.decode()
    # Anchor that the footer link points at
    assert 'id="source-versions"' in html
    # The Data sources section renders a live version-badge strip — one
    # badge per upstream resource. The badge labels are always present;
    # the version values are pulled from a live service so they are not
    # asserted here.
    assert "WikiPathways" in html
    assert "GO" in html
    assert "Reactome" in html
    assert "AOP-Wiki" in html
    assert "version-badge" in html


def test_footer_omits_snapshot_when_manifest_missing(tmp_path, monkeypatch):
    """If data/source_versions.json doesn't exist, the footer omits the
    snapshot block entirely (no broken-looking 'Snapshot:' label)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "data").mkdir()  # data dir exists but no manifest file
    monkeypatch.chdir(work)
    from app import create_app
    app = create_app()
    client = app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert "Snapshot:" not in r.data.decode()


# ---------- Zenodo deposit helpers ----------

@pytest.fixture
def _OK_MANIFEST():
    return {
        "captured_at": "2026-05-14T21:00:00Z",
        "sources": {
            "wikipathways": {
                "status": "ok", "release_date": "2026-05-10",
                "captured_at": "2026-05-14T21:00:00Z",
            },
            "gene_ontology": {
                "status": "ok", "release_date": "2026-01-23",
                "captured_at": "2026-05-14T21:00:00Z",
            },
            "reactome": {
                "status": "ok",
                "release_version": "96", "release_date": "2026-03-25",
                "captured_at": "2026-05-14T21:00:00Z",
            },
            "aopwiki": {
                "status": "ok", "snapshot_date": "2026-05-06",
                "captured_at": "2026-05-14T21:00:00Z",
            },
        },
    }


def test_slice_source_versions_returns_only_named_resources(_OK_MANIFEST):
    from scripts.publish_zenodo import _slice_source_versions
    s = _slice_source_versions(_OK_MANIFEST, "wikipathways", "aopwiki")
    assert set(s["sources"].keys()) == {"wikipathways", "aopwiki"}
    assert s["captured_at"] == _OK_MANIFEST["captured_at"]


def test_slice_source_versions_empty_when_no_match(_OK_MANIFEST):
    from scripts.publish_zenodo import _slice_source_versions
    assert _slice_source_versions(_OK_MANIFEST, "totally_unknown") == {}


def test_format_snapshot_table_md_renders_all_rows(_OK_MANIFEST):
    from scripts.publish_zenodo import _format_snapshot_table_md
    md = _format_snapshot_table_md(_OK_MANIFEST)
    assert "| Resource | Release | Captured |" in md
    assert "WikiPathways" in md and "2026-05-10" in md
    assert "Gene Ontology" in md and "2026-01-23" in md
    assert "Reactome" in md and "v96 (2026-03-25)" in md
    assert "AOP-Wiki" in md and "2026-05-06" in md


def test_format_snapshot_table_md_fallback_when_empty():
    from scripts.publish_zenodo import _format_snapshot_table_md
    assert "Snapshot manifest unavailable" in _format_snapshot_table_md({})
    assert "Snapshot manifest unavailable" in _format_snapshot_table_md(None)


def test_format_versions_for_prose_returns_single_line(_OK_MANIFEST):
    from scripts.publish_zenodo import _format_versions_for_prose
    line = _format_versions_for_prose(_OK_MANIFEST)
    assert "WP 2026-05-10" in line
    assert "GO 2026-01-23" in line
    assert "Reactome v96 (2026-03-25)" in line
    assert "AOP-Wiki 2026-05-06" in line


def test_format_versions_for_prose_skips_unknown_sources():
    from scripts.publish_zenodo import _format_versions_for_prose
    m = {"sources": {
        "wikipathways": {"status": "ok", "release_date": "2026-05-10"},
        "gene_ontology": {"status": "unknown"},
        "reactome": {"status": "unknown"},
        "aopwiki": {"status": "ok", "snapshot_date": "2026-05-06"},
    }}
    line = _format_versions_for_prose(m)
    assert "WP 2026-05-10" in line
    assert "AOP-Wiki 2026-05-06" in line
    assert "GO " not in line
    assert "Reactome" not in line


def test_build_readme_includes_snapshot_section(_OK_MANIFEST):
    from scripts.publish_zenodo import _build_readme
    out = _build_readme(
        "2026-05-15",
        {"All": 27, "High": 19, "Medium": 7, "Low": 1},
        {"All": 3, "High": 1, "Medium": 1, "Low": 1},
        {"All": 0, "High": 0, "Medium": 0, "Low": 0},
        source_versions=_OK_MANIFEST,
    ).decode()
    assert "## Upstream resource snapshot" in out
    assert "| WikiPathways | 2026-05-10 |" in out
    assert "v96 (2026-03-25)" in out
    # Also references the sidecar so consumers know it's in each ZIP
    assert "source_versions.json" in out


def test_build_metadata_description_contains_snapshot_line(_OK_MANIFEST):
    from scripts.publish_zenodo import _build_metadata
    meta = _build_metadata("2026-05-15", source_versions=_OK_MANIFEST)
    assert "Upstream snapshot for this deposit" in meta["description"]
    assert "WP 2026-05-10" in meta["description"]


def test_build_metadata_omits_snapshot_when_manifest_empty():
    from scripts.publish_zenodo import _build_metadata
    meta = _build_metadata("2026-05-15", source_versions={})
    assert "Upstream snapshot" not in meta["description"]


def test_resource_zip_includes_source_versions_sidecar(_OK_MANIFEST):
    """Each per-resource ZIP must carry a source_versions.json sidecar
    containing the relevant slice of the manifest, so GMT-only consumers
    can pin against an upstream release."""
    from scripts.publish_zenodo import _build_resource_zip, _slice_source_versions

    # Build a tiny KE-WP zip using stubbed generators so we don't hit SPARQL.
    # `confidence` is named explicitly: _build_resource_zip passes the exact
    # tier under that name since #206, and absorbing it into **_ would make
    # this stub emit content for every tier.
    def _stub_gmt(mappings, min_confidence=None, confidence=None, **_):
        return "KE_X\tdesc\tBRCA1\tTP53\n" if not (min_confidence or confidence) else ""

    def _stub_ttl(mappings):
        return "@prefix : <https://example/> .\n:m a :Thing .\n"

    raw = _build_resource_zip(
        "KE-WikiPathways", _stub_gmt, _stub_ttl, mappings=[], today="2026-05-15",
        source_versions_slice=_slice_source_versions(_OK_MANIFEST, "wikipathways", "aopwiki"),
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        assert "KE-WikiPathways/source_versions.json" in names
        sidecar = json.loads(zf.read("KE-WikiPathways/source_versions.json"))
        assert set(sidecar["sources"].keys()) == {"wikipathways", "aopwiki"}
        assert sidecar["sources"]["wikipathways"]["release_date"] == "2026-05-10"


def test_resource_zip_omits_sidecar_when_no_slice(_OK_MANIFEST):
    """If the caller passes no slice, no sidecar is emitted."""
    from scripts.publish_zenodo import _build_resource_zip

    def _stub_gmt(mappings, min_confidence=None, confidence=None, **_):
        return "row\n" if not (min_confidence or confidence) else ""

    def _stub_ttl(mappings):
        return "@prefix : <x> .\n"

    raw = _build_resource_zip(
        "KE-WikiPathways", _stub_gmt, _stub_ttl, mappings=[], today="2026-05-15",
        source_versions_slice=None,
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert "KE-WikiPathways/source_versions.json" not in zf.namelist()
