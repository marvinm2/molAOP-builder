"""
Tests for the public REST API v1 Reactome surface (/api/v1/reactome-mappings).

Phase 26-05: Mirrors test_v1_api.py for the Reactome read endpoints.
"""
import os
import tempfile

import pytest

from app import app as flask_app
import src.blueprints.v1_api as v1_mod
from src.core.models import (
    CacheModel,
    Database,
    GoMappingModel,
    MappingModel,
    ReactomeMappingModel,
)


# ---------------------------------------------------------------------------
# Per-test DB fixture — re-wires v1_api module-level models each test
# ---------------------------------------------------------------------------

@pytest.fixture
def v1r_client():
    """Test client that wires v1_api blueprint to a fresh temp-file DB
    with the Reactome model + metadata + gene counts present.
    """
    fd, db_path = tempfile.mkstemp()

    db = Database(db_path)
    mm = MappingModel(db)
    gm = GoMappingModel(db)
    cm = CacheModel(db)
    rm = ReactomeMappingModel(db)

    # Save originals so we can restore after test
    orig = {
        "mm": v1_mod.mapping_model,
        "gm": v1_mod.go_mapping_model,
        "cm": v1_mod.cache_model,
        "rm": getattr(v1_mod, "reactome_mapping_model", None),
        "rmeta": getattr(v1_mod, "reactome_metadata", None),
        "rgc": getattr(v1_mod, "reactome_gene_counts", None),
    }

    # Inject fresh models with Reactome wiring
    v1_mod.set_models(
        mm, gm, cm,
        reactome_mapping=rm,
        reactome_meta={"R-HSA-100": {"description": "p53 sig description"}},
        reactome_counts={"R-HSA-100": 25, "R-HSA-200": 17},
    )

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client, rm

    # Restore
    v1_mod.set_models(
        orig["mm"], orig["gm"], orig["cm"],
        reactome_mapping=orig["rm"],
        reactome_meta=orig["rmeta"],
        reactome_counts=orig["rgc"],
    )

    os.close(fd)
    os.unlink(db_path)


def _seed_reactome(rm, rows):
    """Insert a list of approved KE-Reactome mappings via direct SQL."""
    conn = rm.db.get_connection()
    try:
        for r in rows:
            cols = ",".join(r.keys())
            placeholders = ",".join(["?"] * len(r))
            conn.execute(
                f"INSERT INTO ke_reactome_mappings ({cols}) VALUES ({placeholders})",
                list(r.values()),
            )
        conn.commit()
    finally:
        conn.close()


def _default_rows():
    """3 seed rows: KE 1+R-HSA-100 (High), KE 1+R-HSA-200 (Medium), KE 5+R-HSA-100 (Low)."""
    return [
        {
            "uuid": "u1", "ke_id": "KE 1", "ke_title": "Apoptosis",
            "reactome_id": "R-HSA-100", "pathway_name": "p53 signaling",
            "species": "Homo sapiens", "confidence_level": "High",
            "approved_by_curator": "github:alice",
            "approved_at_curator": "2026-01-01T00:00:00",
            "suggestion_score": 0.9, "proposed_by": "github:alice",
            "created_by": "github:alice",
        },
        {
            "uuid": "u2", "ke_id": "KE 1", "ke_title": "Apoptosis",
            "reactome_id": "R-HSA-200", "pathway_name": "DNA repair",
            "species": "Homo sapiens", "confidence_level": "Medium",
            "approved_by_curator": "github:alice",
            "approved_at_curator": "2026-01-02T00:00:00",
            "suggestion_score": 0.7, "proposed_by": "github:alice",
            "created_by": "github:alice",
        },
        {
            "uuid": "u3", "ke_id": "KE 5", "ke_title": "Cell prolif",
            "reactome_id": "R-HSA-100", "pathway_name": "p53 signaling",
            "species": "Homo sapiens", "confidence_level": "Low",
            "approved_by_curator": "github:bob",
            "approved_at_curator": "2026-01-03T00:00:00",
            "suggestion_score": 0.5, "proposed_by": "github:bob",
            "created_by": "github:bob",
        },
    ]


# ---------------------------------------------------------------------------
# Serializer / constants smoke tests (Task 1)
# ---------------------------------------------------------------------------

