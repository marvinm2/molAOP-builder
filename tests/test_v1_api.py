"""
Tests for the public REST API v1 blueprint (/api/v1/).
"""
import os
import tempfile

import pytest

# Import app for test client construction (same pattern as conftest.py)
from app import app as flask_app
import src.blueprints.v1_api as v1_mod
from src.core.models import CacheModel, Database, GoMappingModel, MappingModel


# ---------------------------------------------------------------------------
# Per-test DB fixture — re-wires v1_api module-level models each test
# ---------------------------------------------------------------------------

@pytest.fixture
def v1_client():
    """
    Test client that re-wires the v1_api blueprint to a fresh temp-file DB.

    Each invocation:
    - Creates a new SQLite temp file DB with fully-migrated schema
    - Calls v1_mod.set_models() to replace the module-level singletons
    - Yields (test_client, mapping_model, go_mapping_model) so tests can seed data
    - Restores original module-level models after the test
    """
    fd, db_path = tempfile.mkstemp()

    db = Database(db_path)
    mm = MappingModel(db)
    gm = GoMappingModel(db)
    cm = CacheModel(db)

    # Save originals so we can restore after test
    orig_mm = v1_mod.mapping_model
    orig_gm = v1_mod.go_mapping_model
    orig_cm = v1_mod.cache_model

    # Inject fresh models
    v1_mod.set_models(mm, gm, cm)

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client, mm, gm

    # Restore originals
    v1_mod.set_models(orig_mm, orig_gm, orig_cm)

    os.close(fd)
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Seed helpers — operate on a given MappingModel / GoMappingModel instance
# ---------------------------------------------------------------------------

