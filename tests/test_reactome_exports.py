"""Unit tests for Reactome GMT generators (Phase 26 / plan 26-03).

Covers:
- _load_reactome_annotations file IO + missing-file fallback
- generate_ke_reactome_gmt per-mapping row format, dedup, confidence filter,
  empty input, and the no-direction-suffix invariant (Reactome has no direction).
- generate_ke_centric_reactome_gmt KE grouping, gene union/dedup, numeric KE
  sort, and confidence filter.
"""
import json

import pytest

from src.exporters.gmt_exporter import (
    _load_reactome_annotations,
    generate_ke_centric_reactome_gmt,
    generate_ke_reactome_gmt,
)


@pytest.fixture
def gene_annotations_file(tmp_path):
    """Tiny Reactome gene-annotations JSON fixture written to a tmp path.

    Overlap on TP53 between R-HSA-100 and R-HSA-200 lets us exercise dedup.
    R-HSA-999 is intentionally absent to exercise the missing-genes skip path.
    """
    annotations = {
        "R-HSA-100": ["TP53", "MDM2", "ATM"],
        "R-HSA-200": ["TP53", "BRCA1", "BRCA2"],  # overlaps with 100 on TP53
        "R-HSA-300": ["EGFR"],
    }
    path = tmp_path / "reactome_gene_annotations.json"
    path.write_text(json.dumps(annotations))
    return str(path)


@pytest.fixture
def sample_mappings():
    return [
        {
            "uuid": "u1",
            "ke_id": "KE 1",
            "ke_title": "Apoptosis",
            "reactome_id": "R-HSA-100",
            "pathway_name": "p53 signaling",
            "confidence_level": "High",
        },
        {
            "uuid": "u2",
            "ke_id": "KE 1",
            "ke_title": "Apoptosis",
            "reactome_id": "R-HSA-200",
            "pathway_name": "DNA repair",
            "confidence_level": "Medium",
        },
        {
            "uuid": "u3",
            "ke_id": "KE 5",
            "ke_title": "Cell proliferation",
            "reactome_id": "R-HSA-300",
            "pathway_name": "EGFR pathway",
            "confidence_level": "Low",
        },
        {
            "uuid": "u4",
            "ke_id": "KE 7",
            "ke_title": "Unmapped",
            "reactome_id": "R-HSA-999",
            "pathway_name": "Missing genes",
            "confidence_level": "High",
        },
    ]


# ---- _load_reactome_annotations ----------------------------------------------


def test_load_reactome_annotations_default_missing(tmp_path):
    out = _load_reactome_annotations(path=str(tmp_path / "nope.json"))
    assert out == {}


def test_load_reactome_annotations_reads_file(gene_annotations_file):
    out = _load_reactome_annotations(path=gene_annotations_file)
    assert "R-HSA-100" in out
    assert out["R-HSA-100"] == ["TP53", "MDM2", "ATM"]


# ---- generate_ke_reactome_gmt (per-mapping) ----------------------------------


def test_generate_ke_reactome_gmt_basic(sample_mappings, gene_annotations_file):
    out = generate_ke_reactome_gmt(sample_mappings, gene_annotations_path=gene_annotations_file)
    lines = [l for l in out.split("\n") if l]
    # u4 has no genes for R-HSA-999, so it is silently skipped -> 3 lines
    assert len(lines) == 3
    # Every line has >=3 tab-separated tokens (term, desc, >=1 gene)
    for l in lines:
        assert len(l.split("\t")) >= 3
    # First line shape: "KE1_..._R-HSA-100\tp53 signaling\tTP53\tMDM2\tATM"
    first = lines[0].split("\t")
    assert first[0].startswith("KE1_")
    assert first[0].endswith("_R-HSA-100")
    assert first[1] == "p53 signaling"
    assert "TP53" in first[2:]


def test_generate_ke_reactome_gmt_no_direction_suffix(sample_mappings, gene_annotations_file):
    out = generate_ke_reactome_gmt(sample_mappings, gene_annotations_path=gene_annotations_file)
    # Reactome must not emit "| direction:" anywhere (D-05).
    assert "| direction:" not in out


def test_generate_ke_reactome_gmt_min_confidence(sample_mappings, gene_annotations_file):
    out = generate_ke_reactome_gmt(
        sample_mappings, gene_annotations_path=gene_annotations_file, min_confidence="high"
    )
    lines = [l for l in out.split("\n") if l]
    # u1 (High) has genes; u4 (High) has no genes and is skipped -> 1 line
    assert len(lines) == 1
    assert "p53 signaling" in lines[0]


def test_generate_ke_reactome_gmt_empty():
    assert generate_ke_reactome_gmt([]) == ""


