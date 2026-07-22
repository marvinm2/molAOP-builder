"""
Main Blueprint
Handles core application routes and page rendering
"""
import json as json_lib
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, current_app, make_response, redirect, render_template, send_file, send_from_directory, session, request, jsonify, url_for
from werkzeug.security import safe_join

from src.blueprints.admin import _get_admin_users
from src.services.monitoring import monitor_performance
from src.utils.text import sanitize_log

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)

# Global model instances (will be set by app initialization)
mapping_model = None
go_mapping_model = None
export_manager = None
metadata_manager = None
cache_model_ref = None
ker_adjacency = None
reactome_mapping_model = None
reactome_metadata = None  # {reactome_id: {description: str, ...}} — passed to the RDF generator for pathway_description triples

EXPORT_CACHE_DIR = Path("static/exports")

# ---------------------------------------------------------------------------
# Preview allowlist — the ONLY source of file paths opened by download_preview.
# Keys are (resource, format_name) tuples matching the URL parameters.
# Values are absolute paths; os.path.abspath resolves them relative to the
# application root at import time so the endpoint never constructs a path
# from user input.  Add new entries here to extend preview coverage.
# ---------------------------------------------------------------------------
_EXPORTS_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'exports'))

PREVIEW_ALLOWLIST = {
    # WikiPathways
    ("wp", "gmt"):          os.path.join(_EXPORTS_BASE, "KE-WP_2026-03-04_All.gmt"),
    ("wp", "gmt-centric"):  os.path.join(_EXPORTS_BASE, "KE-WP-CENTRIC_2026-03-04_All.gmt"),
    ("wp", "ttl"):          os.path.join(_EXPORTS_BASE, "ke-wp-mappings.ttl"),
    # Gene Ontology
    ("go", "gmt"):          os.path.join(_EXPORTS_BASE, "KE-GO_2026-03-04_All.gmt"),
    ("go", "gmt-centric"):  os.path.join(_EXPORTS_BASE, "KE-GO-CENTRIC_2026-03-04_All.gmt"),
    ("go", "ttl"):          os.path.join(_EXPORTS_BASE, "ke-go-mappings.ttl"),
    # Reactome
    ("reactome", "gmt"):    os.path.join(_EXPORTS_BASE, "KE-REACTOME_2026-03-04_All.gmt"),
    ("reactome", "ttl"):    os.path.join(_EXPORTS_BASE, "ke-reactome-mappings.ttl"),
    # CSV/JSON previews are not file-cached; preview is intentionally unavailable.
    # Add ("wp","csv") etc. here once a static export file exists.
}

# Precomputed OECD development-status map — gitignored, lives on Gluster mount.
# Degrades to {} when absent (e.g. fresh CI checkout) — never raises.
oecd_status_data = {}
_oecd_status_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'aop_oecd_status.json')
try:
    with open(_oecd_status_path, 'r') as _f:
        oecd_status_data = json_lib.load(_f).get('aops', {})
except (FileNotFoundError, json_lib.JSONDecodeError):
    oecd_status_data = {}

# KE-to-AOP membership map for the Explore page AOP-context column.
# Shape: {"KE 1": [{"aop_id": "AOP 1", "aop_title": "..."}], ...}
# Degrades to {} when the file is absent (CI / fresh checkout).
ke_aop_membership = {}
_ke_aop_membership_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ke_aop_membership.json')
try:
    with open(_ke_aop_membership_path, 'r') as _f:
        ke_aop_membership = json_lib.load(_f)
except (FileNotFoundError, json_lib.JSONDecodeError):
    ke_aop_membership = {}


def set_models(mapping, export_mgr=None, metadata_mgr=None, go_mapping=None, cache_model=None, ker_adjacency_data=None,
               reactome_mapping=None, reactome_meta=None):
    """Set the model instances"""
    global mapping_model, export_manager, metadata_manager, go_mapping_model, cache_model_ref, ker_adjacency
    global reactome_mapping_model, reactome_metadata
    mapping_model = mapping
    export_manager = export_mgr
    metadata_manager = metadata_mgr
    go_mapping_model = go_mapping
    cache_model_ref = cache_model
    ker_adjacency = ker_adjacency_data
    reactome_mapping_model = reactome_mapping
    reactome_metadata = reactome_meta


