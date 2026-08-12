"""Source-data versioning Phase E.1 — exporter surface tests.

Verifies that the upstream source-version columns added in Phase B and
stamped by Phase C / D actually flow through to:

- MappingModel / GoMappingModel / ReactomeMappingModel.get_all_mappings()
- The Turtle exporters (per-row vocab triples)
- The JSON exporter (schema fields + per-row values)
"""
import json
from pathlib import Path

import pytest

from src.exporters.namespaces import VOCAB_NS
from src.core.models import (
    Database,
    GoMappingModel,
    MappingModel,
    ReactomeMappingModel,
    ReactomeProposalModel,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "k.db"))


# ---------- get_all_mappings returns the new columns ----------

def test_wp_get_all_mappings_returns_version_columns(db):
    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 1", ke_title="t", wp_id="WP1", wp_title="t", created_by="github:u",
        wp_release_date="2026-05-10",
        aopwiki_snapshot_date="2026-05-06",
    )
    rows = mm.get_all_mappings()
    assert len(rows) == 1
    row = rows[0]
    assert row["wp_release_date"] == "2026-05-10"
    assert row["aopwiki_snapshot_date"] == "2026-05-06"


def test_go_get_all_mappings_returns_version_columns(db):
    gm = GoMappingModel(db)
    gm.create_mapping(
        ke_id="KE 2", ke_title="t", go_id="GO:0001", go_name="t",
        created_by="github:u",
        go_release_date="2026-01-23",
        aopwiki_snapshot_date="2026-05-06",
    )
    rows = gm.get_all_mappings()
    assert len(rows) == 1
    row = rows[0]
    assert row["go_release_date"] == "2026-01-23"
    assert row["aopwiki_snapshot_date"] == "2026-05-06"


def test_reactome_get_all_mappings_returns_version_columns(db):
    rpm = ReactomeProposalModel(db)
    rm = ReactomeMappingModel(db)
    pid = rpm.create_new_pair_reactome_proposal(
        ke_id="KE 3", ke_title="t", reactome_id="R-HSA-3", pathway_name="t",
        confidence_level="high", provider_username="github:u",
    )
    rm.create_approved_mapping(
        proposal_id=pid, approved_by_curator="admin",
        approved_at_curator="2026-05-14T21:00:00",
        reactome_release_version="96",
        reactome_release_date="2026-03-25",
        aopwiki_snapshot_date="2026-05-06",
    )
    rows = rm.get_all_mappings()
    assert len(rows) == 1
    row = rows[0]
    assert row["reactome_release_version"] == "96"
    assert row["reactome_release_date"] == "2026-03-25"
    assert row["aopwiki_snapshot_date"] == "2026-05-06"


def test_legacy_rows_have_null_version_columns(db):
    """Rows created without the new kwargs surface NULL in the new fields."""
    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 10", ke_title="t", wp_id="WP10", wp_title="t", created_by="github:u",
    )
    rows = mm.get_all_mappings()
    assert rows[0]["wp_release_date"] is None
    assert rows[0]["aopwiki_snapshot_date"] is None


# ---------- Turtle exporters emit version triples ----------

def test_ke_wp_turtle_emits_version_triples(db):
    from src.exporters.rdf_exporter import generate_ke_wp_turtle

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 1", ke_title="oxidative stress", wp_id="WP1", wp_title="apoptosis",
        confidence_level="high", created_by="github:u",
        wp_release_date="2026-05-10",
        aopwiki_snapshot_date="2026-05-06",
    )
    ttl = generate_ke_wp_turtle(mm.get_all_mappings())
    assert "wpReleaseDate" in ttl
    assert "2026-05-10" in ttl
    assert "aopWikiSnapshotDate" in ttl
    assert "2026-05-06" in ttl


def test_ke_wp_turtle_skips_null_version_triples(db):
    """A mapping without version data must not emit empty/Null triples."""
    from src.exporters.rdf_exporter import generate_ke_wp_turtle

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 2", ke_title="t", wp_id="WP2", wp_title="t",
        confidence_level="high", created_by="github:u",
    )
    ttl = generate_ke_wp_turtle(mm.get_all_mappings())
    assert "wpReleaseDate" not in ttl
    assert "aopWikiSnapshotDate" not in ttl


def test_ke_go_turtle_emits_version_triples(db):
    from src.exporters.rdf_exporter import generate_ke_go_turtle

    gm = GoMappingModel(db)
    gm.create_mapping(
        ke_id="KE 5", ke_title="t", go_id="GO:0006915", go_name="apoptotic process",
        confidence_level="high", created_by="github:u",
        go_release_date="2026-01-23",
        aopwiki_snapshot_date="2026-05-06",
    )
    ttl = generate_ke_go_turtle(gm.get_all_mappings())
    assert "goReleaseDate" in ttl
    assert "2026-01-23" in ttl
    assert "aopWikiSnapshotDate" in ttl
    assert "2026-05-06" in ttl