def _seed_mapping(mm, ke_id="KE 1", wp_id="WP123", confidence="High"):
    """Insert one approved KE-WP mapping.  Returns the row uuid."""
    mapping_id = mm.create_mapping(
        ke_id=ke_id,
        ke_title=f"Test KE {ke_id}",
        wp_id=wp_id,
        wp_title=f"Test Pathway {wp_id}",
        confidence_level=confidence,
        created_by="github:test_curator",
    )
    if mapping_id is None:
        return None

    conn = mm.db.get_connection()
    try:
        conn.execute(
            "UPDATE mappings SET approved_by_curator=?, approved_at_curator=? WHERE id=?",
            ("test_curator", "2026-01-01T00:00:00", mapping_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT uuid FROM mappings WHERE id=?", (mapping_id,)
        ).fetchone()
        return row["uuid"] if row else None
    finally:
        conn.close()


def _seed_go_mapping(gm, ke_id="KE 1", go_id="GO:0001234", go_name="test process",
                     confidence="High"):
    """Insert one approved KE-GO mapping.  Returns the row uuid."""
    mapping_id = gm.create_mapping(
        ke_id=ke_id,
        ke_title=f"Test KE {ke_id}",
        go_id=go_id,
        go_name=go_name,
        confidence_level=confidence,
        created_by="github:test_curator",
    )
    if mapping_id is None:
        return None

    conn = gm.db.get_connection()
    try:
        conn.execute(
            "UPDATE ke_go_mappings SET approved_by_curator=?, approved_at_curator=? WHERE id=?",
            ("test_curator", "2026-01-01T00:00:00", mapping_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT uuid FROM ke_go_mappings WHERE id=?", (mapping_id,)
        ).fetchone()
        return row["uuid"] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListMappings:
    def test_list_mappings_empty(self, v1_client):
        """GET /api/v1/mappings on empty DB returns 200, data=[], pagination.total=0."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/mappings")
        assert response.status_code == 200
        data = response.get_json()
        assert data["data"] == []
        assert data["pagination"]["total"] == 0

    def test_list_mappings_returns_json_envelope(self, v1_client):
        """Seed 1 mapping; GET returns 200 with correct envelope keys on each item."""
        client, mm, gm = v1_client
        _seed_mapping(mm, ke_id="KE T2", wp_id="WP_T2")

        response = client.get("/api/v1/mappings")
        assert response.status_code == 200
        data = response.get_json()
        assert "data" in data
        assert "pagination" in data
        assert len(data["data"]) == 1
        item = data["data"][0]
        for key in ("uuid", "ke_id", "ke_name", "pathway_id", "pathway_title",
                    "confidence_level", "provenance"):
            assert key in item, f"Missing key: {key}"

    def test_list_mappings_pagination_envelope(self, v1_client):
        """Pagination dict contains all required fields; single-page set has next/prev=None."""
        client, mm, gm = v1_client
        _seed_mapping(mm, ke_id="KE T3", wp_id="WP_T3")

        response = client.get("/api/v1/mappings")
        assert response.status_code == 200
        data = response.get_json()
        pagination = data["pagination"]
        for key in ("page", "per_page", "total", "total_pages", "next", "prev"):
            assert key in pagination, f"Missing pagination key: {key}"
        assert pagination["page"] == 1
        assert pagination["prev"] is None
        assert pagination["next"] is None

    def test_list_mappings_csv(self, v1_client):
        """Accept: text/csv returns 200 with text/csv content-type and 'uuid' in header."""
        client, mm, gm = v1_client
        _seed_mapping(mm, ke_id="KE T4", wp_id="WP_T4")

        response = client.get("/api/v1/mappings", headers={"Accept": "text/csv"})
        assert response.status_code == 200
        assert "text/csv" in response.content_type
        body = response.data.decode("utf-8")
        first_line = body.splitlines()[0]
        assert "uuid" in first_line

    def test_list_mappings_filter_ke_id(self, v1_client):
        """?ke_id=X returns only the mapping with ke_id=X, not others."""
        client, mm, gm = v1_client
        _seed_mapping(mm, ke_id="KE Filter5A", wp_id="WP_F5A")
        _seed_mapping(mm, ke_id="KE Filter5B", wp_id="WP_F5B")

        response = client.get("/api/v1/mappings?ke_id=KE Filter5A")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["data"]) == 1
        assert data["data"][0]["ke_id"] == "KE Filter5A"

    def test_list_mappings_filter_pathway_id(self, v1_client):
        """?pathway_id=WP_F6_P1 returns only WP_F6_P1 mapping."""
        client, mm, gm = v1_client
        _seed_mapping(mm, ke_id="KE Filter6A", wp_id="WP_F6_P1")
        _seed_mapping(mm, ke_id="KE Filter6B", wp_id="WP_F6_P2")

        response = client.get("/api/v1/mappings?pathway_id=WP_F6_P1")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["data"]) == 1
        assert data["data"][0]["pathway_id"] == "WP_F6_P1"

    def test_list_mappings_filter_confidence_level(self, v1_client):
        """?confidence_level=High returns only High confidence mappings."""
        client, mm, gm = v1_client
        _seed_mapping(mm, ke_id="KE Filter7A", wp_id="WP_F7A", confidence="High")
        _seed_mapping(mm, ke_id="KE Filter7B", wp_id="WP_F7B", confidence="Low")

        response = client.get("/api/v1/mappings?confidence_level=High")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["data"]) == 1
        assert data["data"][0]["confidence_level"].lower() == "high"

    def test_list_mappings_unknown_uuid(self, v1_client):
        """GET /api/v1/mappings/nonexistent-uuid returns 404 with 'error' key."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/mappings/nonexistent-uuid-000000")
        assert response.status_code == 404
        data = response.get_json()
        assert "error" in data

    def test_get_mapping_by_uuid(self, v1_client):
        """Seed 1 mapping; GET /api/v1/mappings/<uuid> returns 200 with matching uuid."""
        client, mm, gm = v1_client
        mapping_uuid = _seed_mapping(mm, ke_id="KE T9", wp_id="WP_T9")
        assert mapping_uuid is not None

        response = client.get(f"/api/v1/mappings/{mapping_uuid}")
        assert response.status_code == 200
        data = response.get_json()
        assert "data" in data
        assert data["data"]["uuid"] == mapping_uuid


class TestListGoMappings:
    def test_list_go_mappings_empty(self, v1_client):
        """GET /api/v1/go-mappings on empty DB returns 200 with data=[]."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/go-mappings")
        assert response.status_code == 200
        data = response.get_json()
        assert data["data"] == []

    def test_list_go_mappings_returns_json_envelope(self, v1_client):
        """Seed 1 GO mapping; response has go_term_id/go_term_name/go_namespace keys."""
        client, mm, gm = v1_client
        _seed_go_mapping(gm, ke_id="KE GO11", go_id="GO:0011111", go_name="test bp process")

        response = client.get("/api/v1/go-mappings")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["data"]) == 1
        item = data["data"][0]
        for key in ("go_term_id", "go_term_name", "go_namespace"):
            assert key in item, f"Missing key: {key}"
        assert item["go_namespace"] == "biological_process"

    def test_list_go_mappings_csv(self, v1_client):
        """Accept: text/csv returns 200 with text/csv content-type."""
        client, mm, gm = v1_client
        _seed_go_mapping(gm, ke_id="KE GO12", go_id="GO:0012121", go_name="csv test process")

        response = client.get("/api/v1/go-mappings", headers={"Accept": "text/csv"})
        assert response.status_code == 200
        assert "text/csv" in response.content_type
        body = response.data.decode("utf-8")
        first_line = body.splitlines()[0]
        assert "uuid" in first_line

    def test_get_go_mapping_by_uuid(self, v1_client):
        """Seed 1 GO mapping; GET /api/v1/go-mappings/<uuid> returns 200."""
        client, mm, gm = v1_client
        go_uuid = _seed_go_mapping(gm, ke_id="KE GO13", go_id="GO:0013131",
                                   go_name="test go 13")
        assert go_uuid is not None

        response = client.get(f"/api/v1/go-mappings/{go_uuid}")
        assert response.status_code == 200
        data = response.get_json()
        assert "data" in data
        assert data["data"]["uuid"] == go_uuid

    def test_get_go_mapping_unknown_uuid(self, v1_client):
        """GET /api/v1/go-mappings/nonexistent returns 404 with 'error' key."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/go-mappings/nonexistent-go-uuid-000")
        assert response.status_code == 404
        data = response.get_json()
        assert "error" in data

    def test_go_mapping_approved_by_non_null_after_approval(self, v1_client):
        """
        Verifies CURAT-01: a GO mapping created via the approval workflow
        (simulated by _seed_go_mapping with provenance) returns non-null
        approved_by and approved_at fields in GET /api/v1/go-mappings.
        """
        test_client, mm, gm = v1_client
        _seed_go_mapping(
            gm,
            ke_id="KE PROV1",
            go_id="GO:0001111",
            go_name="provenance test process",
        )

        response = test_client.get("/api/v1/go-mappings")
        assert response.status_code == 200
        data = response.get_json()
        items = data.get("data", [])
        assert len(items) == 1

        item = items[0]
        prov = item.get("provenance", {})
        assert prov.get("approved_by") is not None, (
            f"Expected non-null provenance.approved_by, got: {prov.get('approved_by')}"
        )
        assert prov.get("approved_at") is not None, (
            f"Expected non-null provenance.approved_at, got: {prov.get('approved_at')}"
        )
        assert prov["approved_by"] == "test_curator"