def get_mapping_stats():
    """
    Compute aggregate mapping statistics from all three mapping tables.
    Returns a dict with wp_total, go_total, reactome_total, total,
    wp_by_confidence, go_by_confidence, reactome_by_confidence.
    """
    stats = {
        "wp_total": 0,
        "go_total": 0,
        "reactome_total": 0,
        "total": 0,
        "wp_by_confidence": {},
        "go_by_confidence": {},
        "reactome_by_confidence": {},
    }
    try:
        conn = mapping_model.db.get_connection()
        try:
            stats["wp_total"] = conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]
            for row in conn.execute(
                "SELECT LOWER(confidence_level), COUNT(*) FROM mappings GROUP BY LOWER(confidence_level)"
            ).fetchall():
                stats["wp_by_confidence"][row[0]] = row[1]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to query mapping stats: %s", e)
    try:
        if go_mapping_model:
            conn = go_mapping_model.db.get_connection()
            try:
                stats["go_total"] = conn.execute("SELECT COUNT(*) FROM ke_go_mappings").fetchone()[0]
                for row in conn.execute(
                    "SELECT LOWER(confidence_level), COUNT(*) FROM ke_go_mappings GROUP BY LOWER(confidence_level)"
                ).fetchall():
                    stats["go_by_confidence"][row[0]] = row[1]
            finally:
                conn.close()
    except Exception as e:
        logger.warning("Failed to query GO mapping stats: %s", e)
    try:
        if reactome_mapping_model:
            conn = reactome_mapping_model.db.get_connection()
            try:
                stats["reactome_total"] = conn.execute(
                    "SELECT COUNT(*) FROM ke_reactome_mappings"
                ).fetchone()[0]
                for row in conn.execute(
                    "SELECT LOWER(confidence_level), COUNT(*) FROM ke_reactome_mappings"
                    " GROUP BY LOWER(confidence_level)"
                ).fetchall():
                    stats["reactome_by_confidence"][row[0]] = row[1]
            finally:
                conn.close()
    except Exception as e:
        logger.warning("Failed to query Reactome mapping stats: %s", e)
    stats["total"] = stats["wp_total"] + stats["go_total"] + stats["reactome_total"]
    return stats


def is_admin(username: str = None) -> bool:
    """
    Check if a user has admin privileges

    Args:
        username: Username to check (defaults to current session user)

    Returns:
        True if user is admin, False otherwise
    """
    if not username:
        username = session.get("user", {}).get("username")

    return username in _get_admin_users()


@main_bp.route("/")
@monitor_performance
def landing():
    """Public landing page — mission-first funnel with hero, four headline counts, CTAs."""
    stats = get_mapping_stats() if mapping_model else {
        "wp_total": 0,
        "go_total": 0,
        "reactome_total": 0,
        "total": 0,
        "wp_by_confidence": {},
        "go_by_confidence": {},
        "reactome_by_confidence": {},
    }
    return render_template("landing.html", stats=stats)


@main_bp.route("/mapper")
@monitor_performance
def mapper():
    """Main mapping application page (formerly at /)."""
    return render_template("index.html")


@main_bp.route("/explore")
@monitor_performance
def explore():
    """Dataset exploration page"""
    try:
        user_info = session.get("user", {})
        go_data = []
        if go_mapping_model:
            try:
                go_data = go_mapping_model.get_all_mappings()
            except Exception as e:
                logger.warning("Failed to load GO mappings: %s", e)
        reactome_count = 0
        try:
            if reactome_mapping_model:
                reactome_count = len(reactome_mapping_model.get_all_mappings())
        except Exception as e:
            logger.warning("Failed to load Reactome mapping count: %s", e)
            reactome_count = 0
        wp_count = 0
        try:
            if mapping_model:
                wp_count = len(mapping_model.get_all_mappings())
        except Exception as e:
            logger.warning("Failed to load WP mapping count: %s", e)
            wp_count = 0
        return render_template(
            "explore.html",
            go_dataset=go_data,
            user_info=user_info,
            reactome_count=reactome_count,
            wp_count=wp_count,
            ke_aop_membership=ke_aop_membership,
        )
    except Exception as e:
        logger.error("Error loading dataset: %s", e)
        return render_template(
            "explore.html",
            go_dataset=[],
            user_info={},
            reactome_count=0,
            wp_count=0,
            ke_aop_membership={},
            error="Failed to load dataset",
        )


