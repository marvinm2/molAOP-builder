"""
Tests for scripts/capture_source_versions.py.

Covers the GO OBO parser (pure file I/O — happy path, missing file, malformed
header), the manifest writer / merger, and the failure-handling shape of the
HTTP-backed capturers (without hitting the network). The HTTP capturers are
exercised by monkeypatching `requests.post` / `requests.get` so the test
suite stays offline and deterministic.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import capture_source_versions as cap


@pytest.fixture
def tmp_obo(tmp_path: Path) -> Path:
    p = tmp_path / "go-basic.obo"
    p.write_text(
        "format-version: 1.2\n"
        "data-version: releases/2025-12-15\n"
        "subsetdef: foo\n",
        encoding="utf-8",
    )
    return p


# ---------- GO OBO parser ----------

def test_capture_gene_ontology_parses_data_version(tmp_obo: Path) -> None:
    rec = cap.capture_gene_ontology(tmp_obo)
    assert rec["status"] == "ok"
    assert rec["release_label"] == "releases/2025-12-15"
    assert rec["release_date"] == "2025-12-15"
    assert rec["method"] == "obo-header"
    assert "captured_at" in rec


def test_capture_gene_ontology_missing_file(tmp_path: Path) -> None:
    rec = cap.capture_gene_ontology(tmp_path / "does-not-exist.obo")
    assert rec["status"] == "unknown"
    assert "not found" in rec["reason"]


def test_capture_gene_ontology_falls_back_to_go_obo(tmp_path: Path) -> None:
    """When go-basic.obo is absent, the sibling go.obo is used (same header)."""
    (tmp_path / "go.obo").write_text(
        "format-version: 1.2\ndata-version: releases/2026-01-23\n",
        encoding="utf-8",
    )
    rec = cap.capture_gene_ontology(tmp_path / "go-basic.obo")
    assert rec["status"] == "ok"
    assert rec["release_date"] == "2026-01-23"


def test_capture_gene_ontology_no_data_version_line(tmp_path: Path) -> None:
    p = tmp_path / "no-data-version.obo"
    p.write_text("format-version: 1.2\nontology: go\n", encoding="utf-8")
    rec = cap.capture_gene_ontology(p)
    assert rec["status"] == "unknown"
    assert "no data-version" in rec["reason"]


def test_capture_gene_ontology_label_without_date(tmp_path: Path) -> None:
    """A non-ISO label still returns status=ok with release_date=None."""
    p = tmp_path / "weird-label.obo"
    p.write_text("data-version: snapshot-foo\n", encoding="utf-8")
    rec = cap.capture_gene_ontology(p)
    assert rec["status"] == "ok"
    assert rec["release_label"] == "snapshot-foo"
    assert rec["release_date"] is None


# ---------- HTTP capturers — offline via monkeypatch ----------

class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise cap.requests.HTTPError(f"http {self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_capture_wikipathways_extracts_yyyymmdd_from_iri(monkeypatch) -> None:
    payload = {"results": {"bindings": [
        {"dataset": {"value": "http://data.wikipathways.org/20260410/rdf/"}}
    ]}}
    monkeypatch.setattr(cap.requests, "post", lambda *a, **kw: _FakeResponse(payload))
    rec = cap.capture_wikipathways()
    assert rec["status"] == "ok"
    assert rec["release_date"] == "2026-04-10"
    assert rec["method"] == "sparql:void-dataset-iri"


def test_capture_wikipathways_returns_unknown_when_no_bindings(monkeypatch) -> None:
    monkeypatch.setattr(cap.requests, "post",
                        lambda *a, **kw: _FakeResponse({"results": {"bindings": []}}))
    rec = cap.capture_wikipathways()
    assert rec["status"] == "unknown"
    assert "no void:Dataset" in rec["reason"]


def test_capture_wikipathways_returns_unknown_on_network_error(monkeypatch) -> None:
    def boom(*a, **kw):
        raise cap.requests.ConnectionError("simulated dns failure")
    monkeypatch.setattr(cap.requests, "post", boom)
    rec = cap.capture_wikipathways()
    assert rec["status"] == "unknown"
    assert "wp sparql request failed" in rec["reason"]


def test_capture_reactome_uses_version_and_release_date(monkeypatch) -> None:
    payload = {"version": 96, "releaseDate": "2026-03-25"}
    monkeypatch.setattr(cap.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    rec = cap.capture_reactome()
    assert rec["status"] == "ok"
    assert rec["release_version"] == "96"
    assert rec["release_date"] == "2026-03-25"


def test_capture_reactome_returns_unknown_when_missing_version(monkeypatch) -> None:
    payload = {"name": "Reactome Knowledgebase"}  # no version
    monkeypatch.setattr(cap.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    rec = cap.capture_reactome()
    assert rec["status"] == "unknown"
    assert "missing version field" in rec["reason"]


def test_capture_aopwiki_uses_max_dcterms_modified(monkeypatch) -> None:
    payload = {"results": {"bindings": [
        {"latest": {"value": "2026-05-06T00:00:00Z"}}
    ]}}
    monkeypatch.setattr(cap.requests, "post", lambda *a, **kw: _FakeResponse(payload))
    rec = cap.capture_aopwiki()
    assert rec["status"] == "ok"
    assert rec["snapshot_date"] == "2026-05-06"


def test_capture_aopwiki_returns_unknown_when_both_queries_empty(monkeypatch) -> None:
    monkeypatch.setattr(cap.requests, "post",
                        lambda *a, **kw: _FakeResponse({"results": {"bindings": []}}))
    rec = cap.capture_aopwiki()
    assert rec["status"] == "unknown"


# ---------- Manifest assembly + merge ----------

def test_build_manifest_runs_all_sources_by_default(monkeypatch, tmp_obo: Path) -> None:
    monkeypatch.setattr(cap, "DEFAULT_OBO_PATH", tmp_obo)
    monkeypatch.setattr(cap, "capture_wikipathways",
                        lambda **kw: {"status": "ok", "release_date": "2026-05-10"})
    monkeypatch.setattr(cap, "capture_reactome",
                        lambda **kw: {"status": "ok", "release_version": "96"})
    monkeypatch.setattr(cap, "capture_aopwiki",
                        lambda **kw: {"status": "ok", "snapshot_date": "2026-05-06"})
    # Replace the dispatch table so the monkey-patched bare-name capturers are picked up.
    monkeypatch.setitem(cap.CAPTURERS, "wikipathways", cap.capture_wikipathways)
    monkeypatch.setitem(cap.CAPTURERS, "reactome", cap.capture_reactome)
    monkeypatch.setitem(cap.CAPTURERS, "aopwiki", cap.capture_aopwiki)

    manifest = cap.build_manifest(obo_path=tmp_obo)
    assert set(manifest["sources"]) == {"wikipathways", "gene_ontology", "reactome", "aopwiki"}
    assert all(rec["status"] == "ok" for rec in manifest["sources"].values())


def test_merge_with_existing_preserves_untouched_sources(tmp_path: Path) -> None:
    existing = tmp_path / "source_versions.json"
    existing.write_text(json.dumps({
        "captured_at": "2026-04-01T00:00:00Z",
        "sources": {
            "wikipathways": {"status": "ok", "release_date": "2026-03-15"},
            "gene_ontology": {"status": "ok", "release_date": "2025-12-15"},
        },
    }), encoding="utf-8")
    new_manifest = {
        "captured_at": "2026-05-14T21:00:00Z",
        "sources": {"gene_ontology": {"status": "ok", "release_date": "2026-01-23"}},
    }
    merged = cap._merge_with_existing(new_manifest, existing)
    # Old WP entry retained because this run didn't touch it.
    assert merged["sources"]["wikipathways"]["release_date"] == "2026-03-15"
    # New GO entry takes precedence over the old one.
    assert merged["sources"]["gene_ontology"]["release_date"] == "2026-01-23"
    # New capture timestamp is used.
    assert merged["captured_at"] == "2026-05-14T21:00:00Z"


def test_merge_with_existing_overwrites_when_unreadable(tmp_path: Path) -> None:
    existing = tmp_path / "source_versions.json"
    existing.write_text("not json", encoding="utf-8")
    new_manifest = {"captured_at": "now", "sources": {"x": {"status": "ok"}}}
    merged = cap._merge_with_existing(new_manifest, existing)
    assert merged == new_manifest


# ---------- CLI entry point ----------

def test_main_writes_manifest_file(monkeypatch, tmp_path: Path, tmp_obo: Path) -> None:
    out = tmp_path / "source_versions.json"
    monkeypatch.setattr(cap, "capture_wikipathways", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(cap, "capture_reactome", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(cap, "capture_aopwiki", lambda **kw: {"status": "ok"})
    monkeypatch.setitem(cap.CAPTURERS, "wikipathways", cap.capture_wikipathways)
    monkeypatch.setitem(cap.CAPTURERS, "reactome", cap.capture_reactome)
    monkeypatch.setitem(cap.CAPTURERS, "aopwiki", cap.capture_aopwiki)

    exit_code = cap.main(["--output", str(out), "--obo-path", str(tmp_obo)])
    assert exit_code == 0
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload["sources"]) == {"wikipathways", "gene_ontology", "reactome", "aopwiki"}


def test_main_strict_returns_2_on_unknown(monkeypatch, tmp_path: Path, tmp_obo: Path) -> None:
    out = tmp_path / "source_versions.json"
    monkeypatch.setattr(cap, "capture_wikipathways",
                        lambda **kw: {"status": "unknown", "reason": "x"})
    monkeypatch.setattr(cap, "capture_reactome", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(cap, "capture_aopwiki", lambda **kw: {"status": "ok"})
    monkeypatch.setitem(cap.CAPTURERS, "wikipathways", cap.capture_wikipathways)
    monkeypatch.setitem(cap.CAPTURERS, "reactome", cap.capture_reactome)
    monkeypatch.setitem(cap.CAPTURERS, "aopwiki", cap.capture_aopwiki)

    exit_code = cap.main(["--output", str(out), "--obo-path", str(tmp_obo), "--strict"])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# WikiPathways moved its dataset IRIs from http:// to https:// (#204)
# ---------------------------------------------------------------------------

def test_capture_wikipathways_accepts_https_dataset_iri(monkeypatch) -> None:
    """The live endpoint now serves https:// IRIs.

    The original SPARQL filter pinned the scheme with
    STRSTARTS(STR(?dataset), "http://data.wikipathways.org/"), so when upstream
    switched to https the query returned HTTP 200 with zero bindings. Nothing
    errored — the version badge simply read "unknown" indefinitely.
    """
    payload = {"results": {"bindings": [
        {"dataset": {"value": "https://data.wikipathways.org/20260710/rdf/"}}
    ]}}
    monkeypatch.setattr(cap.requests, "post", lambda *a, **kw: _FakeResponse(payload))

    rec = cap.capture_wikipathways()

    assert rec["status"] == "ok"
    assert rec["release_date"] == "2026-07-10"


def test_capture_wikipathways_query_does_not_pin_the_scheme() -> None:
    """Guard the query text itself, since a scheme-pinned filter fails silently.

    A regression here produces zero bindings rather than an exception, so no
    behavioural test on a mocked 200 response would catch it — only inspecting
    the query does.
    """
    import inspect

    source = inspect.getsource(cap.capture_wikipathways)
    filter_lines = [ln for ln in source.splitlines() if "FILTER" in ln]
    assert filter_lines, "capture_wikipathways no longer filters dataset IRIs"
    filter_line = filter_lines[0]

    assert "https?" in filter_line, (
        "the dataset-IRI filter must match either scheme; pinning http:// or "
        "https:// breaks silently when upstream switches"
    )


def test_capture_wikipathways_handles_non_rdf_suffix_iris(monkeypatch) -> None:
    """The endpoint exposes /rdf/, /smiles and /citedin for the same release.

    DESC ordering can return any of them; the release date is what matters and
    is identical across all three, so extraction must not assume the /rdf/ path.
    """
    payload = {"results": {"bindings": [
        {"dataset": {"value": "https://data.wikipathways.org/20260710/smiles"}}
    ]}}
    monkeypatch.setattr(cap.requests, "post", lambda *a, **kw: _FakeResponse(payload))

    rec = cap.capture_wikipathways()

    assert rec["status"] == "ok"
    assert rec["release_date"] == "2026-07-10"