class TestCors:
    def test_cors_header_present(self, v1_client):
        """GET /api/v1/mappings includes Access-Control-Allow-Origin: *."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/mappings")
        assert response.status_code == 200
        assert response.headers.get("Access-Control-Allow-Origin") == "*"

    def test_cors_not_on_internal_routes(self, client):
        """GET /check (internal api_bp) does NOT carry Access-Control-Allow-Origin."""
        response = client.post("/check", data={"ke_id": "KE:0", "wp_id": "WP:0"})
        assert "Access-Control-Allow-Origin" not in response.headers


class TestPagination:
    def test_per_page_clamped_to_200(self, v1_client):
        """?per_page=999 results in per_page <= 200 in the pagination envelope."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/mappings?per_page=999")
        assert response.status_code == 200
        data = response.get_json()
        assert data["pagination"]["per_page"] <= 200

    def test_page_defaults_to_1(self, v1_client):
        """?page=abc (non-integer) falls back to page=1."""
        client, mm, gm = v1_client

        response = client.get("/api/v1/mappings?page=abc")
        assert response.status_code == 200
        data = response.get_json()
        assert data["pagination"]["page"] == 1


class TestAopFilter:
    def test_aop_id_invalid_returns_400(self, v1_client, monkeypatch):
        """?aop_id= with _resolve_aop_ke_ids raising ValueError returns 400 with 'error'."""
        client, mm, gm = v1_client

        def _raise_value_error(aop_id):
            raise ValueError("test error")

        monkeypatch.setattr(v1_mod, "_resolve_aop_ke_ids", _raise_value_error)

        response = client.get("/api/v1/mappings?aop_id=INVALID_NONEXISTENT_AOP_99999")
        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data