class TestSerializer:
    def test_csv_fields_constant(self):
        """Reactome CSV fields constant — Phase 34 ASMT-07 shape.

        Phase 34 added the four assessment-answer columns + assessment_version
        to the Reactome serializer (sibling parity with WP's nested assessment
        block). NOTE: connection_type is intentionally NOT a Reactome field —
        the ke_reactome_mappings table has no such column (only
        ke_reactome_proposals has proposed_connection_type). It stays in the
        forbidden tuple. The remaining forbidden entries guard against
        GO-specific fields leaking into the Reactome shape.
        """
        _REACTOME_MAPPING_CSV_FIELDS = v1_mod._REACTOME_MAPPING_CSV_FIELDS

        assert _REACTOME_MAPPING_CSV_FIELDS[0] == "uuid"
        assert "reactome_id" in _REACTOME_MAPPING_CSV_FIELDS
        assert "pathway_name" in _REACTOME_MAPPING_CSV_FIELDS
        assert "pathway_description" in _REACTOME_MAPPING_CSV_FIELDS
        assert "reactome_gene_count" in _REACTOME_MAPPING_CSV_FIELDS
        assert "ke_aop_context" in _REACTOME_MAPPING_CSV_FIELDS
        # No GO-specific or undefined-on-Reactome fields leak in
        for forbidden in ("go_term_id", "go_term_name", "go_namespace",
                          "go_direction", "go_definition", "go_ic", "go_depth",
                          "connection_type",  # absent from ke_reactome_mappings schema
                          "connection_score", "specificity_score", "evidence_score"):
            assert forbidden not in _REACTOME_MAPPING_CSV_FIELDS, (
                f"Forbidden field present: {forbidden}"
            )
        # Phase 34 ASMT-07: assessment_version + the four proposed_* answer
        # columns are now LEGITIMATE Reactome CSV fields (sibling parity with
        # WP's nested assessment block). Positive assertion locks the shape.
        for required in ("assessment_version",
                          "proposed_relationship", "proposed_basis",
                          "proposed_specificity", "proposed_coverage"):
            assert required in _REACTOME_MAPPING_CSV_FIELDS, (
                f"Required Phase 34 field missing: {required}"
            )

    def test_serialize_basic_shape(self):
        """Reactome serializer shape — Phase 34 ASMT-07 nested assessment object.

        Phase 34 lifted connection_type to a legitimate top-level Reactome key
        and added a nested 'assessment' object (sibling parity with WP).
        The remaining forbidden entries guard against GO-specific fields
        leaking into the Reactome shape.
        """
        _serialize_reactome_mapping = v1_mod._serialize_reactome_mapping

        row = {
            "uuid": "u1", "ke_id": "KE 1", "ke_title": "Apoptosis",
            "reactome_id": "R-HSA-100", "pathway_name": "p53 signaling",
            "species": "Homo sapiens", "confidence_level": "High",
            "suggestion_score": 0.9,
            "approved_by_curator": "github:alice",
            "approved_at_curator": "2026-01-01",
            "proposed_by": "github:alice",
        }
        out = _serialize_reactome_mapping(row)

        assert out["uuid"] == "u1"
        assert out["ke_id"] == "KE 1"
        assert out["ke_name"] == "Apoptosis"
        assert out["reactome_id"] == "R-HSA-100"
        assert out["pathway_name"] == "p53 signaling"
        assert out["species"] == "Homo sapiens"
        assert out["confidence_level"] == "High"
        assert out["provenance"]["suggestion_score"] == 0.9
        assert out["provenance"]["approved_by"] == "github:alice"
        assert out["provenance"]["approved_at"] == "2026-01-01"
        assert out["provenance"]["proposed_by"] == "github:alice"

        # GO-specific keys must not leak in. connection_type also forbidden —
        # ke_reactome_mappings has no such column (only ke_reactome_proposals
        # has proposed_connection_type).
        for forbidden in ("go_term_id", "go_term_name", "go_namespace",
                          "go_direction", "go_definition", "go_ic", "go_depth",
                          "connection_type",
                          "connection_score", "specificity_score", "evidence_score"):
            assert forbidden not in out, f"Forbidden key in output: {forbidden}"

        # Phase 34 ASMT-07 positive assertions: nested assessment object present;
        # legacy v1 row defaults to version='v1' with NULL answer fields.
        assert "assessment" in out
        assert out["assessment"]["version"] in ("v1", "v2")
        assert "relationship" in out["assessment"]
        assert "basis" in out["assessment"]
        assert "specificity" in out["assessment"]
        assert "coverage" in out["assessment"]
        # Legacy row (no proposed_* in input) → all four answer fields are NULL
        assert out["assessment"]["relationship"] is None
        assert out["assessment"]["basis"] is None
        assert out["assessment"]["specificity"] is None
        assert out["assessment"]["coverage"] is None
        assert out["assessment"]["version"] == "v1"

    def test_serialize_enrichment_fallback(self):
        """When metadata/counts globals are unset/None, serializer must not raise."""
        _serialize_reactome_mapping = v1_mod._serialize_reactome_mapping

        original_meta = v1_mod.reactome_metadata
        original_counts = v1_mod.reactome_gene_counts
        v1_mod.reactome_metadata = None
        v1_mod.reactome_gene_counts = None
        try:
            row = {
                "uuid": "u1", "ke_id": "KE 1", "ke_title": "Apoptosis",
                "reactome_id": "R-HSA-NEW", "pathway_name": "Unknown",
                "species": "Homo sapiens", "confidence_level": "High",
            }
            out = _serialize_reactome_mapping(row)
            assert out["pathway_description"] is None
            assert out["reactome_gene_count"] == 0
        finally:
            v1_mod.reactome_metadata = original_meta
            v1_mod.reactome_gene_counts = original_counts