@main_bp.route("/download")
def download():
    """Generate and download comprehensive dataset with metadata"""
    try:
        import csv
        import io
        from datetime import datetime
        from src.utils.timezone import format_export_timestamp

        # Get all mappings from database
        mappings = mapping_model.get_all_mappings()

        if not mappings:
            logger.warning("No mappings found for download")
            return (
                render_template("error.html", error="No data available for download"),
                404,
            )

        # Create CSV content in memory
        output = io.StringIO()

        # Generate statistics
        confidence_stats = {}
        connection_stats = {}
        contributor_stats = {}

        for mapping in mappings:
            conf = mapping.get("confidence_level", "unknown")
            conn = mapping.get("connection_type", "unknown")
            contrib = mapping.get("created_by", "anonymous")

            confidence_stats[conf] = confidence_stats.get(conf, 0) + 1
            connection_stats[conn] = connection_stats.get(conn, 0) + 1
            contributor_stats[contrib] = contributor_stats.get(contrib, 0) + 1

        # Add comprehensive metadata header
        current_time = format_export_timestamp()
        output.write(f"# KE-WP Mapping Dataset Export\n")
        output.write(f"# Generated: {current_time}\n")
        output.write(f"# Total mappings: {len(mappings)}\n")
        output.write(
            f"# Unique Key Events: {len(set(m.get('ke_id') for m in mappings if m.get('ke_id')))}\n"
        )
        output.write(
            f"# Unique WikiPathways: {len(set(m.get('wp_id') for m in mappings if m.get('wp_id')))}\n"
        )
        output.write(f"# Contributors: {len(contributor_stats)}\n")
        output.write(f"#\n")
        output.write(f"# Confidence distribution:\n")
        for conf, count in sorted(confidence_stats.items()):
            output.write(f"#   {conf}: {count} ({count/len(mappings)*100:.1f}%)\n")
        output.write(f"#\n")
        output.write(f"# Connection type distribution:\n")
        for conn, count in sorted(connection_stats.items()):
            output.write(f"#   {conn}: {count} ({count/len(mappings)*100:.1f}%)\n")
        output.write(f"#\n")
        output.write(
            f"# Data sources: AOP-Wiki SPARQL (https://aopwiki.rdf.bigcat-bioinformatics.org/sparql), WikiPathways SPARQL (https://sparql.wikipathways.org/sparql)\n"
        )
        output.write(
            f"# Description: Curated mappings between Key Events and WikiPathways with confidence assessments\n"
        )
        output.write(f"# License: CC0 - Public Domain\n")
        output.write(f"# Repository: https://github.com/marvinm2/molAOP-builder\n")
        output.write(f"# Contact: Generated from the Molecular AOP Builder\n")
        output.write(f"#\n")
        output.write(f"# Column descriptions:\n")
        output.write(f"# - id: Unique identifier for the mapping\n")
        output.write(f"# - ke_id: Key Event identifier from AOP-Wiki\n")
        output.write(f"# - ke_title: Full title of the Key Event\n")
        output.write(f"# - wp_id: WikiPathways identifier\n")
        output.write(f"# - wp_title: Full title of the WikiPathways pathway\n")
        output.write(
            f"# - connection_type: Type of relationship (causative, responsive, other, undefined)\n"
        )
        output.write(f"# - confidence_level: Expert assessment (high, medium, low)\n")
        output.write(f"# - created_by: GitHub username of contributor\n")
        output.write(f"# - created_at: Timestamp when mapping was created\n")
        output.write(f"# - updated_at: Timestamp when mapping was last updated\n")
        output.write(
            f"# - updated_by: GitHub username of last updater (if different from creator)\n"
        )
        output.write(f"#\n")

        # Define CSV columns
        fieldnames = [
            "id",
            "ke_id",
            "ke_title",
            "wp_id",
            "wp_title",
            "connection_type",
            "confidence_level",
            "created_by",
            "created_at",
            "updated_at",
            "updated_by",
        ]

        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        # Write mapping data
        for mapping in mappings:
            # Convert database row to dictionary and handle None values
            row_data = {
                "id": mapping.get("id"),
                "ke_id": mapping.get("ke_id"),
                "ke_title": mapping.get("ke_title", ""),
                "wp_id": mapping.get("wp_id"),
                "wp_title": mapping.get("wp_title", ""),
                "connection_type": mapping.get("connection_type"),
                "confidence_level": mapping.get("confidence_level"),
                "created_by": mapping.get("created_by", ""),
                "created_at": mapping.get("created_at", ""),
                "updated_at": mapping.get("updated_at", ""),
                "updated_by": mapping.get("updated_by", mapping.get("created_by", "")),
            }
            writer.writerow(row_data)

        # Prepare file for download
        output.seek(0)
        csv_content = output.getvalue()
        output.close()

        # Create response with proper headers
        response = make_response(csv_content)
        response.headers["Content-Type"] = "text/csv"
        response.headers[
            "Content-Disposition"
        ] = f'attachment; filename=ke_wp_mappings_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'

        logger.info("Dataset downloaded: %d mappings exported", len(mappings))
        return response

    except Exception as e:
        logger.error("Error generating dataset download: %s", e)
        return (
            render_template("error.html", error="Failed to generate dataset download"),
            500,
        )


@main_bp.route("/ke-details")
def ke_details():
    """Key Event details page"""
    return render_template("ke-details.html")


@main_bp.route("/pw-details")
def pw_details():
    """Pathway details page"""
    return render_template("pw-details.html")


# ========== Enhanced Export Routes ==========