# ---------------------------------------------------------------------------
# Phase 34 ASMT-07 — WP serializer parity with Reactome
# ---------------------------------------------------------------------------

class TestAssessmentShape:
    """Phase 34 ASMT-07 — WP serializer parity with Reactome.

    Mirrors tests/test_v1_api_reactome.py::TestSerializer assertions against
    the WP serializer. The two resources MUST emit the same nested-object
    envelope (CONTEXT.md sibling-parity invariant).
    """

    def test_csv_fields_present(self):
        """Phase 34 fields appended at the END of _MAPPING_CSV_FIELDS."""
        _MAPPING_CSV_FIELDS = v1_mod._MAPPING_CSV_FIELDS

        for required in ("connection_type", "assessment_version",
                          "proposed_relationship", "proposed_basis",
                          "proposed_specificity", "proposed_coverage"):
            assert required in _MAPPING_CSV_FIELDS, (
                f"Required Phase 34 field missing from _MAPPING_CSV_FIELDS: "
                f"{required}"
            )
        # New columns must be at the END (back-compat for column-positional
        # consumers — see plan 34-04 task 1 step 3).
        tail = _MAPPING_CSV_FIELDS[-5:]
        assert tail == [
            "proposed_relationship", "proposed_basis", "proposed_specificity",
            "proposed_coverage", "assessment_version",
        ]

    def test_serialize_emits_assessment(self):
        """v2 row (all four answers + version='v2') round-trips through serializer."""
        _serialize_mapping = v1_mod._serialize_mapping

        row = {
            "uuid": "x", "ke_id": "KE 1", "ke_title": "t",
            "wp_id": "WP1", "wp_title": "p", "confidence_level": "High",
            "connection_type": "causative",
            "proposed_relationship": "causative",
            "proposed_basis": "known",
            "proposed_specificity": "specific",
            "proposed_coverage": "complete",
            "assessment_version": "v2",
        }
        out = _serialize_mapping(row)
        assert out["assessment"] == {
            "relationship": "causative",
            "basis": "known",
            "specificity": "specific",
            "coverage": "complete",
            "version": "v2",
        }

    def test_legacy_v1_serializes_with_null_answers(self):
        """Pre-Phase-34 row (no proposed_*, no assessment_version) → version='v1', NULLs."""
        _serialize_mapping = v1_mod._serialize_mapping

        row = {
            "uuid": "x", "ke_id": "KE 1", "ke_title": "t",
            "wp_id": "WP1", "wp_title": "p", "confidence_level": "Low",
            # No assessment_version, no proposed_* — simulates a v1 legacy row
            # before plan 34-01's migration ran (or a row whose answer columns
            # are all NULL after migration).
        }
        out = _serialize_mapping(row)
        assert out["assessment"]["version"] == "v1"
        assert out["assessment"]["relationship"] is None
        assert out["assessment"]["basis"] is None
        assert out["assessment"]["specificity"] is None
        assert out["assessment"]["coverage"] is None

    def test_flatten_for_csv_lifts_assessment(self):
        """_flatten_for_csv lifts the nested assessment object to top-level columns."""
        _flatten_for_csv = v1_mod._flatten_for_csv

        serialized = {
            "uuid": "x", "ke_id": "KE 1", "ke_name": "t",
            "pathway_id": "WP1", "pathway_title": "p",
            "confidence_level": "High",
            "connection_type": "causative",
            "ke_aop_context": ["AOP 1"],
            "ke_bio_level": None,
            "assessment": {
                "relationship": "causative",
                "basis": "known",
                "specificity": "specific",
                "coverage": "complete",
                "version": "v2",
            },
            "provenance": {
                "suggestion_score": 0.9,
                "approved_by": "alice",
                "approved_at": "2026-01-01",
                "proposed_by": "alice",
            },
        }
        flat = _flatten_for_csv(serialized)
        assert flat["proposed_relationship"] == "causative"
        assert flat["proposed_basis"] == "known"
        assert flat["proposed_specificity"] == "specific"
        assert flat["proposed_coverage"] == "complete"
        assert flat["assessment_version"] == "v2"
        # Nested object is removed from flat
        assert "assessment" not in flat

    def test_flatten_for_csv_handles_missing_assessment(self):
        """_flatten_for_csv defaults assessment_version='v1' when block is absent."""
        _flatten_for_csv = v1_mod._flatten_for_csv

        serialized = {
            "uuid": "x", "ke_id": "KE 1", "ke_name": "t",
            "pathway_id": "WP1", "pathway_title": "p",
            "confidence_level": "Low",
            "ke_aop_context": [],
            "provenance": {},
            # No 'assessment' key at all — defensive default test.
        }
        flat = _flatten_for_csv(serialized)
        assert flat["proposed_relationship"] is None
        assert flat["assessment_version"] == "v1"


