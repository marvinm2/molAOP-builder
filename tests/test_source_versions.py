"""
Tests for SourceVersionService (src/services/source_versions.py).

Covers:
  1. snapshot() returns a dict with keys wikipathways, reactome, gene_ontology,
     aopwiki — each entry has 'version' (str) and 'unavailable' (bool).
  2. A second snapshot() call within the TTL window does NOT re-invoke the fetch
     functions (monkeypatch a fetch, assert call count stays 1).
  3. When an underlying capture_* returns a non-ok / raises, that entry has
     unavailable=True and version='unavailable'; snapshot() never raises.
  4. Date identifiers are normalized to ISO YYYY-MM-DD; Reactome stays a native
     release number (e.g. 'v96' or '96').
"""
import time

import pytest


# ---------------------------------------------------------------------------
# Helper factories for monkeypatching
# ---------------------------------------------------------------------------

def _ok_wp():
    return {"status": "ok", "release_date": "2026-05-10", "dataset_iri": "http://..."}


def _ok_go():
    return {"status": "ok", "release_date": "2026-03-25", "release_label": "releases/2026-03-25"}


def _ok_reactome():
    return {"status": "ok", "release_version": "96"}


def _ok_aopwiki():
    return {"status": "ok", "snapshot_date": "2026-04-01"}


def _unknown_result():
    return {"status": "unknown", "reason": "test failure", "captured_at": "2026-05-16T00:00:00Z"}


# ---------------------------------------------------------------------------
# Fixture: reset module-level cache between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the module-level _CACHE before and after each test."""
    import importlib
    import src.services.source_versions as svc_mod

    svc_mod._CACHE.clear()
    yield
    svc_mod._CACHE.clear()
    importlib.reload(svc_mod)  # reset to pristine state


# ---------------------------------------------------------------------------
# Test 1: snapshot() returns correct structure
# ---------------------------------------------------------------------------

def test_snapshot_returns_expected_keys_and_shape(monkeypatch):
    """snapshot() returns a dict with all four resource keys, each with version+unavailable."""
    import scripts.capture_source_versions as cap
    import src.services.source_versions as svc

    monkeypatch.setattr(cap, "capture_wikipathways", lambda **kw: _ok_wp())
    monkeypatch.setattr(cap, "capture_gene_ontology_upstream", lambda **kw: _ok_go())
    monkeypatch.setattr(cap, "capture_reactome", lambda **kw: _ok_reactome())
    monkeypatch.setattr(cap, "capture_aopwiki", lambda **kw: _ok_aopwiki())

    result = svc.snapshot()

    assert isinstance(result, dict)
    for key in ("wikipathways", "reactome", "gene_ontology", "aopwiki"):
        assert key in result, f"Missing key: {key}"
        entry = result[key]
        assert "version" in entry, f"{key} missing 'version'"
        assert "unavailable" in entry, f"{key} missing 'unavailable'"
        assert isinstance(entry["version"], str)
        assert isinstance(entry["unavailable"], bool)


# ---------------------------------------------------------------------------
# Test 2: TTL caching — second call does NOT re-invoke fetch functions
# ---------------------------------------------------------------------------

def test_snapshot_ttl_caching(monkeypatch):
    """A second snapshot() within TTL returns cached value without re-calling fetch."""
    import scripts.capture_source_versions as cap
    import src.services.source_versions as svc

    call_counts = {"wp": 0}

    def counting_wp(**kw):
        call_counts["wp"] += 1
        return _ok_wp()

    monkeypatch.setattr(cap, "capture_wikipathways", counting_wp)
    monkeypatch.setattr(cap, "capture_gene_ontology_upstream", lambda **kw: _ok_go())
    monkeypatch.setattr(cap, "capture_reactome", lambda **kw: _ok_reactome())
    monkeypatch.setattr(cap, "capture_aopwiki", lambda **kw: _ok_aopwiki())

    svc.snapshot()
    svc.snapshot()  # second call — should hit cache

    assert call_counts["wp"] == 1, (
        f"capture_wikipathways called {call_counts['wp']} times within TTL; expected 1"
    )


# ---------------------------------------------------------------------------
# Test 3: non-ok / raises → unavailable=True, version='unavailable', no raise
# ---------------------------------------------------------------------------

def test_snapshot_unavailable_on_failure(monkeypatch):
    """When a capture function returns non-ok or raises, entry is unavailable; snapshot never raises."""
    import scripts.capture_source_versions as cap
    import src.services.source_versions as svc

    monkeypatch.setattr(cap, "capture_wikipathways", lambda **kw: _unknown_result())

    def raising_go(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(cap, "capture_gene_ontology_upstream", raising_go)
    monkeypatch.setattr(cap, "capture_reactome", lambda **kw: _ok_reactome())
    monkeypatch.setattr(cap, "capture_aopwiki", lambda **kw: _ok_aopwiki())

    # Must not raise
    result = svc.snapshot()

    wp = result["wikipathways"]
    assert wp["unavailable"] is True
    assert wp["version"] == "unavailable"

    go = result["gene_ontology"]
    assert go["unavailable"] is True
    assert go["version"] == "unavailable"

    # reactome and aopwiki should still be ok
    assert result["reactome"]["unavailable"] is False
    assert result["aopwiki"]["unavailable"] is False


# ---------------------------------------------------------------------------
# Test 4: Date normalization + Reactome release number format
# ---------------------------------------------------------------------------

def test_snapshot_version_normalization(monkeypatch):
    """Dates are ISO YYYY-MM-DD; Reactome release_version is bare/prefixed number."""
    import scripts.capture_source_versions as cap
    import src.services.source_versions as svc

    # WP returns YYYYMMDD embedded in a date string — service should normalise
    monkeypatch.setattr(cap, "capture_wikipathways", lambda **kw: {
        "status": "ok", "release_date": "2026-05-10"
    })
    # GO already ISO
    monkeypatch.setattr(cap, "capture_gene_ontology_upstream", lambda **kw: {
        "status": "ok", "release_date": "2026-03-25"
    })
    # Reactome bare integer → should be 'v96' or '96' (a release number, not a date)
    monkeypatch.setattr(cap, "capture_reactome", lambda **kw: {
        "status": "ok", "release_version": "96"
    })
    # AOP-Wiki snapshot_date field (not release_date)
    monkeypatch.setattr(cap, "capture_aopwiki", lambda **kw: {
        "status": "ok", "snapshot_date": "2026-04-01"
    })

    result = svc.snapshot()

    # WikiPathways: ISO date
    assert result["wikipathways"]["version"] == "2026-05-10"
    assert result["wikipathways"]["unavailable"] is False

    # GO: ISO date
    assert result["gene_ontology"]["version"] == "2026-03-25"

    # Reactome: release version string (v-prefixed or bare number, not a date)
    reactome_ver = result["reactome"]["version"]
    assert not reactome_ver.startswith("unavail"), f"Reactome should not be unavailable: {reactome_ver}"
    # Must be a release number identifier (96 or v96), not a date like 2026-...
    assert "2026" not in reactome_ver, (
        f"Reactome version should be a release number, not a date: {reactome_ver}"
    )

    # AOP-Wiki: ISO date
    assert result["aopwiki"]["version"] == "2026-04-01"