@main_bp.route("/export/<format_name>")
@monitor_performance
def export_dataset(format_name):
    """Export dataset in specified format"""
    if not export_manager:
        return jsonify({"error": "Export functionality not available"}), 500
    
    try:
        # Validate format
        available_formats = export_manager.get_available_formats()
        if format_name not in available_formats:
            return jsonify({
                "error": f"Unsupported format: {format_name}",
                "available_formats": available_formats
            }), 400
        
        # Get export options from query parameters
        include_metadata = request.args.get('metadata', 'true').lower() == 'true'
        include_statistics = request.args.get('statistics', 'true').lower() == 'true'
        include_provenance = request.args.get('provenance', 'true').lower() == 'true'
        compression = request.args.get('compression', 'snappy')
        
        # Export data
        if format_name == 'json':
            export_data = export_manager.export('json', 
                include_metadata=include_metadata, 
                include_provenance=include_provenance
            )
        elif format_name == 'jsonld':
            export_data = export_manager.export('jsonld', 
                include_metadata=include_metadata
            )
        elif format_name in ['excel', 'xlsx']:
            export_data = export_manager.export('excel',
                include_statistics=include_statistics,
                include_metadata=include_metadata
            )
        elif format_name == 'parquet':
            export_data = export_manager.export('parquet',
                include_metadata_columns=include_metadata,
                compression=compression
            )
        else:
            export_data = export_manager.export(format_name)
        
        # Create response
        response = make_response(export_data)
        response.headers["Content-Type"] = export_manager.get_content_type(format_name)
        
        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        extension = export_manager.get_file_extension(format_name)
        filename = f"ke_wp_mappings_{timestamp}.{extension}"
        
        if format_name in ['excel', 'parquet']:
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        else:
            response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        
        # Add custom headers
        response.headers["X-Dataset-Version"] = metadata_manager.metadata.get("version", "1.0.0") if metadata_manager else "1.0.0"
        response.headers["X-Export-Format"] = format_name
        from src.utils.timezone import format_local_datetime
        response.headers["X-Export-Timestamp"] = format_local_datetime()
        
        logger.info("Dataset exported in %s format", sanitize_log(format_name))
        return response
        
    except ImportError as e:
        logger.error("Missing dependencies for %s export: %s", sanitize_log(format_name), sanitize_log(str(e)))
        return jsonify({
            "error": f"Required dependencies not installed for {format_name} export",
            "details": "Internal error"
        }), 500
    except Exception as e:
        logger.error("Error exporting dataset in %s format: %s", sanitize_log(format_name), sanitize_log(str(e)))
        return jsonify({"error": "Export failed", "details": "Internal error"}), 500


@main_bp.route("/export/formats")
def list_export_formats():
    """List available export formats"""
    if not export_manager:
        return jsonify({"error": "Export functionality not available"}), 500
    
    formats_info = {
        "available_formats": export_manager.get_available_formats(),
        "format_details": {
            "csv": {
                "description": "Comma-separated values with comprehensive metadata header",
                "content_type": "text/csv",
                "use_cases": ["Spreadsheet analysis", "Basic data processing"]
            },
            "json": {
                "description": "Comprehensive JSON with schema, statistics, and provenance",
                "content_type": "application/json", 
                "use_cases": ["Web APIs", "Data interchange", "Programmatic access"]
            },
            "jsonld": {
                "description": "JSON-LD format for semantic web applications",
                "content_type": "application/ld+json",
                "use_cases": ["Semantic web", "Linked data", "Knowledge graphs"]
            },
            "rdf": {
                "description": "RDF/XML format with biological ontologies",
                "content_type": "application/rdf+xml",
                "use_cases": ["Ontology integration", "Semantic reasoning"]
            },
            "turtle": {
                "description": "Turtle format for RDF data",
                "content_type": "text/turtle",
                "use_cases": ["Triple stores", "SPARQL queries"]
            },
            "excel": {
                "description": "Excel workbook with multiple sheets and data dictionary",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "use_cases": ["Data analysis", "Reporting", "Manual review"]
            },
            "parquet": {
                "description": "Columnar format optimized for analytics",
                "content_type": "application/octet-stream",
                "use_cases": ["Big data analytics", "Machine learning", "Data science"]
            }
        }
    }
    
    return jsonify(formats_info)


@main_bp.route("/dataset/metadata")
def dataset_metadata():
    """Get comprehensive dataset metadata"""
    if metadata_manager is None:
        return jsonify({"error": "dataset metadata not configured",
                        "reason": "metadata_manager unavailable"}), 503

    try:
        metadata = metadata_manager.get_current_metadata()
        return jsonify(metadata)
    except Exception as e:
        logger.error("Error retrieving dataset metadata: %s", e)
        return jsonify({"error": "Failed to retrieve metadata", "details": "Internal error"}), 500


@main_bp.route("/dataset/versions")
def dataset_versions():
    """Get dataset version history"""
    if metadata_manager is None:
        return jsonify({"error": "dataset metadata not configured",
                        "reason": "metadata_manager unavailable"}), 503

    try:
        versions = metadata_manager.get_versions()
        return jsonify({"versions": versions})
    except Exception as e:
        logger.error("Error retrieving dataset versions: %s", e)
        return jsonify({"error": "Failed to retrieve versions", "details": "Internal error"}), 500


@main_bp.route("/dataset/citation")
def dataset_citation():
    """Generate dataset citation in various formats"""
    if metadata_manager is None:
        return jsonify({"error": "dataset metadata not configured",
                        "reason": "metadata_manager unavailable"}), 503

    citation_format = request.args.get('format', 'apa').lower()
    
    try:
        citation = metadata_manager.generate_citation(citation_format)
        
        response_data = {
            "format": citation_format,
            "citation": citation,
            "available_formats": ["apa", "bibtex"]
        }
        
        if citation_format == "bibtex":
            response = make_response(citation)
            response.headers["Content-Type"] = "application/x-bibtex"
            response.headers["Content-Disposition"] = 'inline; filename="ke_wp_dataset.bib"'
            return response
        else:
            return jsonify(response_data)
            
    except ValueError as e:
        logger.warning("Invalid citation format request: %s", e)
        return jsonify({"error": "Invalid citation format"}), 400
    except Exception as e:
        logger.error("Error generating citation: %s", e)
        return jsonify({"error": "Failed to generate citation", "details": "Internal error"}), 500