# ---------------------------------------------------------------------------
# GET /api/v1/aops
# ---------------------------------------------------------------------------

@pytest.fixture
def aop_membership(request):
    """Install a small KE->AOP membership snapshot for the duration of a test.

    The real snapshot is a 1,500-KE precomputed file; these tests need a
    handful of KEs with known AOP membership, so the module global is swapped
    and restored rather than reading from disk.
    """
    original = v1_mod.ke_aop_membership
    v1_mod.ke_aop_membership = {
        # KE 1 sits in two AOPs; KE 2 only in AOP 10; KE 3 in neither mapped set
        "KE 1": [
            {"aop_id": "AOP 10", "aop_title": "Liver steatosis"},
            {"aop_id": "AOP 20", "aop_title": "Kidney failure"},
        ],
        "KE 2": [{"aop_id": "AOP 10", "aop_title": "Liver steatosis"}],
        "KE 3": [{"aop_id": "AOP 20", "aop_title": "Kidney failure"}],
    }
    yield v1_mod.ke_aop_membership
    v1_mod.ke_aop_membership = original


def test_aops_counts_kes_and_mappings_per_aop(v1_client, aop_membership):
    """Each AOP reports its total KEs and how many carry a mapping."""
    client, mm, gm = v1_client
    _seed_mapping(mm, ke_id="KE 1", wp_id="WP1")
    _seed_go_mapping(gm, ke_id="KE 2", go_id="GO:0000002")

    resp = client.get("/api/v1/aops")
    assert resp.status_code == 200
    by_id = {a["aop_id"]: a for a in resp.get_json()["data"]}

    # AOP 10 holds KE 1 (WP-mapped) and KE 2 (GO-mapped) — both mapped
    assert by_id["AOP 10"]["ke_count"] == 2
    assert by_id["AOP 10"]["mapped_ke_count"] == 2
    assert by_id["AOP 10"]["wikipathways_ke_count"] == 1
    assert by_id["AOP 10"]["go_ke_count"] == 1
    assert by_id["AOP 10"]["reactome_ke_count"] == 0

    # AOP 20 holds KE 1 (mapped) and KE 3 (unmapped)
    assert by_id["AOP 20"]["ke_count"] == 2
    assert by_id["AOP 20"]["mapped_ke_count"] == 1
    assert by_id["AOP 20"]["aop_title"] == "Kidney failure"


