"""Unit tests for src/exporters/zenodo_assembly.py — the pure-Python
deposit assembly extracted from scripts/publish_zenodo.py (#158 follow-up).

These tests pin the v3 deposit shape: three per-resource ZIPs each
containing GMT-by-confidence + Turtle + optional source_versions.json
sidecar, plus a README that quantifies per-tier counts and pins the
upstream snapshot. The shape is what the canonical Zenodo deposit
(10.5281/zenodo.20184643 concept DOI) was minted with on 2026-05-14, and
the admin `publish_zenodo` route now reuses the same helpers so the
in-app trigger reproduces it.
"""
import io
import json
import zipfile


from src.exporters.gmt_exporter import _apply_confidence
from src.exporters.zenodo_assembly import (
    assemble_deposit_files,
    build_metadata,
    build_readme,
    build_resource_zip,
    changes_significant,
    counts,
    format_snapshot_table_md,
    format_versions_for_prose,
    slice_source_versions,
)


# ---------- counts() ----------

def test_counts_buckets_by_confidence():
    rows = [
        {"confidence_level": "High"}, {"confidence_level": "high"},
        {"confidence_level": "Medium"},
        {"confidence_level": "Low"}, {"confidence_level": "Low"},
        {"confidence_level": None}, {"confidence_level": "weird"},
    ]
    c = counts(rows)
    assert c == {"All": 7, "High": 2, "Medium": 1, "Low": 2}


def test_counts_empty_list():
    assert counts([]) == {"All": 0, "High": 0, "Medium": 0, "Low": 0}


# ---------- changes_significant() ----------

def _ctotals(wp=0, go=0, rx=0):
    return {
        "wp": {"All": wp, "High": 0, "Medium": 0, "Low": 0},
        "go": {"All": go, "High": 0, "Medium": 0, "Low": 0},
        "reactome": {"All": rx, "High": 0, "Medium": 0, "Low": 0},
    }


def test_changes_significant_no_prior_deposit():
    assert changes_significant(_ctotals(10, 5, 1), {}, min_delta=1) is True


def test_changes_significant_under_threshold():
    last = _ctotals(10, 5, 1)
    # +1 across all three resources = delta 1, under threshold 5
    assert changes_significant(_ctotals(11, 6, 2), last, min_delta=5) is False


def test_changes_significant_at_threshold():
    last = _ctotals(10, 5, 1)
    # +5 across resources = delta 5 = threshold
    assert changes_significant(_ctotals(13, 7, 4), last, min_delta=5) is True


# ---------- slice_source_versions() ----------

_MANIFEST = {
    "captured_at": "2026-05-14T08:00:00Z",
    "sources": {
        "wikipathways": {"status": "ok", "release_date": "2026-05-10"},
        "gene_ontology": {"status": "ok", "release_date": "2026-01-23"},
        "reactome": {"status": "ok", "release_version": "96", "release_date": "2026-03-15"},
        "aopwiki": {"status": "ok", "snapshot_date": "2026-05-06"},
    },
}


def test_slice_source_versions_picks_only_named_resources():
    s = slice_source_versions(_MANIFEST, "wikipathways", "aopwiki")
    assert set(s["sources"].keys()) == {"wikipathways", "aopwiki"}
    assert s["captured_at"] == "2026-05-14T08:00:00Z"


def test_slice_source_versions_empty_when_nothing_matches():
    assert slice_source_versions(_MANIFEST, "no_such_resource") == {}
    assert slice_source_versions({}, "wikipathways") == {}


# ---------- format_versions_for_prose() ----------

def test_format_versions_for_prose_includes_all_ok_sources():
    s = format_versions_for_prose(_MANIFEST)
    assert "WP 2026-05-10" in s
    assert "GO 2026-01-23" in s
    assert "Reactome v96 (2026-03-15)" in s
    assert "AOP-Wiki 2026-05-06" in s


def test_format_versions_for_prose_skips_non_ok_status():
    manifest = {"sources": {"wikipathways": {"status": "error"}}}
    assert format_versions_for_prose(manifest) == ""


# ---------- format_snapshot_table_md() ----------

def test_snapshot_table_renders_markdown_rows():
    md = format_snapshot_table_md(_MANIFEST)
    # Header + 4 resource rows
    assert "| Resource | Release | Captured |" in md
    assert "| WikiPathways | 2026-05-10 |" in md
    assert "| Reactome | v96 (2026-03-15) |" in md


