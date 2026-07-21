"""
Public REST API v1 Blueprint
Versioned, read-only, no authentication required.
Entirely separate from the internal api_bp.
"""
import csv
import hashlib
import io
import json
import logging
import math

import requests as requests_lib
from flask import Blueprint, jsonify, make_response, request

from src.utils.text import sanitize_log

logger = logging.getLogger(__name__)

v1_api_bp = Blueprint("v1_api", __name__, url_prefix="/api/v1")

# Module-level model references — set by app.py via set_models()
mapping_model = None
go_mapping_model = None
cache_model = None
ke_metadata_index = None
ke_aop_membership = None
go_hierarchy = None
go_bp_metadata = None
go_mf_metadata = None
reactome_mapping_model = None
reactome_metadata = None       # {reactome_id: {description: str, ...}}
reactome_gene_counts = None    # {reactome_id: int}


def set_models(mapping, go_mapping, cache, ke_meta_index=None,
               ke_aop_data=None, go_hier=None, go_bp_meta=None, go_mf_meta=None,
               reactome_mapping=None, reactome_meta=None, reactome_counts=None):
    """Inject model instances from create_app()."""
    global mapping_model, go_mapping_model, cache_model
    global ke_metadata_index, ke_aop_membership, go_hierarchy, go_bp_metadata, go_mf_metadata
    global reactome_mapping_model, reactome_metadata, reactome_gene_counts
    mapping_model = mapping
    go_mapping_model = go_mapping
    cache_model = cache
    ke_metadata_index = ke_meta_index
    ke_aop_membership = ke_aop_data
    go_hierarchy = go_hier
    go_bp_metadata = go_bp_meta
    go_mf_metadata = go_mf_meta
    reactome_mapping_model = reactome_mapping
    reactome_metadata = reactome_meta
    reactome_gene_counts = reactome_counts


# ---------------------------------------------------------------------------
# CORS — blueprint-scoped, does NOT affect internal api_bp
# ---------------------------------------------------------------------------