# ---- generate_ke_centric_reactome_gmt ----------------------------------------


def test_generate_ke_centric_reactome_gmt_unions_genes(sample_mappings, gene_annotations_file):
    out = generate_ke_centric_reactome_gmt(
        sample_mappings, gene_annotations_path=gene_annotations_file
    )
    lines = [l for l in out.split("\n") if l]
    # KE 1 has u1+u2 (3+3 genes, 1 overlap -> 5 unique); KE 5 has u3 (1 gene);
    # KE 7 has no genes -> skipped. Expect 2 lines.
    assert len(lines) == 2
    ke1 = next(l for l in lines if l.startswith("KE1\t"))
    ke1_tokens = ke1.split("\t")
    assert ke1_tokens[0] == "KE1"
    assert ke1_tokens[1] == "Apoptosis"
    # Genes deduplicated: TP53 should appear exactly once
    gene_tokens = ke1_tokens[2:]
    assert gene_tokens.count("TP53") == 1
    assert set(gene_tokens) == {"TP53", "MDM2", "ATM", "BRCA1", "BRCA2"}


def test_generate_ke_centric_reactome_gmt_sorts_by_ke_number(sample_mappings, gene_annotations_file):
    out = generate_ke_centric_reactome_gmt(
        sample_mappings, gene_annotations_path=gene_annotations_file
    )
    lines = [l for l in out.split("\n") if l]
    # KE1 must come before KE5 in numeric order
    assert lines[0].startswith("KE1\t")
    assert lines[1].startswith("KE5\t")


def test_generate_ke_centric_reactome_gmt_min_confidence(sample_mappings, gene_annotations_file):
    out = generate_ke_centric_reactome_gmt(
        sample_mappings, gene_annotations_path=gene_annotations_file, min_confidence="medium"
    )
    lines = [l for l in out.split("\n") if l]
    # Only u2 (Medium) survives the filter -> KE 1 with BRCA1/BRCA2/TP53
    assert len(lines) == 1
    assert lines[0].startswith("KE1\t")


# ---- generate_ke_reactome_turtle (RDF/Turtle) --------------------------------


from rdflib import Graph, Literal
from rdflib.namespace import DCTERMS, RDF, XSD

from src.exporters.rdf_exporter import (
    MAPPING,
    VOCAB,
    generate_ke_reactome_turtle,
)


def _row(uuid="u1", **overrides):
    base = {
        "uuid": uuid,
        "ke_id": "KE 1",
        "ke_title": "Apoptosis",
        "reactome_id": "R-HSA-100",
        "pathway_name": "p53 signaling",
        "species": "Homo sapiens",
        "confidence_level": "High",
        "suggestion_score": 0.9,
        "approved_by_curator": "github:alice",
        "approved_at_curator": "2026-01-01T00:00:00",
    }
    base.update(overrides)
    return base


def test_turtle_parses_with_rdflib():
    out = generate_ke_reactome_turtle([_row()])
    g = Graph()
    g.parse(data=out, format="turtle")
    subject = MAPPING["u1"]
    assert (subject, RDF.type, VOCAB.KeyEventReactomeMapping) in g
    assert (subject, VOCAB.keyEventId, Literal("KE 1")) in g
    assert (subject, VOCAB.reactomeId, Literal("R-HSA-100")) in g
    assert (subject, VOCAB.pathwayName, Literal("p53 signaling")) in g
    assert (subject, VOCAB.species, Literal("Homo sapiens")) in g
    assert (subject, VOCAB.confidenceLevel, Literal("High")) in g
    assert (subject, DCTERMS.creator, Literal("github:alice")) in g


def test_turtle_provenance_typed_literals():
    out = generate_ke_reactome_turtle([_row()])
    g = Graph()
    g.parse(data=out, format="turtle")
    subject = MAPPING["u1"]
    # dcterms:date is xsd:dateTime
    date_lit = Literal("2026-01-01T00:00:00", datatype=XSD.dateTime)
    assert (subject, DCTERMS.date, date_lit) in g
    # suggestionScore is xsd:decimal
    score_lit = Literal(0.9, datatype=XSD.decimal)
    assert (subject, VOCAB.suggestionScore, score_lit) in g


def test_turtle_no_go_predicates():
    out = generate_ke_reactome_turtle([_row()])
    # Predicates from GO must not appear
    assert "goDirection" not in out
    assert "goNamespace" not in out