@main_bp.route("/dataset/datacite")
def datacite_metadata():
    """Get DataCite XML metadata"""
    if metadata_manager is None:
        return jsonify({"error": "dataset metadata not configured",
                        "reason": "metadata_manager unavailable"}), 503

    try:
        datacite_xml = metadata_manager.export_datacite_xml()
        
        response = make_response(datacite_xml)
        response.headers["Content-Type"] = "application/xml"
        response.headers["Content-Disposition"] = 'inline; filename="datacite_metadata.xml"'
        
        return response
        
    except Exception as e:
        logger.error("Error generating DataCite metadata: %s", e)
        return jsonify({"error": "Failed to generate DataCite metadata", "details": "Internal error"}), 500


@main_bp.route("/mappings/<string:mapping_uuid>")
def mapping_detail(mapping_uuid):
    """Stable mapping detail page — accessible via permanent UUID URL."""
    mapping = mapping_model.get_mapping_by_uuid(mapping_uuid)
    if not mapping:
        abort(404)
    return render_template("mapping_detail.html", mapping=mapping)


@main_bp.route("/aop-network")
def aop_network():
    """Permanent redirect: /aop-network -> /aop-explorer (AOPX-02).
    Route registration MUST stay — ~10 weeks of inbound links from papers/Slack/slides."""
    return redirect(url_for('main.aop_explorer'), 301)


@main_bp.route("/aop-explorer")
def aop_explorer():
    """AOP Explorer visualization page (renamed from aop-network, AOPX-01)."""
    return render_template("aop-explorer.html")


@main_bp.route("/api/ker-adjacency")
def ker_adjacency_api():
    """Serve precomputed KER adjacency data for the AOP Network page."""
    if ker_adjacency is None:
        return jsonify({"error": "KER adjacency data not loaded"}), 503
    return jsonify(ker_adjacency)


@main_bp.route("/api/mapped-ke-ids")
def mapped_ke_ids():
    """Return KE IDs with at least one approved mapping, by type (wp or go)."""
    mapping_type = request.args.get("type", "wp")
    if mapping_type == "go" and go_mapping_model:
        ke_ids = go_mapping_model.get_mapped_ke_ids()
    elif mapping_type == "wp" and mapping_model:
        ke_ids = mapping_model.get_mapped_ke_ids()
    elif mapping_type == "reactome" and reactome_mapping_model:
        ke_ids = reactome_mapping_model.get_mapped_ke_ids()
    else:
        ke_ids = []
    return jsonify({"type": mapping_type, "ke_ids": ke_ids})


@main_bp.route("/api/aop-oecd-status")
def api_aop_oecd_status():
    """Serve precomputed per-AOP OECD development-status map (AOPX-06/07).
    Degrades to an empty object when the gitignored data file is absent."""
    return jsonify(oecd_status_data)


@main_bp.route("/api/preview/<resource>/<format_name>")
def download_preview(resource, format_name):
    """Return the first ≤20 lines of a cached export file for in-page preview.

    Security: the file path is looked up exclusively from PREVIEW_ALLOWLIST.
    The raw URL parameters (resource, format_name) are NEVER used to construct
    a filesystem path — they are only dict keys.  Any (resource, format_name)
    pair not present in the allowlist returns {"lines": [], "available": false}.
    """
    import itertools
    file_path = PREVIEW_ALLOWLIST.get((resource, format_name))
    if not file_path or not os.path.exists(file_path):
        return jsonify({"lines": [], "available": False})
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = list(itertools.islice(f, 20))
    except OSError as exc:
        logger.warning("Preview read failed for %s/%s: %s", resource, format_name, exc)
        return jsonify({"lines": [], "available": False})
    return jsonify({"lines": lines, "available": True})


@main_bp.route("/api/ke-biolevels")
def ke_biolevels():
    """Return a map of KE ID to biological level."""
    svc = getattr(current_app, 'service_container', None)
    index = svc.ke_metadata_index if svc else {}
    result = {}
    for ke_label, meta in index.items():
        biolevel = meta.get("biolevel", "")
        if biolevel:
            result[ke_label] = biolevel
    return jsonify(result)