def test_ke_reactome_turtle_emits_version_triples(db):
    from src.exporters.rdf_exporter import generate_ke_reactome_turtle

    rpm = ReactomeProposalModel(db)
    rm = ReactomeMappingModel(db)
    pid = rpm.create_new_pair_reactome_proposal(
        ke_id="KE 7", ke_title="t", reactome_id="R-HSA-7", pathway_name="t",
        confidence_level="high", provider_username="github:u",
    )
    rm.create_approved_mapping(
        proposal_id=pid, approved_by_curator="admin",
        approved_at_curator="2026-05-14T21:00:00",
        reactome_release_version="96",
        reactome_release_date="2026-03-25",
        aopwiki_snapshot_date="2026-05-06",
    )
    ttl = generate_ke_reactome_turtle(rm.get_all_mappings())
    assert "reactomeReleaseVersion" in ttl
    assert "96" in ttl
    assert "reactomeReleaseDate" in ttl
    assert "2026-03-25" in ttl
    assert "aopWikiSnapshotDate" in ttl
    assert "2026-05-06" in ttl


def test_turtle_round_trip_with_rdflib(db):
    """Generated Turtle parses cleanly with rdflib (no malformed output)."""
    from rdflib import Graph
    from src.exporters.rdf_exporter import generate_ke_wp_turtle

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 1", ke_title="t", wp_id="WP1", wp_title="t",
        confidence_level="high", created_by="github:u",
        wp_release_date="2026-05-10", aopwiki_snapshot_date="2026-05-06",
    )
    ttl = generate_ke_wp_turtle(mm.get_all_mappings())
    g = Graph().parse(data=ttl, format="turtle")
    # Look for the two new predicates by IRI.
    from rdflib import URIRef
    # Built from the shared constant rather than a literal, so the namespace
    # cannot drift back to a domain nobody owns without this test moving too.
    wp_release = URIRef(f"{VOCAB_NS}wpReleaseDate")
    aop_snap = URIRef(f"{VOCAB_NS}aopWikiSnapshotDate")
    assert any(p == wp_release for _, p, _ in g)
    assert any(p == aop_snap for _, p, _ in g)


# ---------- JSON exporter includes the new fields ----------

def test_json_export_includes_version_fields_in_data_schema(db, monkeypatch):
    """The data_schema.fields block advertises the new columns to downstream
    consumers (so they can pick them up by name from the JSON envelope)."""
    from src.exporters.json_exporter import JSONExporter

    # JSONExporter needs a metadata manager — patch with a minimal stub.
    class _StubMeta:
        metadata = {"version": "test"}

        def get_current_metadata(self):
            return {}

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 1", ke_title="t", wp_id="WP1", wp_title="t", created_by="github:u",
        wp_release_date="2026-05-10",
        aopwiki_snapshot_date="2026-05-06",
    )

    # JSONExporter signature varies; instantiate via attribute injection to
    # avoid coupling the test to its constructor surface.
    exporter = JSONExporter.__new__(JSONExporter)
    exporter.db = db
    exporter.metadata = _StubMeta()
    # Stub the helper methods this test doesn't exercise.
    exporter._get_provenance_info = lambda: {}
    exporter._generate_statistics = lambda mappings: {"count": len(mappings)}

    raw = exporter.export(include_metadata=False, include_provenance=False)
    payload = json.loads(raw)

    field_names = {f["name"] for f in payload["data_schema"]["fields"]}
    assert "wp_release_date" in field_names
    assert "aopwiki_snapshot_date" in field_names

    # And the actual row values are present.
    assert len(payload["mappings"]) == 1
    row = payload["mappings"][0]
    assert row["wp_release_date"] == "2026-05-10"
    assert row["aopwiki_snapshot_date"] == "2026-05-06"


def test_no_export_mints_uris_on_the_unregistered_domain():
    """Issue #162 — ke-wp-mapping.org has no DNS record and is not ours.

    Every exporter used to mint URIs against it: the RDF vocabulary and mapping
    namespaces, the JSON-LD @id and dataset URI, and the download URLs advertised
    inside the JSON exports. Nothing at those URIs ever resolved, they named the
    project by a name it no longer has, and because the domain is unregistered a
    third party could have served content at this project's own identifiers.

    This guards the source tree rather than one namespace constant, because the
    original spread by being retyped in each new exporter.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((root / "src").rglob("*.py")) + list((root / "examples").rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".md", ".html"}:
            continue
        # The scheme-qualified form, so prose explaining why the domain was
        # abandoned (namespaces.py, and the comment in rdf_exporter.py) does not
        # trip the guard that exists because of it.
        if "https://ke-wp-mapping.org" in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        f"unregistered domain reintroduced in: {offenders}. Use the constants in "
        f"src/exporters/namespaces.py."
    )