def test_turtle_pathway_description_emitted_when_metadata_present():
    meta = {"R-HSA-100": {"description": "Tumor suppressor pathway"}}
    out = generate_ke_reactome_turtle([_row()], reactome_metadata=meta)
    g = Graph()
    g.parse(data=out, format="turtle")
    subject = MAPPING["u1"]
    assert (
        subject,
        VOCAB.pathwayDescription,
        Literal("Tumor suppressor pathway"),
    ) in g


def test_turtle_pathway_description_omitted_when_metadata_absent():
    out = generate_ke_reactome_turtle([_row()])
    g = Graph()
    g.parse(data=out, format="turtle")
    subject = MAPPING["u1"]
    # No triples with predicate pathwayDescription on this subject
    assert list(g.triples((subject, VOCAB.pathwayDescription, None))) == []


def test_turtle_min_confidence_filter():
    rows = [
        _row("u_high", confidence_level="High"),
        _row("u_med", confidence_level="Medium"),
    ]
    out = generate_ke_reactome_turtle(rows, min_confidence="high")
    g = Graph()
    g.parse(data=out, format="turtle")
    # Only u_high survives
    types = list(g.subjects(RDF.type, VOCAB.KeyEventReactomeMapping))
    assert types == [MAPPING["u_high"]]


def test_turtle_empty_input():
    out = generate_ke_reactome_turtle([])
    g = Graph()
    g.parse(data=out, format="turtle")
    # No rdf:type KeyEventReactomeMapping triples
    assert list(g.subjects(RDF.type, VOCAB.KeyEventReactomeMapping)) == []


# ---- Plan 26-06: route handlers + _get_or_generate_gmt extension --------------

import os
import tempfile

from app import app as flask_app
import src.blueprints.main as main_bp_mod
from src.core.models import Database, ReactomeMappingModel


@pytest.fixture
def export_seeded(tmp_path, monkeypatch):
    """Wire main blueprint Reactome model + metadata to a fresh temp-file DB
    with two seeded rows. Stubs the gene-annotations loader and clears any
    cached export files so each test exercises fresh generation."""
    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    rm = ReactomeMappingModel(db)

    rows = [
        {
            "uuid": "u1", "ke_id": "KE 1", "ke_title": "Apop",
            "reactome_id": "R-HSA-100", "pathway_name": "p53",
            "species": "Homo sapiens", "confidence_level": "High",
            "approved_by_curator": "github:a",
            "approved_at_curator": "2026-01-01T00:00:00",
            "suggestion_score": 0.9, "proposed_by": "github:a",
        },
        {
            "uuid": "u2", "ke_id": "KE 5", "ke_title": "Cell",
            "reactome_id": "R-HSA-200", "pathway_name": "DNA",
            "species": "Homo sapiens", "confidence_level": "Medium",
            "approved_by_curator": "github:b",
            "approved_at_curator": "2026-01-02T00:00:00",
            "suggestion_score": 0.7, "proposed_by": "github:b",
        },
    ]
    conn = rm.db.get_connection()
    try:
        for r in rows:
            cols = ",".join(r.keys())
            ph = ",".join(["?"] * len(r))
            conn.execute(
                f"INSERT INTO ke_reactome_mappings ({cols}) VALUES ({ph})",
                list(r.values()),
            )
        conn.commit()
    finally:
        conn.close()

    # Clear any cached export files so we exercise fresh generation
    for fname in ("ke-reactome-mappings.ttl",):
        p = main_bp_mod.EXPORT_CACHE_DIR / fname
        if p.exists():
            p.unlink()
    if main_bp_mod.EXPORT_CACHE_DIR.exists():
        for p in main_bp_mod.EXPORT_CACHE_DIR.glob("KE-REACTOME*.gmt"):
            p.unlink()

    # Stub gene annotations via monkeypatched gmt_exporter loader
    gene_annotations = {"R-HSA-100": ["TP53", "MDM2"], "R-HSA-200": ["BRCA1"]}
    from src.exporters import gmt_exporter
    monkeypatch.setattr(
        gmt_exporter,
        "_load_reactome_annotations",
        lambda path=None: gene_annotations,
    )

    # Wire model + metadata onto the main blueprint
    monkeypatch.setattr(main_bp_mod, "reactome_mapping_model", rm)
    monkeypatch.setattr(
        main_bp_mod,
        "reactome_metadata",
        {"R-HSA-100": {"description": "p53 pathway desc"}},
    )

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client

    os.close(fd)
    os.unlink(db_path)


def test_download_ke_reactome_gmt_route(export_seeded):
    client = export_seeded
    resp = client.get("/exports/gmt/ke-reactome")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    body = resp.get_data(as_text=True)
    assert "R-HSA-100" in body
    assert "TP53" in body
    # Format: KE{N}_{Slug}_R-HSA-XXX
    first_line = body.splitlines()[0]
    tokens = first_line.split("\t")
    assert tokens[0].startswith("KE1_") and tokens[0].endswith("_R-HSA-100")