@main_bp.route("/api/ke-gene-counts")
def ke_gene_counts():
    """Return a map of KE ID to de-duplicated gene count from WP+GO mappings."""
    from src.exporters.gmt_exporter import _fetch_pathway_genes_batch

    mapping_type = request.args.get("type", "all").lower()
    result = {}

    # WP gene data
    if mapping_type in ("wp", "all"):
        wp_mappings = list(mapping_model.get_all_mappings() if mapping_model else [])
        wp_ids = list({m['wp_id'] for m in wp_mappings})
        genes_by_wp = _fetch_pathway_genes_batch(wp_ids, cache_model=cache_model_ref) if wp_ids else {}

        for m in wp_mappings:
            ke_id = m['ke_id']
            genes = genes_by_wp.get(m['wp_id'], [])
            result.setdefault(ke_id, set()).update(genes)

    # GO gene data
    if mapping_type in ("go", "all"):
        go_mappings = list(go_mapping_model.get_all_mappings() if go_mapping_model else [])
        go_annotations = {}
        try:
            go_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'go_bp_gene_annotations.json')
            with open(go_path, 'r') as f:
                go_annotations = json_lib.load(f)
        except Exception as e:
            # GO BP annotations file is generated by precompute_go_hierarchy.py;
            # absence just means GO gene rows in the response stay empty.
            logger.debug("GO BP annotations load failed: %s", e)

        for m in go_mappings:
            ke_id = m['ke_id']
            genes = go_annotations.get(m['go_id'], [])
            result.setdefault(ke_id, set()).update(genes)

    return jsonify({ke_id: len(genes) for ke_id, genes in result.items() if len(genes) > 0})


@main_bp.route("/api/ke-genes/<ke_id>")
def ke_genes_for_ke(ke_id):
    """Return genes for a single KE grouped by WP/GO source term."""
    from src.exporters.gmt_exporter import _fetch_pathway_genes_batch

    mapping_type = request.args.get("type", "all").lower()
    all_genes = set()
    groups = []

    # WP gene data
    if mapping_type in ("wp", "all"):
        wp_mappings = [m for m in (mapping_model.get_all_mappings() if mapping_model else [])
                       if m.get('ke_id') == ke_id]
        wp_ids = list({m['wp_id'] for m in wp_mappings})
        genes_by_wp = _fetch_pathway_genes_batch(wp_ids, cache_model=cache_model_ref) if wp_ids else {}

        for m in wp_mappings:
            genes = sorted(genes_by_wp.get(m['wp_id'], []))
            all_genes.update(genes)
            if genes:
                groups.append({
                    "type": "wp",
                    "id": m['wp_id'],
                    "name": m.get('wp_title', m['wp_id']),
                    "confidence_level": m.get('confidence_level', 'low'),
                    "genes": genes
                })

    # GO gene data
    if mapping_type in ("go", "all"):
        go_mappings = [m for m in (go_mapping_model.get_all_mappings() if go_mapping_model else [])
                       if m.get('ke_id') == ke_id]
        go_annotations = {}
        try:
            go_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'go_bp_gene_annotations.json')
            with open(go_path, 'r') as f:
                go_annotations = json_lib.load(f)
        except Exception as e:
            # GO BP annotations file is generated by precompute_go_hierarchy.py;
            # absence just means GO gene rows in the response stay empty.
            logger.debug("GO BP annotations load failed: %s", e)

        for m in go_mappings:
            genes = sorted(go_annotations.get(m['go_id'], []))
            all_genes.update(genes)
            if genes:
                groups.append({
                    "type": "go",
                    "id": m['go_id'],
                    "name": m.get('go_name', m['go_id']),
                    "confidence_level": m.get('confidence_level', 'low'),
                    "genes": genes
                })

    return jsonify({
        "ke_id": ke_id,
        "genes": sorted(list(all_genes)),
        "groups": groups
    })


@main_bp.route("/stats")
def stats():
    """Public dataset metrics dashboard — no login required."""
    mapping_stats = get_mapping_stats() if mapping_model else {
        "wp_total": 0, "go_total": 0, "reactome_total": 0, "total": 0,
        "wp_by_confidence": {}, "go_by_confidence": {}, "reactome_by_confidence": {}
    }
    return render_template("stats.html", stats=mapping_stats)


@main_bp.route("/downloads")
def downloads():
    """Public downloads page — no login required."""
    meta_path = Path("data/zenodo_meta.json")
    zenodo_meta = {}
    try:
        if meta_path.exists():
            zenodo_meta = json_lib.loads(meta_path.read_text())
    except Exception as e:
        # Same best-effort read as the context processor — page just renders
        # without the Zenodo DOI block if the manifest is missing/malformed.
        logger.debug("zenodo_meta read failed on /downloads: %s", e)
    return render_template("downloads.html", zenodo_meta=zenodo_meta)


_VALID_MAPPING_TYPES = {
    "wp", "wp-centric", "go", "go-centric", "reactome", "reactome-centric",
}
_VALID_MIN_CONFIDENCE = {None, "high", "medium", "low"}