# ---------------------------------------------------------------------------
# List endpoint tests (Task 2)
# ---------------------------------------------------------------------------

class TestListReactomeMappings:
    def test_list_reactome_mappings_paginated(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["pagination"]["total"] == 3
        assert len(body["data"]) == 3

    def test_list_reactome_mappings_filter_ke_id(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings?ke_id=KE+1")
        body = resp.get_json()
        assert body["pagination"]["total"] == 2
        assert {r["ke_id"] for r in body["data"]} == {"KE 1"}

    def test_list_reactome_mappings_filter_reactome_id(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings?reactome_id=R-HSA-100")
        body = resp.get_json()
        assert body["pagination"]["total"] == 2
        for r in body["data"]:
            assert r["reactome_id"] == "R-HSA-100"

    def test_list_reactome_mappings_filter_confidence(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings?confidence_level=High")
        body = resp.get_json()
        assert body["pagination"]["total"] == 1
        assert body["data"][0]["confidence_level"] == "High"

    def test_list_reactome_mappings_csv(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type
        first_line = resp.get_data(as_text=True).splitlines()[0]
        for field in (
            "uuid", "ke_id", "ke_name", "reactome_id", "pathway_name",
            "species", "confidence_level", "suggestion_score",
            "approved_by", "approved_at", "proposed_by",
            "ke_aop_context", "ke_bio_level", "pathway_description",
            "reactome_gene_count",
        ):
            assert field in first_line, f"CSV header missing field: {field}"

    def test_list_reactome_mappings_aop_filter(self, v1r_client, monkeypatch):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())
        monkeypatch.setattr(v1_mod, "_resolve_aop_ke_ids", lambda aid: ["KE 1"])

        resp = client.get("/api/v1/reactome-mappings?aop_id=42")
        body = resp.get_json()
        assert body["pagination"]["total"] == 2
        assert {r["ke_id"] for r in body["data"]} == {"KE 1"}

    def test_list_reactome_mappings_aop_invalid(self, v1r_client, monkeypatch):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        def _raise(_):
            raise ValueError("nope")

        monkeypatch.setattr(v1_mod, "_resolve_aop_ke_ids", _raise)

        resp = client.get("/api/v1/reactome-mappings?aop_id=99999")
        assert resp.status_code == 400
        assert "AOP ID not found" in resp.get_json()["error"]

    def test_response_shape(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings")
        item = resp.get_json()["data"][0]
        expected = {
            "uuid", "ke_id", "ke_name", "reactome_id", "pathway_name",
            "species", "confidence_level", "pathway_description",
            "reactome_gene_count", "ke_aop_context", "ke_bio_level",
            "provenance",
        }
        assert expected <= set(item.keys()), (
            f"Missing keys: {expected - set(item.keys())}"
        )
        assert {"suggestion_score", "approved_by", "approved_at", "proposed_by"} <= set(
            item["provenance"].keys()
        )

    def test_pagination_preserves_aop_id(self, v1r_client, monkeypatch):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())
        monkeypatch.setattr(
            v1_mod, "_resolve_aop_ke_ids", lambda aid: ["KE 1", "KE 5"]
        )

        resp = client.get(
            "/api/v1/reactome-mappings?aop_id=42&per_page=1&page=1"
        )
        body = resp.get_json()
        nxt = body["pagination"].get("next")
        assert nxt is not None and "aop_id=42" in nxt

    def test_cors_header_present(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    def test_enrichment_fields_present_when_known(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings?reactome_id=R-HSA-100")
        body = resp.get_json()
        assert body["data"]
        item = body["data"][0]
        assert item["pathway_description"] == "p53 sig description"
        assert item["reactome_gene_count"] == 25


# ---------------------------------------------------------------------------
# Single-uuid endpoint tests (Task 2)
# ---------------------------------------------------------------------------

class TestGetReactomeMapping:
    def test_get_reactome_mapping_found(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings/u1")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["uuid"] == "u1"
        assert data["reactome_id"] == "R-HSA-100"
        assert data["pathway_name"] == "p53 signaling"

    def test_get_reactome_mapping_not_found(self, v1r_client):
        client, rm = v1r_client
        _seed_reactome(rm, _default_rows())

        resp = client.get("/api/v1/reactome-mappings/nonexistent")
        assert resp.status_code == 404
        assert "Reactome mapping not found" in resp.get_json()["error"]


class TestReactomeCsvFullExport:
    """#291: a CSV export without paging params returns every matching row."""

    def test_csv_returns_every_row(self, v1r_client):
        client, rm = v1r_client
        n = 205  # above the per_page clamp of 200
        _seed_reactome(rm, [
            {
                "uuid": f"rx-{i}", "ke_id": f"KE {i}", "ke_title": f"KE {i}",
                "reactome_id": f"R-HSA-{i}", "pathway_name": f"pathway {i}",
                "confidence_level": "High",
            }
            for i in range(n)
        ])

        resp = client.get("/api/v1/reactome-mappings?format=csv")
        lines = resp.get_data(as_text=True).splitlines()
        assert len(lines) == n + 1  # header + every row

        payload = client.get("/api/v1/reactome-mappings").get_json()
        assert len(payload["data"]) == 50
        assert payload["pagination"]["total"] == n

    def test_csv_full_export_filters_paging_and_envelope(self, v1r_client):
        client, rm = v1r_client
        n = 205
        _seed_reactome(rm, [
            {
                "uuid": f"rx-{i}", "ke_id": f"KE {i}", "ke_title": f"KE {i}",
                "reactome_id": f"R-HSA-{i}", "pathway_name": f"pathway {i}",
                "confidence_level": "High" if i % 5 else "Low",
            }
            for i in range(n)
        ])
        low = sum(1 for i in range(n) if i % 5 == 0)

        resp = client.get("/api/v1/reactome-mappings?format=csv&confidence_level=low")
        assert len(resp.get_data(as_text=True).splitlines()) == low + 1
        assert resp.headers["X-Total-Count"] == str(low)
        assert resp.headers["Content-Disposition"] == (
            "attachment; filename=ke_reactome_mappings.csv"
        )
        assert resp.headers["Access-Control-Expose-Headers"] == "X-Total-Count"

        resp = client.get("/api/v1/reactome-mappings?format=csv&page=3")
        assert len(resp.get_data(as_text=True).splitlines()) == 50 + 1

        resp = client.get("/api/v1/reactome-mappings")
        assert resp.get_json()["pagination"] == {
            "page": 1,
            "per_page": 50,
            "total": n,
            "total_pages": 5,
            "next": "http://localhost/api/v1/reactome-mappings?page=2&per_page=50",
            "prev": None,
        }
        assert "X-Total-Count" not in resp.headers