def test_aops_sorted_by_mapped_count_descending(v1_client, aop_membership):
    """Best-covered AOPs come first, which is the order the picker shows."""
    client, mm, gm = v1_client
    _seed_mapping(mm, ke_id="KE 1", wp_id="WP1")
    _seed_mapping(mm, ke_id="KE 2", wp_id="WP2")

    data = client.get("/api/v1/aops").get_json()["data"]
    assert [a["aop_id"] for a in data] == ["AOP 10", "AOP 20"]
    assert data[0]["mapped_ke_count"] >= data[1]["mapped_ke_count"]


def test_aops_mapped_only_filter(v1_client, aop_membership):
    """mapped_only drops AOPs no curator has touched."""
    client, mm, gm = v1_client
    _seed_mapping(mm, ke_id="KE 2", wp_id="WP2")  # only KE 2 -> only AOP 10

    all_ids = [a["aop_id"] for a in client.get("/api/v1/aops").get_json()["data"]]
    mapped_ids = [
        a["aop_id"]
        for a in client.get("/api/v1/aops?mapped_only=true").get_json()["data"]
    ]
    assert "AOP 20" in all_ids
    assert mapped_ids == ["AOP 10"]


def test_aops_query_filter_matches_title_and_id(v1_client, aop_membership):
    """q searches both the AOP ID and its title."""
    client, mm, gm = v1_client

    by_title = client.get("/api/v1/aops?q=kidney").get_json()["data"]
    assert [a["aop_id"] for a in by_title] == ["AOP 20"]

    by_id = client.get("/api/v1/aops?q=aop 10").get_json()["data"]
    assert [a["aop_id"] for a in by_id] == ["AOP 10"]


def test_aops_pagination_envelope(v1_client, aop_membership):
    """Pagination matches the envelope the other v1 collections return."""
    client, mm, gm = v1_client

    payload = client.get("/api/v1/aops?per_page=1").get_json()
    assert len(payload["data"]) == 1
    assert payload["pagination"]["total"] == 2
    assert payload["pagination"]["total_pages"] == 2
    assert payload["pagination"]["next"] is not None
    assert payload["pagination"]["prev"] is None


def test_aops_csv_format(v1_client, aop_membership):
    """?format=csv returns the flat per-resource columns."""
    client, mm, gm = v1_client
    _seed_mapping(mm, ke_id="KE 1", wp_id="WP1")

    resp = client.get("/api/v1/aops?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    body = resp.get_data(as_text=True)
    assert body.splitlines()[0] == (
        "aop_id,aop_title,ke_count,mapped_ke_count,"
        "wikipathways_ke_count,go_ke_count,reactome_ke_count"
    )


def test_aops_empty_when_membership_snapshot_missing(v1_client):
    """No snapshot means an empty list, not a 500.

    The snapshot is a precomputed file mounted at runtime, so a deployment
    without it must degrade to "no AOPs known" rather than breaking the API.
    """
    client, mm, gm = v1_client
    original = v1_mod.ke_aop_membership
    v1_mod.ke_aop_membership = None
    try:
        resp = client.get("/api/v1/aops")
        assert resp.status_code == 200
        assert resp.get_json()["data"] == []
    finally:
        v1_mod.ke_aop_membership = original