def _get_or_generate_gmt(mapping_type: str, min_confidence: str = None):
    """Return (path, filename) for GMT file, generating it if not cached."""
    from src.exporters.gmt_exporter import (
        generate_ke_wp_gmt, generate_ke_go_gmt,
        generate_ke_centric_wp_gmt, generate_ke_centric_go_gmt,
        generate_ke_reactome_gmt, generate_ke_centric_reactome_gmt,
    )
    # Whitelist user-controlled values that flow into the cache filename below —
    # without this guard, a crafted min_confidence (e.g. "../etc/passwd") would
    # let the cache write escape EXPORT_CACHE_DIR.
    if mapping_type not in _VALID_MAPPING_TYPES:
        abort(404)
    if min_confidence not in _VALID_MIN_CONFIDENCE:
        abort(400)
    today = datetime.today().date().isoformat()
    # "MinHigh", not "High": since #206 this parameter is a threshold, and the
    # admin/Zenodo bundles write exact-tier files under the bare _High/_Medium/
    # _Low names into the same directory. Distinct tokens stop a threshold file
    # and a partition file of the same day from being mistaken for each other.
    tier = f"Min{min_confidence.capitalize()}" if min_confidence else "All"
    filename = f"KE-{mapping_type.upper()}_{today}_{tier}.gmt"
    # werkzeug.security.safe_join is on CodeQL's recognised path-injection
    # sanitizer list — it returns None if the joined path would escape the
    # base directory. Combined with the entry whitelist above, this is
    # defence in depth: two independent layers both prevent traversal.
    safe = safe_join(str(EXPORT_CACHE_DIR), filename)
    if safe is None:
        abort(404)
    cache_path = Path(safe)
    if not cache_path.exists():
        EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if mapping_type == "wp":
            mappings = mapping_model.get_all_mappings() if mapping_model else []
            content = generate_ke_wp_gmt(mappings, cache_model=cache_model_ref, min_confidence=min_confidence)
        elif mapping_type == "wp-centric":
            mappings = mapping_model.get_all_mappings() if mapping_model else []
            content = generate_ke_centric_wp_gmt(mappings, cache_model=cache_model_ref, min_confidence=min_confidence)
        elif mapping_type == "go-centric":
            mappings = go_mapping_model.get_all_mappings() if go_mapping_model else []
            content = generate_ke_centric_go_gmt(mappings, min_confidence=min_confidence)
        elif mapping_type == "reactome":
            mappings = reactome_mapping_model.get_all_mappings() if reactome_mapping_model else []
            content = generate_ke_reactome_gmt(mappings, min_confidence=min_confidence)
        elif mapping_type == "reactome-centric":
            mappings = reactome_mapping_model.get_all_mappings() if reactome_mapping_model else []
            content = generate_ke_centric_reactome_gmt(mappings, min_confidence=min_confidence)
        else:
            mappings = go_mapping_model.get_all_mappings() if go_mapping_model else []
            content = generate_ke_go_gmt(mappings, min_confidence=min_confidence)
        if content:
            cache_path.write_text(content, encoding="utf-8")
        else:
            # Write empty placeholder so next request doesn't re-query
            cache_path.write_text("", encoding="utf-8")
    return cache_path, filename


@main_bp.route("/exports/gmt/ke-wp")
def download_ke_wp_gmt():
    """Download KE-WP GMT file. ?min_confidence=High|Medium|Low is a minimum: High yields high only, Medium yields medium and high, Low yields all."""
    min_conf = request.args.get("min_confidence", "").lower() or None
    cache_path, filename = _get_or_generate_gmt("wp", min_conf)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-WP mappings available or WikiPathways SPARQL unavailable"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name=filename, mimetype="text/plain")


@main_bp.route("/exports/gmt/ke-go")
def download_ke_go_gmt():
    """Download KE-GO GMT file. ?min_confidence=High|Medium|Low is a minimum: High yields high only, Medium yields medium and high, Low yields all."""
    min_conf = request.args.get("min_confidence", "").lower() or None
    cache_path, filename = _get_or_generate_gmt("go", min_conf)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-GO mappings available"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name=filename, mimetype="text/plain")


@main_bp.route("/exports/gmt/ke-wp-centric")
def download_ke_wp_centric_gmt():
    """KE-centric WP GMT: one row per KE, genes unioned across all WP mappings. ?min_confidence=High|Medium|Low is a minimum (Medium yields medium and high)."""
    min_conf = request.args.get("min_confidence", "").lower() or None
    cache_path, filename = _get_or_generate_gmt("wp-centric", min_conf)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-WP mappings available or WikiPathways SPARQL unavailable"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name=filename, mimetype="text/plain")


@main_bp.route("/exports/gmt/ke-go-centric")
def download_ke_go_centric_gmt():
    """KE-centric GO GMT: one row per KE, genes unioned across all GO mappings. ?min_confidence=High|Medium|Low is a minimum (Medium yields medium and high)."""
    min_conf = request.args.get("min_confidence", "").lower() or None
    cache_path, filename = _get_or_generate_gmt("go-centric", min_conf)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-GO mappings available"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name=filename, mimetype="text/plain")


@main_bp.route("/exports/gmt/ke-reactome")
def download_ke_reactome_gmt():
    """Download KE-Reactome GMT file. ?min_confidence=High|Medium|Low is a minimum: High yields high only, Medium yields medium and high, Low yields all."""
    min_conf = request.args.get("min_confidence", "").lower() or None
    cache_path, filename = _get_or_generate_gmt("reactome", min_conf)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-Reactome mappings available"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name=filename, mimetype="text/plain")


@main_bp.route("/exports/gmt/ke-reactome-centric")
def download_ke_reactome_centric_gmt():
    """KE-centric Reactome GMT: one row per KE, genes unioned across all Reactome mappings. ?min_confidence=High|Medium|Low is a minimum (Medium yields medium and high)."""
    min_conf = request.args.get("min_confidence", "").lower() or None
    cache_path, filename = _get_or_generate_gmt("reactome-centric", min_conf)
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-Reactome mappings available"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name=filename, mimetype="text/plain")