def test_snapshot_table_fallback_when_no_sources():
    assert "Snapshot manifest unavailable" in format_snapshot_table_md(None)
    assert "Snapshot manifest unavailable" in format_snapshot_table_md({})


# ---------- build_resource_zip() ----------

def _fake_gmt(rows, min_confidence=None, confidence=None, **kwargs):
    """Return one TSV line per surviving row, or "" when none qualify — that
    empty string triggers the omit-empty-tier branch in build_resource_zip.

    Delegates to the production filters rather than reimplementing them. This
    stub previously carried its own exact-match copy of the filter *and*
    absorbed unknown kwargs into **kwargs, so when build_resource_zip switched
    from min_confidence= to confidence= (#206) it silently stopped filtering
    at all while the tests kept passing. A stub that defines the behaviour
    under test has to track the real thing.
    """
    rows = _apply_confidence(rows, min_confidence, confidence)
    if not rows:
        return ""
    return "\n".join(f"{r['ke_id']}\tdesc\t{r.get('wp_id') or r.get('go_id') or r.get('reactome_id')}" for r in rows) + "\n"


def _fake_ttl(rows):
    if not rows:
        return ""
    return "@prefix : <x:> .\n" + "\n".join(f":r{i} a :Row ." for i, _ in enumerate(rows))


def test_build_resource_zip_includes_all_tiers_when_present():
    rows = [
        {"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"},
        {"ke_id": "KE 2", "wp_id": "WP2", "confidence_level": "medium"},
        {"ke_id": "KE 3", "wp_id": "WP3", "confidence_level": "low"},
    ]
    blob = build_resource_zip("KE-WikiPathways", _fake_gmt, _fake_ttl, rows, "2026-05-14")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_All.gmt" in names
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_High.gmt" in names
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_Medium.gmt" in names
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_Low.gmt" in names
    assert "KE-WikiPathways/ke-wikipathways-mappings.ttl" in names


def test_build_resource_zip_omits_empty_tiers():
    """When a tier has zero rows, that _Level.gmt file must not appear."""
    rows = [{"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"}]
    blob = build_resource_zip("KE-WikiPathways", _fake_gmt, _fake_ttl, rows, "2026-05-14")
    names = set(zipfile.ZipFile(io.BytesIO(blob)).namelist())
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_High.gmt" in names
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_Medium.gmt" not in names
    assert "KE-WikiPathways/KE-WikiPathways_2026-05-14_Low.gmt" not in names


def test_build_resource_zip_attaches_sidecar_when_slice_present():
    rows = [{"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"}]
    sidecar = slice_source_versions(_MANIFEST, "wikipathways", "aopwiki")
    blob = build_resource_zip(
        "KE-WikiPathways", _fake_gmt, _fake_ttl, rows, "2026-05-14",
        source_versions_slice=sidecar,
    )
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert "KE-WikiPathways/source_versions.json" in zf.namelist()
    payload = json.loads(zf.read("KE-WikiPathways/source_versions.json"))
    assert set(payload["sources"].keys()) == {"wikipathways", "aopwiki"}


def test_build_resource_zip_omits_sidecar_when_slice_empty():
    rows = [{"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"}]
    blob = build_resource_zip(
        "KE-WikiPathways", _fake_gmt, _fake_ttl, rows, "2026-05-14",
        source_versions_slice={},
    )
    assert "KE-WikiPathways/source_versions.json" not in zipfile.ZipFile(io.BytesIO(blob)).namelist()


# ---------- build_readme() ----------

def test_readme_includes_per_tier_counts():
    body = build_readme(
        "2026-05-14",
        {"All": 125, "High": 79, "Medium": 44, "Low": 2},
        {"All": 7, "High": 2, "Medium": 5, "Low": 0},
        {"All": 2, "High": 1, "Medium": 1, "Low": 0},
        source_versions=_MANIFEST,
    ).decode("utf-8")
    assert "**125** total · 79 High · 44 Medium · 2 Low" in body
    assert "**7** total · 2 High · 5 Medium · 0 Low" in body
    assert "**2** total · 1 High · 1 Medium · 0 Low" in body
    assert "WikiPathways" in body and "Reactome" in body
    # Confirms snapshot table is embedded.
    assert "| Resource | Release | Captured |" in body