@v1_api_bp.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_pagination_params():
    """Parse and clamp ?page= and ?per_page= from request.args."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
        per_page = max(1, min(per_page, 200))
    except (ValueError, TypeError):
        per_page = 50
    return page, per_page


def _make_pagination(page, per_page, total, base_url, extra_params):
    """Build pagination envelope with absolute next/prev URLs."""
    total_pages = math.ceil(total / per_page) if per_page and total else 0

    from urllib.parse import urlencode

    def _page_url(p):
        params = {**extra_params, "page": p, "per_page": per_page}
        return f"{base_url}?{urlencode(params)}"

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "next": _page_url(page + 1) if page < total_pages else None,
        "prev": _page_url(page - 1) if page > 1 else None,
    }


def _serialize_mapping(row):
    """Convert a DB row dict to the v1 mapping object shape."""
    ke_id = row["ke_id"]

    # KE context enrichment
    aop_entries = (ke_aop_membership or {}).get(ke_id, [])
    ke_aop_context = [entry["aop_id"] for entry in aop_entries]

    ke_meta = (ke_metadata_index or {}).get(ke_id)
    ke_bio_level = ke_meta.get("biolevel") if ke_meta else None

    return {
        "uuid": row["uuid"],
        "ke_id": ke_id,
        "ke_name": row["ke_title"],
        "pathway_id": row["wp_id"],
        "pathway_title": row["wp_title"],
        "confidence_level": row["confidence_level"],
        "connection_type": row.get("connection_type"),
        "ke_aop_context": ke_aop_context,
        "ke_bio_level": ke_bio_level,
        # Phase 34 ASMT-07: nested assessment object — sibling parity with Reactome.
        # Legacy v1 rows (pre-Phase-34 migration) emit the same shape with all
        # four answer fields NULL and version='v1'.
        "assessment": {
            "relationship": row.get("proposed_relationship"),
            "basis": row.get("proposed_basis"),
            "specificity": row.get("proposed_specificity"),
            "coverage": row.get("proposed_coverage"),
            "version": row.get("assessment_version", "v1"),
        },
        "provenance": {
            "suggestion_score": row.get("suggestion_score"),
            "approved_by": row.get("approved_by_curator"),
            "approved_at": row.get("approved_at_curator"),
            "proposed_by": row.get("proposed_by"),
        },
    }


def _serialize_go_mapping(row):
    """Convert a DB row dict to the v1 GO mapping object shape."""
    ke_id = row["ke_id"]
    go_id = row["go_id"]

    # KE context enrichment
    aop_entries = (ke_aop_membership or {}).get(ke_id, [])
    ke_aop_context = [entry["aop_id"] for entry in aop_entries]

    ke_meta = (ke_metadata_index or {}).get(ke_id)
    ke_bio_level = ke_meta.get("biolevel") if ke_meta else None

    # GO hierarchy enrichment
    go_hier_entry = (go_hierarchy or {}).get(go_id)
    go_ic = round(go_hier_entry["ic_score"], 2) if go_hier_entry and go_hier_entry.get("ic_score") is not None else None
    go_depth = go_hier_entry.get("depth") if go_hier_entry else None

    go_bp_entry = (go_bp_metadata or {}).get(go_id)
    if go_bp_entry is None:
        go_mf_entry = (go_mf_metadata or {}).get(go_id)
        go_definition = go_mf_entry.get("definition") if go_mf_entry else None
    else:
        go_definition = go_bp_entry.get("definition")

    return {
        "uuid": row["uuid"],
        "ke_id": ke_id,
        "ke_name": row["ke_title"],
        "go_term_id": go_id,
        "go_term_name": row["go_name"],
        "go_namespace": row.get("go_namespace", "biological_process"),
        "confidence_level": row["confidence_level"],
        "go_direction": row.get("go_direction"),  # positive/negative/null
        "connection_type": row.get("connection_type"),
        "assessment_version": row.get("assessment_version", "v1"),
        "connection_score": row.get("connection_score"),   # null for v1 mappings
        "specificity_score": row.get("specificity_score"), # null for v1 mappings
        "evidence_score": row.get("evidence_score"),       # null for v1 mappings
        "ke_aop_context": ke_aop_context,
        "ke_bio_level": ke_bio_level,
        "go_definition": go_definition,
        "go_ic": go_ic,
        "go_depth": go_depth,
        "provenance": {
            "suggestion_score": row.get("suggestion_score"),
            "approved_by": row.get("approved_by_curator"),
            "approved_at": row.get("approved_at_curator"),
            "proposed_by": row.get("proposed_by"),
        },
    }


# CSV fieldnames — provenance is flattened (nested dicts don't serialize to CSV)
_MAPPING_CSV_FIELDS = [
    "uuid", "ke_id", "ke_name", "pathway_id", "pathway_title",
    "confidence_level", "suggestion_score", "approved_by", "approved_at", "proposed_by",
    "connection_type", "ke_aop_context", "ke_bio_level",
    # Phase 34 ASMT-08: assessment fields appended at END for back-compat with
    # column-positional CSV consumers.
    "proposed_relationship", "proposed_basis", "proposed_specificity",
    "proposed_coverage", "assessment_version",
]
_GO_MAPPING_CSV_FIELDS = [
    "uuid", "ke_id", "ke_name", "go_term_id", "go_term_name", "go_namespace",
    "confidence_level", "go_direction", "suggestion_score", "approved_by", "approved_at", "proposed_by",
    "connection_type", "ke_aop_context", "ke_bio_level", "go_definition", "go_ic", "go_depth",
]
_REACTOME_MAPPING_CSV_FIELDS = [
    "uuid", "ke_id", "ke_name", "reactome_id", "pathway_name", "species",
    "confidence_level", "suggestion_score", "approved_by", "approved_at", "proposed_by",
    "ke_aop_context", "ke_bio_level", "pathway_description", "reactome_gene_count",
    # Phase 34 ASMT-08: assessment fields appended at END for back-compat with
    # column-positional CSV consumers. NOTE: connection_type is intentionally
    # NOT included — ke_reactome_mappings has no such column (only
    # ke_reactome_proposals has proposed_connection_type).
    "proposed_relationship", "proposed_basis", "proposed_specificity",
    "proposed_coverage", "assessment_version",
]


def _flatten_for_csv(obj):
    """Flatten provenance + assessment nested dicts into the top-level object for CSV."""
    flat = dict(obj)
    prov = flat.pop("provenance", {})
    flat["suggestion_score"] = prov.get("suggestion_score")
    flat["approved_by"] = prov.get("approved_by")
    flat["approved_at"] = prov.get("approved_at")
    flat["proposed_by"] = prov.get("proposed_by")
    # Phase 34 ASMT-08: lift the assessment nested object to top-level CSV columns,
    # mirroring the provenance flattening pattern above.
    assess = flat.pop("assessment", {}) or {}
    flat["proposed_relationship"] = assess.get("relationship")
    flat["proposed_basis"] = assess.get("basis")
    flat["proposed_specificity"] = assess.get("specificity")
    flat["proposed_coverage"] = assess.get("coverage")
    flat["assessment_version"] = assess.get("version", "v1")
    # Convert ke_aop_context array to semicolon-separated string for CSV
    aop_ctx = flat.get("ke_aop_context")
    flat["ke_aop_context"] = ";".join(aop_ctx) if aop_ctx else ""
    return flat


def _serialize_reactome_mapping(row):
    """Convert a DB row dict to the v1 Reactome mapping object shape (Phase 26 D-05)."""
    ke_id = row["ke_id"]
    reactome_id = row["reactome_id"]

    # KE context enrichment (same plumbing as GO/WP)
    aop_entries = (ke_aop_membership or {}).get(ke_id, [])
    ke_aop_context = [entry["aop_id"] for entry in aop_entries]

    ke_meta = (ke_metadata_index or {}).get(ke_id)
    ke_bio_level = ke_meta.get("biolevel") if ke_meta else None

    # Reactome enrichment from precomputed JSON dicts
    rmeta = (reactome_metadata or {}).get(reactome_id) or {}
    pathway_description = rmeta.get("description")
    gene_count = (reactome_gene_counts or {}).get(reactome_id, 0)

    return {
        "uuid": row["uuid"],
        "ke_id": ke_id,
        "ke_name": row["ke_title"],
        "reactome_id": reactome_id,
        "pathway_name": row["pathway_name"],
        "species": row.get("species"),
        "confidence_level": row["confidence_level"],
        # NOTE: ke_reactome_mappings has no `connection_type` column (only
        # ke_reactome_proposals has `proposed_connection_type`). The Reactome
        # serializer therefore omits the top-level connection_type field; the
        # nested `assessment` block below carries the equivalent rubric data
        # for v1.6+ rows.
        "pathway_description": pathway_description,
        "reactome_gene_count": gene_count,
        "ke_aop_context": ke_aop_context,
        "ke_bio_level": ke_bio_level,
        # Phase 34 ASMT-07: nested assessment object — sibling parity with WP.
        # Legacy v1 rows (pre-Phase-34 migration) emit the same shape with all
        # four answer fields NULL and version='v1'.
        "assessment": {
            "relationship": row.get("proposed_relationship"),
            "basis": row.get("proposed_basis"),
            "specificity": row.get("proposed_specificity"),
            "coverage": row.get("proposed_coverage"),
            "version": row.get("assessment_version", "v1"),
        },
        "provenance": {
            "suggestion_score": row.get("suggestion_score"),
            "approved_by": row.get("approved_by_curator"),
            "approved_at": row.get("approved_at_curator"),
            "proposed_by": row.get("proposed_by"),
        },
    }


def _respond_collection(serialized_rows, pagination, csv_fields):
    """
    Return JSON or CSV based on Accept header or ?format=csv query param.
    JSON: {"data": [...], "pagination": {...}}
    CSV:  header row + data rows (provenance flattened)
    """
    format_param = request.args.get("format", "").lower()
    if format_param == "csv":
        use_csv = True
    else:
        best = request.accept_mimetypes.best_match(
            ["application/json", "text/csv"], default="application/json"
        )
        use_csv = best == "text/csv"

    if use_csv:
        flat_rows = [_flatten_for_csv(r) for r in serialized_rows]
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=csv_fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(flat_rows)
        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = "attachment; filename=ke_wp_mappings.csv"
        return response
    return jsonify({"data": serialized_rows, "pagination": pagination})


def _resolve_aop_ke_ids(aop_id):
    """
    Resolve aop_id to a list of KE ID strings using AOP-Wiki SPARQL + cache.

    Returns:
        list of ke_id strings — may be empty if AOP has no KEs in SPARQL
    Raises:
        ValueError — if SPARQL is unavailable or aop_id is not found
    """
    aop_label = f"AOP {aop_id}" if aop_id.isdigit() else aop_id
    cache_key = f"aop_kes_{aop_label}"
    query_hash = hashlib.md5(cache_key.encode()).hexdigest()
    endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

    cached = cache_model.get_cached_response(endpoint, query_hash)
    if cached:
        results = json.loads(cached)
        return [item["KElabel"] for item in results]

    sparql_query = f"""
    PREFIX aopo: <http://aopkb.org/aop_ontology#>
    PREFIX dc: <http://purl.org/dc/elements/1.1/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX nci: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
    PREFIX foaf: <http://xmlns.com/foaf/0.1/>

    SELECT DISTINCT ?ke ?keId ?keTitle ?biolevel ?kePage
    WHERE {{
        ?aop a aopo:AdverseOutcomePathway ;
             rdfs:label "{aop_label}" ;
             aopo:has_key_event ?ke .
        ?ke a aopo:KeyEvent ;
            rdfs:label ?keId ;
            dc:title ?keTitle ;
            foaf:page ?kePage .
        OPTIONAL {{ ?ke nci:C25664 ?biolevel }}
    }}
    ORDER BY ?keId
    """

    try:
        response = requests_lib.post(
            endpoint,
            data={"query": sparql_query},
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
    except requests_lib.exceptions.Timeout:
        raise ValueError("AOP-Wiki SPARQL timed out")
    except Exception as exc:
        raise ValueError(f"AOP-Wiki SPARQL unavailable: {exc}") from exc

    if response.status_code != 200:
        raise ValueError(f"AOP-Wiki SPARQL returned {response.status_code}")

    data = response.json()
    if "results" not in data or "bindings" not in data["results"]:
        raise ValueError("Invalid SPARQL response format")

    results = [
        {
            "KElabel": binding.get("keId", {}).get("value", ""),
            "KEtitle": binding.get("keTitle", {}).get("value", ""),
        }
        for binding in data["results"]["bindings"]
        if "keId" in binding and "keTitle" in binding
    ]

    # Cache for 24 hours (same TTL as api_bp.get_aop_kes)
    cache_model.cache_response(endpoint, query_hash, json.dumps(results), 24)
    return [item["KElabel"] for item in results]


# ---------------------------------------------------------------------------
# Routes: KE-WP Mappings
# ---------------------------------------------------------------------------

@v1_api_bp.route("/mappings", methods=["GET"])
def list_mappings():
    """
    GET /api/v1/mappings

    Query params (all optional, combinable):
      ke_id            — filter by KE ID (comma-separated for multiple)
      pathway_id       — filter by WikiPathways ID (comma-separated)
      confidence_level — filter by confidence level (High/Medium/Low, case-insensitive)
      aop_id           — filter to KEs belonging to this AOP (numeric or "AOP N")
      page             — page number (default 1)
      per_page         — results per page (default 50, max 200)

    Accept header:
      application/json (default) — returns {"data": [...], "pagination": {...}}
      text/csv                   — returns CSV with flattened provenance
    """
    page, per_page = _parse_pagination_params()

    # Filter params
    ke_id_raw = request.args.get("ke_id")
    pathway_id_raw = request.args.get("pathway_id")
    confidence_level = request.args.get("confidence_level")
    aop_id = request.args.get("aop_id")

    # Comma-separated multi-value support — use first value if single given
    ke_id = ke_id_raw.split(",")[0].strip() if ke_id_raw else None
    pathway_id = pathway_id_raw.split(",")[0].strip() if pathway_id_raw else None

    ke_ids = None  # None means "no AOP filter"; [] means "AOP valid but no KEs"
    if aop_id:
        try:
            ke_ids = _resolve_aop_ke_ids(aop_id.strip())
        except ValueError as exc:
            logger.warning("AOP resolution failed for '%s': %s", sanitize_log(aop_id), exc)
            return jsonify({"error": f"AOP ID not found or SPARQL unavailable: {aop_id}"}), 400

    try:
        rows, total = mapping_model.get_mappings_paginated(
            page=page,
            per_page=per_page,
            ke_id=ke_id,
            pathway_id=pathway_id,
            confidence_level=confidence_level,
            ke_ids=ke_ids,
        )
    except Exception as exc:
        logger.error("Error in list_mappings: %s", exc)
        return jsonify({"error": "Failed to retrieve mappings"}), 500

    serialized = [_serialize_mapping(r) for r in rows]
    base_url = request.url_root.rstrip("/") + "/api/v1/mappings"
    extra_params = {}
    if ke_id_raw:
        extra_params["ke_id"] = ke_id_raw
    if pathway_id_raw:
        extra_params["pathway_id"] = pathway_id_raw
    if confidence_level:
        extra_params["confidence_level"] = confidence_level
    if aop_id:
        extra_params["aop_id"] = aop_id
    pagination = _make_pagination(page, per_page, total, base_url, extra_params)

    return _respond_collection(serialized, pagination, _MAPPING_CSV_FIELDS)


@v1_api_bp.route("/mappings/<uuid>", methods=["GET"])
def get_mapping(uuid):
    """
    GET /api/v1/mappings/<uuid>

    Returns a single mapping by its stable UUID.
    Returns 404 if the UUID does not exist.
    """
    try:
        row = mapping_model.get_mapping_by_uuid(uuid)
    except Exception as exc:
        logger.error("Error in get_mapping uuid=%s: %s", sanitize_log(uuid), exc)
        return jsonify({"error": "Failed to retrieve mapping"}), 500

    if row is None:
        return jsonify({"error": f"Mapping not found: {uuid}"}), 404

    return jsonify({"data": _serialize_mapping(row)})


# ---------------------------------------------------------------------------
# Routes: KE-GO Mappings
# ---------------------------------------------------------------------------

@v1_api_bp.route("/go-mappings", methods=["GET"])
def list_go_mappings():
    """
    GET /api/v1/go-mappings

    Query params (all optional, combinable):
      ke_id            — filter by KE ID (comma-separated for multiple)
      go_term_id       — filter by GO term ID (comma-separated)
      confidence_level — filter by confidence level (High/Medium/Low, case-insensitive)
      direction        — filter by GO direction: "positive" or "negative"
      page             — page number (default 1)
      per_page         — results per page (default 50, max 200)

    Accept header:
      application/json (default) — returns {"data": [...], "pagination": {...}}
      text/csv                   — returns CSV with flattened provenance
    """
    page, per_page = _parse_pagination_params()

    ke_id_raw = request.args.get("ke_id")
    go_term_id_raw = request.args.get("go_term_id")
    confidence_level = request.args.get("confidence_level")
    direction = request.args.get("direction")

    if direction is not None and direction not in ("positive", "negative"):
        return jsonify({"error": "Invalid direction value. Must be 'positive' or 'negative'"}), 400

    ke_id = ke_id_raw.split(",")[0].strip() if ke_id_raw else None
    go_term_id = go_term_id_raw.split(",")[0].strip() if go_term_id_raw else None

    try:
        rows, total = go_mapping_model.get_go_mappings_paginated(
            page=page,
            per_page=per_page,
            ke_id=ke_id,
            go_term_id=go_term_id,
            confidence_level=confidence_level,
            direction=direction,
        )
    except Exception as exc:
        logger.error("Error in list_go_mappings: %s", exc)
        return jsonify({"error": "Failed to retrieve GO mappings"}), 500

    serialized = [_serialize_go_mapping(r) for r in rows]
    base_url = request.url_root.rstrip("/") + "/api/v1/go-mappings"
    extra_params = {}
    if ke_id_raw:
        extra_params["ke_id"] = ke_id_raw
    if go_term_id_raw:
        extra_params["go_term_id"] = go_term_id_raw
    if confidence_level:
        extra_params["confidence_level"] = confidence_level
    if direction:
        extra_params["direction"] = direction
    pagination = _make_pagination(page, per_page, total, base_url, extra_params)

    return _respond_collection(serialized, pagination, _GO_MAPPING_CSV_FIELDS)


@v1_api_bp.route("/go-mappings/<uuid>", methods=["GET"])
def get_go_mapping(uuid):
    """
    GET /api/v1/go-mappings/<uuid>

    Returns a single KE-GO mapping by its stable UUID.
    Returns 404 if the UUID does not exist.
    """
    try:
        row = go_mapping_model.get_go_mapping_by_uuid(uuid)
    except Exception as exc:
        logger.error("Error in get_go_mapping uuid=%s: %s", sanitize_log(uuid), exc)
        return jsonify({"error": "Failed to retrieve GO mapping"}), 500

    if row is None:
        return jsonify({"error": f"GO mapping not found: {uuid}"}), 404

    return jsonify({"data": _serialize_go_mapping(row)})


# ---------------------------------------------------------------------------
# Routes: KE-Reactome Mappings
# ---------------------------------------------------------------------------

@v1_api_bp.route("/reactome-mappings", methods=["GET"])
def list_reactome_mappings():
    """
    GET /api/v1/reactome-mappings

    Paginated list of approved KE-to-Reactome pathway mappings.

    Query params (all optional, combinable):
      ke_id            — filter by KE ID (comma-separated for multiple; first token used)
      reactome_id      — filter by Reactome stable ID (comma-separated; first token used)
      confidence_level — filter by confidence level (High/Medium/Low, case-insensitive)
      aop_id           — filter to KEs belonging to this AOP (numeric or "AOP N")
      page             — page number (default 1)
      per_page         — results per page (default 50, max 200)
      format           — "csv" to force CSV; default returns JSON

    Accept header:
      application/json (default) — {"data": [...], "pagination": {...}}
      text/csv                   — flattened provenance CSV
    """
    page, per_page = _parse_pagination_params()

    ke_id_raw = request.args.get("ke_id")
    reactome_id_raw = request.args.get("reactome_id")
    confidence_level = request.args.get("confidence_level")
    aop_id = request.args.get("aop_id")

    ke_id = ke_id_raw.split(",")[0].strip() if ke_id_raw else None
    reactome_id = reactome_id_raw.split(",")[0].strip() if reactome_id_raw else None

    # AOP filter: resolve AOP -> list of KE IDs (mirrors WP list_mappings, D-08).
    ke_ids = None  # None = no AOP filter; [] = AOP valid but no KEs
    if aop_id:
        try:
            ke_ids = _resolve_aop_ke_ids(aop_id.strip())
        except ValueError as exc:
            logger.warning(
                "AOP resolution failed for '%s': %s", sanitize_log(aop_id), exc
            )
            return jsonify(
                {"error": f"AOP ID not found or SPARQL unavailable: {aop_id}"}
            ), 400

    try:
        rows, total = reactome_mapping_model.get_reactome_mappings_paginated(
            page=page,
            per_page=per_page,
            ke_id=ke_id,
            reactome_id=reactome_id,
            confidence_level=confidence_level,
            ke_ids=ke_ids,
        )
    except Exception as exc:
        logger.error("Error in list_reactome_mappings: %s", exc)
        return jsonify({"error": "Failed to retrieve Reactome mappings"}), 500

    serialized = [_serialize_reactome_mapping(r) for r in rows]
    base_url = request.url_root.rstrip("/") + "/api/v1/reactome-mappings"
    extra_params = {}
    if ke_id_raw:
        extra_params["ke_id"] = ke_id_raw
    if reactome_id_raw:
        extra_params["reactome_id"] = reactome_id_raw
    if confidence_level:
        extra_params["confidence_level"] = confidence_level
    if aop_id:
        extra_params["aop_id"] = aop_id
    pagination = _make_pagination(page, per_page, total, base_url, extra_params)

    return _respond_collection(serialized, pagination, _REACTOME_MAPPING_CSV_FIELDS)


@v1_api_bp.route("/reactome-mappings/<uuid>", methods=["GET"])
def get_reactome_mapping(uuid):
    """
    GET /api/v1/reactome-mappings/<uuid>

    Returns a single KE-Reactome mapping by its stable UUID.
    Returns 404 if the UUID does not exist.
    """
    try:
        row = reactome_mapping_model.get_reactome_mapping_by_uuid(uuid)
    except Exception as exc:
        logger.error(
            "Error in get_reactome_mapping uuid=%s: %s", sanitize_log(uuid), exc
        )
        return jsonify({"error": "Failed to retrieve Reactome mapping"}), 500

    if row is None:
        return jsonify({"error": f"Reactome mapping not found: {uuid}"}), 404

    return jsonify({"data": _serialize_reactome_mapping(row)})


# ---------------------------------------------------------------------------
# Routes: AOPs
# ---------------------------------------------------------------------------

_AOP_CSV_FIELDS = [
    "aop_id", "aop_title", "ke_count", "mapped_ke_count",
    "wikipathways_ke_count", "go_ke_count", "reactome_ke_count",
]


def _build_aop_index():
    """Aggregate the KE->AOP membership snapshot into an AOP-keyed index.

    Inverts ``ke_aop_membership`` (KE label -> [{aop_id, aop_title}]) and
    cross-references the mapped KE IDs of each resource, so every AOP carries
    both its total KE count and how many of those KEs the curators have mapped.

    Returns:
        List of AOP dicts sorted by mapped_ke_count descending, then numeric
        AOP ID ascending.  Empty list if the membership snapshot is missing.
    """
    if not ke_aop_membership:
        logger.warning("ke_aop_membership is unavailable; /api/v1/aops will be empty")
        return []

    def _mapped_ids(model, label):
        # get_mapped_ke_ids() does not filter on approval, matching the
        # collection endpoints above: the mapping tables only ever receive
        # approved rows (pending work lives in the *_proposals tables). If that
        # ever changes, this and /mappings both start counting pending rows.
        if model is None:
            return set()
        try:
            return set(model.get_mapped_ke_ids())
        except Exception as exc:
            logger.warning("Could not read mapped KE IDs for %s: %s", label, exc)
            return set()

    wp_ids = _mapped_ids(mapping_model, "wikipathways")
    go_ids = _mapped_ids(go_mapping_model, "go")
    rx_ids = _mapped_ids(reactome_mapping_model, "reactome")
    any_ids = wp_ids | go_ids | rx_ids

    # aop_id -> {title, kes, wp, go, rx}
    index = {}
    for ke_id, entries in ke_aop_membership.items():
        for entry in entries or []:
            aop_id = entry.get("aop_id")
            if not aop_id:
                continue
            agg = index.setdefault(aop_id, {
                "aop_id": aop_id,
                "aop_title": entry.get("aop_title") or aop_id,
                "kes": set(), "wp": set(), "go": set(), "rx": set(),
            })
            agg["kes"].add(ke_id)
            if ke_id in wp_ids:
                agg["wp"].add(ke_id)
            if ke_id in go_ids:
                agg["go"].add(ke_id)
            if ke_id in rx_ids:
                agg["rx"].add(ke_id)

    aops = [
        {
            "aop_id": agg["aop_id"],
            "aop_title": agg["aop_title"],
            "ke_count": len(agg["kes"]),
            "mapped_ke_count": len(agg["kes"] & any_ids),
            "wikipathways_ke_count": len(agg["wp"]),
            "go_ke_count": len(agg["go"]),
            "reactome_ke_count": len(agg["rx"]),
        }
        for agg in index.values()
    ]

    def _sort_key(aop):
        try:
            numeric = int(str(aop["aop_id"]).split()[-1].replace("AOP:", ""))
        except (ValueError, IndexError):
            numeric = 10 ** 9
        return (-aop["mapped_ke_count"], numeric)

    aops.sort(key=_sort_key)
    return aops


@v1_api_bp.route("/aops", methods=["GET"])
def list_aops():
    """
    GET /api/v1/aops

    Lists the Adverse Outcome Pathways this instance knows about, each with its
    Key Event count and how many of those KEs carry approved mappings, broken
    down per resource.  Sorted by mapped_ke_count descending.

    Query params (all optional):
      mapped_only — "true" to return only AOPs with at least one mapped KE
      q           — case-insensitive substring filter over aop_id and aop_title
      page        — page number (default 1)
      per_page    — results per page (default 50, max 200)

    Accept header / ?format=csv behaves as for /mappings.

    AOP membership comes from the precomputed AOP-Wiki snapshot
    (data/ke_aop_membership.json), the same source behind the ke_aop_context
    field on mapping records — so it is as current as the last run of
    scripts/precompute_ke_aop_membership.py, not live SPARQL.
    """
    page, per_page = _parse_pagination_params()
    mapped_only = request.args.get("mapped_only", "").lower() in ("1", "true", "yes")
    q = (request.args.get("q") or "").strip().lower()

    try:
        aops = _build_aop_index()
    except Exception as exc:
        logger.error("Error in list_aops: %s", exc)
        return jsonify({"error": "Failed to retrieve AOPs"}), 500

    if mapped_only:
        aops = [a for a in aops if a["mapped_ke_count"] > 0]
    if q:
        aops = [
            a for a in aops
            if q in a["aop_id"].lower() or q in a["aop_title"].lower()
        ]

    total = len(aops)
    start = (page - 1) * per_page
    window = aops[start:start + per_page]

    base_url = request.url_root.rstrip("/") + "/api/v1/aops"
    extra_params = {}
    if mapped_only:
        extra_params["mapped_only"] = "true"
    if q:
        extra_params["q"] = request.args.get("q")
    pagination = _make_pagination(page, per_page, total, base_url, extra_params)

    return _respond_collection(window, pagination, _AOP_CSV_FIELDS)