@main_bp.route("/exports/rdf/ke-wp")
def download_ke_wp_rdf():
    """Download KE-WP RDF/Turtle file."""
    from src.exporters.rdf_exporter import generate_ke_wp_turtle
    cache_path = EXPORT_CACHE_DIR / "ke-wp-mappings.ttl"
    if not cache_path.exists():
        EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        mappings = mapping_model.get_all_mappings() if mapping_model else []
        if mappings:
            content = generate_ke_wp_turtle(mappings)
            cache_path.write_text(content or "", encoding="utf-8")
        else:
            # No mappings → write empty placeholder so the 503 branch fires below.
            # generate_ke_wp_turtle([]) emits a non-empty @prefix prelude
            # (rdflib's Graph.serialize always writes prefix declarations),
            # which would otherwise bypass the st_size == 0 check and return
            # a half-formed Turtle file to clients.
            cache_path.write_text("", encoding="utf-8")
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-WP mappings available for RDF export"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name="ke-wp-mappings.ttl", mimetype="text/turtle")


@main_bp.route("/exports/rdf/ke-go")
def download_ke_go_rdf():
    """Download KE-GO RDF/Turtle file."""
    from src.exporters.rdf_exporter import generate_ke_go_turtle
    cache_path = EXPORT_CACHE_DIR / "ke-go-mappings.ttl"
    if not cache_path.exists():
        EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        mappings = go_mapping_model.get_all_mappings() if go_mapping_model else []
        if mappings:
            content = generate_ke_go_turtle(mappings)
            cache_path.write_text(content or "", encoding="utf-8")
        else:
            # No mappings → write empty placeholder so the 503 branch fires below.
            # generate_ke_go_turtle([]) emits a non-empty @prefix prelude
            # (rdflib's Graph.serialize always writes prefix declarations),
            # which would otherwise bypass the st_size == 0 check and return
            # a half-formed Turtle file to clients.
            cache_path.write_text("", encoding="utf-8")
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-GO mappings available for RDF export"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name="ke-go-mappings.ttl", mimetype="text/turtle")


@main_bp.route("/exports/rdf/ke-reactome")
def download_ke_reactome_rdf():
    """Download KE-Reactome RDF/Turtle file."""
    from src.exporters.rdf_exporter import generate_ke_reactome_turtle
    cache_path = EXPORT_CACHE_DIR / "ke-reactome-mappings.ttl"
    if not cache_path.exists():
        EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        mappings = reactome_mapping_model.get_all_mappings() if reactome_mapping_model else []
        if mappings:
            content = generate_ke_reactome_turtle(mappings, reactome_metadata=reactome_metadata)
            cache_path.write_text(content or "", encoding="utf-8")
        else:
            # No mappings → write empty placeholder so the 503 branch fires below
            cache_path.write_text("", encoding="utf-8")
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return jsonify({"error": "No KE-Reactome mappings available for RDF export"}), 503
    return send_file(str(cache_path), as_attachment=True, download_name="ke-reactome-mappings.ttl", mimetype="text/turtle")


@main_bp.route("/documentation")
@main_bp.route("/documentation/<section>")
@monitor_performance
def documentation(section='overview'):
    """
    Serve documentation pages with section navigation

    Args:
        section: Documentation section to display ('overview', 'user-guide', 'admin-guide', 'api')

    Returns:
        Rendered documentation template
    """
    sections = {
        'overview': 'Getting Started',
        'user-guide': 'User Guide',
        'scoring-guide': 'Scoring Systems',
        'admin-guide': 'Admin Guide',
        'api': 'API Documentation',
    }

    # Validate section
    if section not in sections:
        logger.warning("Invalid documentation section requested: %s", sanitize_log(section))
        section = 'overview'

    logger.info("Documentation section requested: %s", sanitize_log(section))

    return render_template('documentation.html',
        current_section=section,
        sections=sections
    )


@main_bp.route("/privacy")
def privacy_notice():
    """
    Static privacy notice and data-retention policy.

    Mirrors the GDPR content of docs/DMP.md §6 and the retention rules in
    docs/GOVERNANCE.md. Publicly accessible; linked from the site footer
    and the login modal so that proposers see it before authenticating.
    """
    return render_template("privacy.html")


@main_bp.route("/api/docs")
def swagger_ui():
    """Interactive Swagger UI — publicly accessible, no login required."""
    spec_url = "/api/v1/spec"
    return render_template("swagger_ui.html", spec_url=spec_url)


@main_bp.route("/api/v1/spec")
def openapi_spec():
    """Serve the static OpenAPI 3.0 spec YAML — no rate limiting, CORS-enabled."""
    import os as _os
    static_openapi_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "static", "openapi"
    )
    resp = send_from_directory(static_openapi_dir, "openapi.yaml", mimetype="application/x-yaml")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@main_bp.route("/docs")
def api_consumer_docs():
    """Public API consumer guide — Python and R code examples, rate limit policy."""
    return render_template("docs_api.html")