def test_readme_renders_fallback_when_manifest_missing():
    body = build_readme(
        "2026-05-14",
        {"All": 1, "High": 1, "Medium": 0, "Low": 0},
        {"All": 0, "High": 0, "Medium": 0, "Low": 0},
        {"All": 0, "High": 0, "Medium": 0, "Low": 0},
        source_versions=None,
    ).decode("utf-8")
    assert "Snapshot manifest unavailable" in body


# ---------- build_metadata() ----------

def test_build_metadata_includes_snapshot_in_description():
    md = build_metadata("2026-05-14", source_versions=_MANIFEST)
    assert "Upstream snapshot for this deposit:" in md["description"]
    assert "WP 2026-05-10" in md["description"]
    assert md["license"] == "cc-zero"
    assert md["publication_date"] == "2026-05-14"


def test_build_metadata_omits_snapshot_when_manifest_missing():
    md = build_metadata("2026-05-14")
    assert "Upstream snapshot for this deposit" not in md["description"]


# ---------- assemble_deposit_files() ----------
# This integrates the GMT / Turtle exporters. We pin only the file
# membership and per-resource shape; per-row content is covered by the
# exporters' own test suites.

def test_assemble_deposit_files_v3_layout(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.gmt_exporter.generate_ke_wp_gmt",
        lambda rows, min_confidence=None, **kw: _fake_gmt(rows, min_confidence),
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter.generate_ke_go_gmt",
        lambda rows, min_confidence=None, **kw: _fake_gmt(rows, min_confidence),
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter.generate_ke_reactome_gmt",
        lambda rows, min_confidence=None, **kw: _fake_gmt(rows, min_confidence),
    )
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_wp_turtle", _fake_ttl)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_go_turtle", _fake_ttl)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_reactome_turtle", _fake_ttl)

    wp = [{"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"}]
    go = [{"ke_id": "KE 1", "go_id": "GO:1", "confidence_level": "medium"}]
    rx = [{"ke_id": "KE 1", "reactome_id": "R-HSA-1", "confidence_level": "high"}]
    files = assemble_deposit_files("2026-05-14", wp, go, rx, source_versions=_MANIFEST)

    assert set(files.keys()) == {
        "KE-WikiPathways.zip", "KE-GO.zip", "KE-Reactome.zip", "README.md",
    }
    # Each ZIP is a non-empty bytes blob with the expected internal layout.
    for name in ("KE-WikiPathways.zip", "KE-GO.zip", "KE-Reactome.zip"):
        prefix = name[:-4]
        zf = zipfile.ZipFile(io.BytesIO(files[name]))
        ttl = f"{prefix}/{prefix.lower()}-mappings.ttl"
        assert ttl in zf.namelist(), f"{ttl} missing from {name}: {zf.namelist()}"
        # source_versions sidecar should pick the right resources per ZIP.
        sidecar_name = f"{prefix}/source_versions.json"
        assert sidecar_name in zf.namelist()


def test_assemble_deposit_files_skips_sidecar_with_no_manifest(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.gmt_exporter.generate_ke_wp_gmt",
        lambda rows, min_confidence=None, **kw: _fake_gmt(rows, min_confidence),
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter.generate_ke_go_gmt",
        lambda rows, min_confidence=None, **kw: _fake_gmt(rows, min_confidence),
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter.generate_ke_reactome_gmt",
        lambda rows, min_confidence=None, **kw: _fake_gmt(rows, min_confidence),
    )
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_wp_turtle", _fake_ttl)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_go_turtle", _fake_ttl)
    monkeypatch.setattr("src.exporters.rdf_exporter.generate_ke_reactome_turtle", _fake_ttl)

    wp = [{"ke_id": "KE 1", "wp_id": "WP1", "confidence_level": "high"}]
    files = assemble_deposit_files("2026-05-14", wp, [], [])

    for name in ("KE-WikiPathways.zip", "KE-GO.zip", "KE-Reactome.zip"):
        prefix = name[:-4]
        zf = zipfile.ZipFile(io.BytesIO(files[name]))
        assert f"{prefix}/source_versions.json" not in zf.namelist()