def test_download_ke_reactome_gmt_min_confidence(export_seeded):
    client = export_seeded
    resp = client.get("/exports/gmt/ke-reactome?min_confidence=High")
    body = resp.get_data(as_text=True)
    # Only u1 (High) survives
    lines = [l for l in body.splitlines() if l]
    assert len(lines) == 1
    assert "p53" in lines[0]


def test_download_ke_reactome_centric_gmt_route(export_seeded):
    client = export_seeded
    resp = client.get("/exports/gmt/ke-reactome-centric")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    lines = [l for l in body.splitlines() if l]
    # KE 1 and KE 5, each one line
    assert any(l.startswith("KE1\t") for l in lines)
    assert any(l.startswith("KE5\t") for l in lines)


def test_download_ke_reactome_gmt_503_when_empty(client, monkeypatch):
    class _Empty:
        def get_all_mappings(self):
            return []
    monkeypatch.setattr(main_bp_mod, "reactome_mapping_model", _Empty())
    # Clear cache so the empty content is regenerated
    if main_bp_mod.EXPORT_CACHE_DIR.exists():
        for p in main_bp_mod.EXPORT_CACHE_DIR.glob("KE-REACTOME*.gmt"):
            p.unlink()
    resp = client.get("/exports/gmt/ke-reactome")
    assert resp.status_code == 503
    assert "No KE-Reactome mappings available" in resp.get_json()["error"]


def test_download_ke_reactome_rdf_route(export_seeded):
    client = export_seeded
    resp = client.get("/exports/rdf/ke-reactome")
    assert resp.status_code == 200
    assert resp.mimetype == "text/turtle"
    body = resp.get_data(as_text=True)
    g = Graph()
    g.parse(data=body, format="turtle")
    types = list(g.triples((None, RDF.type, VOCAB.KeyEventReactomeMapping)))
    assert len(types) == 2


def test_download_ke_reactome_rdf_pathway_description(export_seeded):
    client = export_seeded
    resp = client.get("/exports/rdf/ke-reactome")
    body = resp.get_data(as_text=True)
    # The "p53 pathway desc" string should appear because reactome_metadata is wired
    assert "p53 pathway desc" in body


def test_download_ke_reactome_rdf_503_when_empty(client, monkeypatch):
    class _Empty:
        def get_all_mappings(self):
            return []
    monkeypatch.setattr(main_bp_mod, "reactome_mapping_model", _Empty())
    # Clear RDF cache
    p = main_bp_mod.EXPORT_CACHE_DIR / "ke-reactome-mappings.ttl"
    if p.exists():
        p.unlink()
    resp = client.get("/exports/rdf/ke-reactome")
    assert resp.status_code == 503
    assert "No KE-Reactome mappings available" in resp.get_json()["error"]


def test_rdf_export_cache_reflects_new_mappings(export_seeded):
    """A mapping added after the first download must appear on the next one.

    Regression guard for the stale-cache half of #211. These caches were
    write-once — `if not cache_path.exists()` with no invalidation anywhere —
    so an approval after the first request was never reflected until a redeploy
    wiped the in-image cache directory. Verified in production on 2026-07-22:
    /exports/rdf/ke-go served 10 mappings while the database held 11.
    """
    client = export_seeded

    first = client.get("/exports/rdf/ke-reactome")
    assert first.status_code == 200
    g = Graph()
    g.parse(data=first.get_data(as_text=True), format="turtle")
    assert len(list(g.triples((None, RDF.type, VOCAB.KeyEventReactomeMapping)))) == 2

    rm = main_bp_mod.reactome_mapping_model
    conn = rm.db.get_connection()
    try:
        conn.execute(
            "INSERT INTO ke_reactome_mappings (uuid, ke_id, ke_title, reactome_id,"
            " pathway_name, species, confidence_level, approved_by_curator,"
            " approved_at_curator, suggestion_score, proposed_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("u3", "KE 9", "Necrosis", "R-HSA-300", "Regulated Necrosis",
             "Homo sapiens", "High", "github:c", "2026-01-03T00:00:00", 0.8,
             "github:c"),
        )
        conn.commit()
    finally:
        conn.close()

    second = client.get("/exports/rdf/ke-reactome")
    assert second.status_code == 200
    g2 = Graph()
    g2.parse(data=second.get_data(as_text=True), format="turtle")
    assert len(list(g2.triples((None, RDF.type, VOCAB.KeyEventReactomeMapping)))) == 3, (
        "The cached Turtle export did not pick up a newly inserted mapping"
    )
