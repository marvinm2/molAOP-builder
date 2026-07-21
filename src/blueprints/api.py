"""
API Blueprint
Handles all API endpoints for data submission and retrieval
"""
import hashlib
import json
import logging
import time
from datetime import datetime
from functools import wraps

import requests
from flask import Blueprint, jsonify, request, session

from src.core.models import GoProposalModel, ProposalModel
from src.services.rate_limiter import general_rate_limit, sparql_rate_limit, submission_rate_limit
from src.core.schemas import (
    CheckEntrySchema,
    GoCheckEntrySchema,
    GoMappingSchema,
    GoProposalChangeSchema,
    MappingSchema,
    ProposalSchema,
    ReactomeCheckEntrySchema,
    ReactomeMappingSchema,
    ReactomeProposalChangeSchema,
    SecurityValidation,
    validate_request_data,
)
from src.core.config_loader import ConfigLoader
from src.services import source_versions
from src.utils.text import sanitize_log

logger = logging.getLogger(__name__)

# Module-level cache for scoring config
_config_cache = None
_config_cache_time = None
_config_cache_ttl = 300  # 5 minutes

api_bp = Blueprint("api", __name__)

# Global model instances (will be set by app initialization)
mapping_model = None
proposal_model = None
cache_model = None
pathway_suggestion_service = None
go_suggestion_service = None
go_mapping_model = None
go_proposal_model = None
ke_metadata = None
pathway_metadata = None
ke_aop_membership = None
reactome_suggestion_service = None
reactome_mapping_model = None
reactome_proposal_model = None


def set_models(mapping, proposal, cache, suggestion_service=None,
               go_suggestion_svc=None, go_mapping=None, go_proposal=None,
               ke_meta=None, pathway_meta=None, ke_aop_membership_data=None,
               reactome_suggestion_svc=None, reactome_mapping=None,
               reactome_proposal=None):
    """Set the model instances"""
    global mapping_model, proposal_model, cache_model, pathway_suggestion_service
    global go_suggestion_service, go_mapping_model, go_proposal_model
    global ke_metadata, pathway_metadata, ke_aop_membership
    global reactome_suggestion_service, reactome_mapping_model, reactome_proposal_model
    mapping_model = mapping
    proposal_model = proposal
    cache_model = cache
    pathway_suggestion_service = suggestion_service
    go_suggestion_service = go_suggestion_svc
    go_mapping_model = go_mapping
    go_proposal_model = go_proposal
    ke_metadata = ke_meta
    pathway_metadata = pathway_meta
    ke_aop_membership = ke_aop_membership_data
    reactome_suggestion_service = reactome_suggestion_svc
    reactome_mapping_model = reactome_mapping
    reactome_proposal_model = reactome_proposal


def login_required(f):
    """Decorator to require login for protected routes"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)

    return decorated_function


def _log_method_filter_deprecation(filter_value: str, endpoint_name: str) -> None:
    """Log a deprecation warning when method_filter is used with a non-default value.

    Called once per request right after method_filter is read from query args.
    Default value ('all') does NOT log — avoids spam from normal frontend traffic.
    Non-default values still work (backward compatible) but emit one WARNING per
    request so external scripts can be updated before v2 removes the parameter.
    """
    if filter_value and filter_value != 'all':
        logger.warning(
            "DEPRECATED: method_filter=%s on %s — this query parameter is deprecated "
            "and will be removed in v2. Frontend no longer sends it; backend still honors "
            "it for backward compatibility. Pure-semantic ranking is the v1.5 default.",
            sanitize_log(filter_value), sanitize_log(endpoint_name),
        )


@api_bp.route("/check", methods=["POST"])
@general_rate_limit
def check_entry():
    """Check if the KE ID or the KE-WP pair already exist in the dataset."""
    try:
        # Extract only the required fields for validation
        check_data = {
            "ke_id": request.form.get("ke_id"),
            "wp_id": request.form.get("wp_id"),
        }

        # Validate input data
        is_valid, validated_data, errors = validate_request_data(
            CheckEntrySchema, check_data
        )

        if not is_valid:
            logger.warning("Invalid check entry request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        ke_id = validated_data["ke_id"]
        wp_id = validated_data["wp_id"]

        result = mapping_model.check_mapping_exists_with_proposals(ke_id, wp_id)
        return jsonify(result), 200
    except Exception as e:
        logger.error("Error checking entry: %s", e)
        return jsonify({"error": "Failed to check entry"}), 500


@api_bp.route("/submit", methods=["POST"])
@submission_rate_limit
@login_required
def submit():
    """Add a new KE-WP mapping entry to the dataset."""
    try:
        # Extract only the required fields for validation (exclude CSRF token).
        # Phase 34 ASMT-02: step1..step4 are the four assessment-question
        # answers the mapper UI already sends (static/js/main.js:1378-1391);
        # they are optional in MappingSchema for backward-compat.
        submit_data = {
            "ke_id": request.form.get("ke_id"),
            "ke_title": request.form.get("ke_title"),
            "wp_id": request.form.get("wp_id"),
            "wp_title": request.form.get("wp_title"),
            "connection_type": request.form.get("connection_type"),
            "confidence_level": request.form.get("confidence_level"),
            "step1": request.form.get("step1"),
            "step2": request.form.get("step2"),
            "step3": request.form.get("step3"),
            "step4": request.form.get("step4"),
        }
        # Drop None values so Marshmallow's `required=False` semantics fire
        # (rather than treating None as an explicit "" -> validation failure).
        submit_data = {k: v for k, v in submit_data.items() if v is not None}

        # Validate input data
        is_valid, validated_data, errors = validate_request_data(
            MappingSchema, submit_data
        )

        if not is_valid:
            logger.warning("Invalid submit request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        # Sanitize string inputs
        ke_id = SecurityValidation.sanitize_string(validated_data["ke_id"])
        ke_title = SecurityValidation.sanitize_string(validated_data["ke_title"])
        wp_id = SecurityValidation.sanitize_string(validated_data["wp_id"])
        wp_title = SecurityValidation.sanitize_string(validated_data["wp_title"])
        connection_type = validated_data["connection_type"]
        confidence_level = validated_data["confidence_level"]

        # Phase 34 ASMT-02: read assessment-question answers from validated
        # payload. Rename from JS-side keys (step1..step4) to DB column names
        # per the MappingSchema docstring whitelist. Each value already
        # validated server-side against canonical option keys via
        # validate.OneOf(...); sanitize_string is defense-in-depth.
        step1 = validated_data.get("step1")
        step2 = validated_data.get("step2")
        step3 = validated_data.get("step3")
        step4 = validated_data.get("step4")
        proposed_relationship = (
            SecurityValidation.sanitize_string(step1) if step1 else None
        )
        proposed_basis = (
            SecurityValidation.sanitize_string(step2) if step2 else None
        )
        proposed_specificity = (
            SecurityValidation.sanitize_string(step3) if step3 else None
        )
        proposed_coverage = (
            SecurityValidation.sanitize_string(step4) if step4 else None
        )

        # Get current user
        created_by = session.get("user", {}).get("username", "anonymous")

        # Additional validation for GitHub username if available
        if created_by != "anonymous" and not SecurityValidation.validate_username(
            created_by
        ):
            logger.error("Invalid username format: %s", created_by)
            return jsonify({"error": "Authentication error"}), 401

        # Capture suggestion score from form (stored on proposal; written to mapping at approval)
        suggestion_score_raw = request.form.get("suggestion_score")
        try:
            suggestion_score = float(suggestion_score_raw) if suggestion_score_raw else None
        except (ValueError, TypeError):
            suggestion_score = None

        # Create proposal record (status=pending) — mapping is created only after admin approval.
        # Phase 34 ASMT-02: forward the four assessment fields to the model
        # layer; they persist on the proposals row and are carried into the
        # mapping at admin-approve time (see src/blueprints/admin.py).
        proposal_id = proposal_model.create_new_pair_proposal(
            ke_id=ke_id,
            ke_title=ke_title,
            wp_id=wp_id,
            wp_title=wp_title,
            connection_type=connection_type,
            confidence_level=confidence_level,
            provider_username=created_by,
            suggestion_score=suggestion_score,
            proposed_relationship=proposed_relationship,
            proposed_basis=proposed_basis,
            proposed_specificity=proposed_specificity,
            proposed_coverage=proposed_coverage,
        )
        # Phase 32 H-2 port: the partial-unique index on
        # proposals(ke_id, wp_id) WHERE status='pending' AND mapping_id IS NULL
        # rejects concurrent duplicate submits. Surface as 409 using the
        # existing check_mapping_exists_with_proposals shape (which WP
        # clients already handle) rather than Reactome's verbatim
        # {error, blocking_type} shape — see CONTEXT.md L34-39.
        if proposal_id == ProposalModel.DUPLICATE_PENDING:
            dup_payload = mapping_model.check_mapping_exists_with_proposals(
                ke_id, wp_id
            )
            logger.info(
                "Duplicate-pending /submit blocked at DB layer: "
                "%s -> %s by %s", ke_id, wp_id, created_by,
            )
            return jsonify(dup_payload), 409
        if proposal_id:
            logger.info(
                "New-pair proposal created: %s -> %s by %s (proposal_id=%s)",
                ke_id, wp_id, created_by, proposal_id,
            )
            return jsonify({
                "message": "Proposal submitted successfully and is pending admin review.",
                "proposal_id": proposal_id,
            }), 200
        else:
            return jsonify({"error": "Failed to create proposal"}), 500
    except Exception as e:
        logger.error("Error adding entry: %s", e)
        return jsonify({"error": "Failed to add entry"}), 500


@api_bp.route("/get_ke_options", methods=["GET"])
@sparql_rate_limit
def get_ke_options():
    """Fetch Key Event options from pre-computed metadata or SPARQL endpoint"""
    try:
        # Serve from pre-computed metadata if available
        if ke_metadata:
            logger.info("Serving %d KE options from pre-computed metadata", len(ke_metadata))
            return jsonify(ke_metadata), 200

        # Fall back to live SPARQL query
        sparql_query = """
        PREFIX aopo: <http://aopkb.org/aop_ontology#>
        PREFIX dc: <http://purl.org/dc/elements/1.1/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX foaf: <http://xmlns.com/foaf/0.1/>
        PREFIX nci: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>

        SELECT ?KEtitle ?KElabel ?KEpage ?KEdescription ?biolevel
        WHERE {
          ?KE a aopo:KeyEvent ;
              dc:title ?KEtitle ;
              rdfs:label ?KElabel;
              foaf:page ?KEpage .
          OPTIONAL { ?KE dc:description ?KEdescription }
          OPTIONAL { ?KE nci:C25664 ?biolevel }
        }
        """
        endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

        # Check cache first
        query_hash = hashlib.md5(sparql_query.encode()).hexdigest()
        cached_response = cache_model.get_cached_response(endpoint, query_hash)

        if cached_response:
            logger.info("Serving KE options from cache")
            return jsonify(json.loads(cached_response)), 200

        response = requests.post(
            endpoint,
            data={"query": sparql_query},
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )

        if response.status_code == 200:
            data = response.json()
            if "results" not in data or "bindings" not in data["results"]:
                logger.error("Invalid SPARQL response format")
                return jsonify({"error": "Invalid response from KE service"}), 500

            results = [
                {
                    "KEtitle": binding.get("KEtitle", {}).get("value", ""),
                    "KElabel": binding.get("KElabel", {}).get("value", ""),
                    "KEpage": binding.get("KEpage", {}).get("value", ""),
                    "KEdescription": binding.get("KEdescription", {}).get("value", ""),
                    "biolevel": binding.get("biolevel", {}).get("value", ""),
                }
                for binding in data["results"]["bindings"]
                if all(key in binding for key in ["KEtitle", "KElabel", "KEpage"])
            ]

            # Cache the response
            cache_model.cache_response(endpoint, query_hash, json.dumps(results), 24)
            logger.info("Fetched and cached %d KE options", len(results))
            return jsonify(results), 200
        else:
            logger.error(
                "SPARQL Query Failed: %s - %s", response.status_code, response.text
            )
            return jsonify({"error": "Failed to fetch KE options"}), 500
    except requests.exceptions.Timeout:
        logger.error("SPARQL request timeout")
        return jsonify({"error": "Service timeout - please try again"}), 503
    except Exception as e:
        logger.error("Error fetching KE options: %s", e)
        return jsonify({"error": "Failed to fetch KE options"}), 500


@api_bp.route("/api/ke_detail/<path:ke_id>", methods=["GET"])
@general_rate_limit
def get_ke_detail(ke_id):
    """
    Get KE detail from pre-fetched local data.
    Returns: title, description, biolevel, ke_page, and aop_membership.
    No live SPARQL — reads ke_metadata.json + ke_aop_membership.json.
    """
    if not ke_id or not ke_id.strip():
        return jsonify({"error": "Invalid Key Event ID"}), 400

    if not ke_metadata:
        return jsonify({"error": "KE metadata not available"}), 503

    # Linear scan — fast enough for ~1561 entries, no extra index required
    ke_data = next(
        (ke for ke in ke_metadata if ke.get("KElabel") == ke_id),
        None,
    )
    if not ke_data:
        return jsonify({"error": f"KE not found: {ke_id}"}), 404

    aop_list = ke_aop_membership.get(ke_id, []) if ke_aop_membership else []

    return jsonify({
        "ke_id": ke_id,
        "ke_title": ke_data.get("KEtitle", ""),
        "ke_description": ke_data.get("KEdescription", ""),
        "biolevel": ke_data.get("biolevel", ""),
        "ke_page": ke_data.get("KEpage", ""),
        "aop_membership": aop_list,
    })


@api_bp.route("/get_pathway_options", methods=["GET"])
@sparql_rate_limit
def get_pathway_options():
    """Fetch pathway options from pre-computed metadata or SPARQL endpoint."""
    try:
        # Serve from pre-computed metadata if available
        if pathway_metadata:
            logger.info("Serving %d pathway options from pre-computed metadata", len(pathway_metadata))
            return jsonify(pathway_metadata), 200

        # Fall back to live SPARQL query
        sparql_query = """
        PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
        PREFIX dc: <http://purl.org/dc/elements/1.1/>
        PREFIX dcterms: <http://purl.org/dc/terms/>

        SELECT DISTINCT ?pathwayID ?pathwayTitle ?pathwayLink ?pathwayDescription
        WHERE {
            ?pathwayRev a wp:Pathway ;
                        dc:title ?pathwayTitle ;
                        dc:identifier ?pathwayLink ;
                        dcterms:identifier ?pathwayID ;
                        wp:organismName "Homo sapiens" .
            OPTIONAL { ?pathwayRev dcterms:description ?pathwayDescription }
        }
        """
        endpoint = "https://sparql.wikipathways.org/sparql"

        # Check cache first
        query_hash = hashlib.md5(sparql_query.encode()).hexdigest()
        cached_response = cache_model.get_cached_response(endpoint, query_hash)

        if cached_response:
            logger.info("Serving pathway options from cache")
            return jsonify(json.loads(cached_response)), 200

        response = requests.post(
            endpoint,
            data={"query": sparql_query},
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )

        if response.status_code == 200:
            data = response.json()
            if "results" not in data or "bindings" not in data["results"]:
                logger.error("Invalid SPARQL response format")
                return jsonify({"error": "Invalid response from pathway service"}), 500

            results = [
                {
                    "pathwayID": binding.get("pathwayID", {}).get("value", ""),
                    "pathwayTitle": binding.get("pathwayTitle", {}).get("value", ""),
                    "pathwayLink": binding.get("pathwayLink", {}).get("value", ""),
                    "pathwayDescription": binding.get("pathwayDescription", {}).get(
                        "value", ""
                    ),
                }
                for binding in data["results"]["bindings"]
                if all(
                    key in binding
                    for key in ["pathwayID", "pathwayTitle", "pathwayLink"]
                )
            ]

            # Cache the response
            cache_model.cache_response(endpoint, query_hash, json.dumps(results), 24)
            logger.info("Fetched and cached %d pathway options", len(results))
            return jsonify(results), 200
        else:
            logger.error(
                "SPARQL Pathway Query Failed: %s - %s", response.status_code, response.text
            )
            return jsonify({"error": "Failed to fetch pathway options"}), 500
    except requests.exceptions.Timeout:
        logger.error("SPARQL pathway request timeout")
        return jsonify({"error": "Service timeout - please try again"}), 503
    except Exception as e:
        logger.error("Error fetching pathway options: %s", e)
        return jsonify({"error": "Failed to fetch pathway options"}), 500


@api_bp.route("/get_data_versions", methods=["GET"])
@sparql_rate_limit
def get_data_versions():
    """Report upstream release versions for the four source resources.

    Delegates to :mod:`src.services.source_versions`, which is the single place
    upstream releases are resolved (and is what the footer badges already use).

    This route previously issued its own SPARQL against AOP-Wiki and
    WikiPathways. That duplicate implementation drifted: it sent
    ``Accept: application/json``, which both endpoints answer with HTTP 406, and
    its handler only branched on ``status_code == 200`` — so a failure produced
    an empty object with a 200 status and no log line (#204). Delegating removes
    the second copy, picks up the 24 h cache, and extends coverage from two
    resources to four (GO and Reactome were never reported here).

    Response shape is unchanged in spirit but now consistent per resource::

        {"wikipathways": {"source": "WikiPathways", "version": "2026-07-10",
                          "unavailable": false}, ...}
    """
    labels = {
        "wikipathways": "WikiPathways",
        "gene_ontology": "Gene Ontology",
        "reactome": "Reactome",
        "aopwiki": "AOP-Wiki",
    }

    try:
        snapshot = source_versions.snapshot()
    except Exception as e:
        # snapshot() is documented never to raise; treat a breach as a real error
        # rather than silently returning an empty body as the old code did.
        logger.error("Error fetching data versions: %s", e)
        return jsonify({"error": "Failed to load data version information"}), 500

    versions = {
        key: {
            "source": label,
            "version": snapshot.get(key, {}).get("version", "unavailable"),
            "unavailable": snapshot.get(key, {}).get("unavailable", True),
        }
        for key, label in labels.items()
    }
    return jsonify(versions), 200


@api_bp.route("/submit_proposal", methods=["POST"])
@login_required
@submission_rate_limit
def submit_proposal():
    """
    Save user proposals to database for admin review

    Handles proposal submission from the explore page modal form.
    Proposals are stored in the database with status 'pending' for admin review.

    Returns:
        JSON response with success/error message
    """
    try:
        # Extract only the required fields for validation (exclude CSRF token)
        proposal_data = {
            "entry": request.form.get("entry"),
            "userName": request.form.get("userName"),
            "userEmail": request.form.get("userEmail"),
            "userAffiliation": request.form.get("userAffiliation"),
            "deleteEntry": request.form.get("deleteEntry", ""),
            "changeConfidence": request.form.get("changeConfidence", ""),
            "changeType": request.form.get("changeType", ""),
        }

        # Debug logging
        logger.info("Proposal submission data: %s", sanitize_log(str(proposal_data)))

        # Validate input data
        is_valid, validated_data, errors = validate_request_data(
            ProposalSchema, proposal_data
        )

        if not is_valid:
            logger.warning("Invalid proposal request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        # Sanitize and extract validated data
        entry_data = validated_data["entry"]
        user_name = SecurityValidation.sanitize_string(validated_data["userName"])
        user_email = validated_data["userEmail"]
        user_affiliation = SecurityValidation.sanitize_string(
            validated_data["userAffiliation"]
        )

        # Extract proposed changes
        proposed_delete = validated_data["deleteEntry"] == "on"
        proposed_confidence = validated_data.get("changeConfidence") or None
        proposed_connection_type = validated_data.get("changeType") or None

        # Additional email domain validation
        if not SecurityValidation.validate_email_domain(user_email):
            return jsonify({"error": "Invalid email domain."}), 400

        # Parse entry data to extract KE and WP IDs
        try:
            # Handle double-serialized JSON
            if entry_data.startswith('"') and entry_data.endswith('"'):
                entry_data = json.loads(entry_data)  # First deserialization
            entry_dict = json.loads(
                entry_data.replace("'", '"')
            )  # Second deserialization with quote fix
            ke_id = entry_dict.get("ke_id") or entry_dict.get("KE_ID")
            wp_id = (
                entry_dict.get("wp_id")
                or entry_dict.get("WP_ID")
                or entry_dict.get("pathway_id")
            )

            if not ke_id or not wp_id:
                return jsonify({"error": "Invalid entry data format."}), 400

        except (json.JSONDecodeError, AttributeError):
            return jsonify({"error": "Could not parse entry data."}), 400

        # Find the mapping ID
        mapping_id = proposal_model.find_mapping_by_details(ke_id, wp_id)
        if not mapping_id:
            return jsonify({"error": "Original mapping not found."}), 404

        # Get current user
        provider_username = session.get("user", {}).get("username", "unknown")

        # Create proposal in database
        proposal_id = proposal_model.create_proposal(
            mapping_id=mapping_id,
            user_name=user_name,
            user_email=user_email,
            user_affiliation=user_affiliation,
            provider_username=provider_username,
            proposed_delete=proposed_delete,
            proposed_confidence=proposed_confidence if proposed_confidence else None,
            proposed_connection_type=proposed_connection_type
            if proposed_connection_type
            else None,
        )

        if proposal_id:
            logger.info(
                "Created proposal %s by user %s for mapping %s", proposal_id, provider_username, mapping_id
            )
            return (
                jsonify(
                    {
                        "message": "Proposal submitted successfully and is pending admin review.",
                        "proposal_id": proposal_id,
                    }
                ),
                200,
            )
        else:
            return jsonify({"error": "Failed to create proposal"}), 500

    except Exception as e:
        logger.error("Error saving proposal: %s", e)
        return jsonify({"error": "Failed to save proposal"}), 500


def _parse_change_proposal_entry(entry_data, id_keys):
    """Parse a change-proposal entry JSON string, returning (ke_id, resource_id).

    Handles the same single/double JSON serialisation quirk as submit_proposal.
    Returns (None, None) on parse failure so callers can 400.
    """
    try:
        if entry_data.startswith('"') and entry_data.endswith('"'):
            entry_data = json.loads(entry_data)
        entry_dict = json.loads(entry_data.replace("'", '"'))
        ke_id = entry_dict.get("ke_id") or entry_dict.get("KE_ID")
        resource_id = None
        for key in id_keys:
            if entry_dict.get(key):
                resource_id = entry_dict.get(key)
                break
        return entry_dict, ke_id, resource_id
    except (json.JSONDecodeError, AttributeError):
        return None, None, None


@api_bp.route("/submit_go_proposal", methods=["POST"])
@login_required
@submission_rate_limit
def submit_go_proposal():
    """Save a change/deletion proposal against an existing KE-GO mapping.

    Feeds the /admin/go-proposals review queue so corrections to approved GO
    mappings stay inside the auditable proposal workflow (issue #197), matching
    the WikiPathways "Propose Change" action.
    """
    try:
        proposal_data = {
            "entry": request.form.get("entry"),
            "userName": request.form.get("userName"),
            "userEmail": request.form.get("userEmail"),
            "userAffiliation": request.form.get("userAffiliation"),
            "deleteEntry": request.form.get("deleteEntry", ""),
            "changeConfidence": request.form.get("changeConfidence", ""),
            "changeType": request.form.get("changeType", ""),
        }

        is_valid, validated_data, errors = validate_request_data(
            GoProposalChangeSchema, proposal_data
        )
        if not is_valid:
            logger.warning("Invalid GO change proposal request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        entry_data = validated_data["entry"]
        user_name = SecurityValidation.sanitize_string(validated_data["userName"])
        user_email = validated_data["userEmail"]
        user_affiliation = SecurityValidation.sanitize_string(
            validated_data["userAffiliation"]
        )
        proposed_delete = validated_data["deleteEntry"] == "on"
        proposed_confidence = validated_data.get("changeConfidence") or None
        proposed_connection_type = validated_data.get("changeType") or None

        if not (proposed_delete or proposed_confidence or proposed_connection_type):
            return jsonify({"error": "No changes specified."}), 400

        if not SecurityValidation.validate_email_domain(user_email):
            return jsonify({"error": "Invalid email domain."}), 400

        entry_dict, ke_id, go_id = _parse_change_proposal_entry(
            entry_data, ("go_id", "GO_ID")
        )
        if entry_dict is None:
            return jsonify({"error": "Could not parse entry data."}), 400
        if not ke_id or not go_id:
            return jsonify({"error": "Invalid entry data format."}), 400

        if not go_proposal_model:
            return jsonify({"error": "GO mapping service unavailable"}), 503

        mapping_id = go_proposal_model.find_mapping_by_details(ke_id, go_id)
        if not mapping_id:
            return jsonify({"error": "Original mapping not found."}), 404

        provider_username = session.get("user", {}).get("username", "unknown")

        proposal_id = go_proposal_model.create_proposal(
            mapping_id=mapping_id,
            user_name=user_name,
            user_email=user_email,
            user_affiliation=user_affiliation,
            provider_username=provider_username,
            proposed_delete=proposed_delete,
            proposed_confidence=proposed_confidence,
            proposed_connection_type=proposed_connection_type,
            ke_id=ke_id,
            ke_title=entry_dict.get("ke_title"),
            go_id=go_id,
            go_name=entry_dict.get("go_name"),
        )

        if proposal_id:
            logger.info(
                "Created GO change proposal %s by %s for mapping %s",
                proposal_id, provider_username, mapping_id,
            )
            return jsonify({
                "message": "Proposal submitted successfully and is pending admin review.",
                "proposal_id": proposal_id,
            }), 200
        return jsonify({"error": "Failed to create proposal"}), 500

    except Exception as e:
        logger.error("Error saving GO proposal: %s", e)
        return jsonify({"error": "Failed to save proposal"}), 500


@api_bp.route("/submit_reactome_proposal", methods=["POST"])
@login_required
@submission_rate_limit
def submit_reactome_proposal():
    """Save a deletion proposal against an existing KE-Reactome mapping.

    Feeds the /admin/reactome-proposals review queue (issue #197). Reactome
    mappings have no connection type and their confidence is locked at proposal
    creation (D-02), so the only correction a change proposal can carry is a
    request to retire (delete) the mapping.
    """
    try:
        proposal_data = {
            "entry": request.form.get("entry"),
            "userName": request.form.get("userName"),
            "userEmail": request.form.get("userEmail"),
            "userAffiliation": request.form.get("userAffiliation"),
            "deleteEntry": request.form.get("deleteEntry", ""),
        }

        is_valid, validated_data, errors = validate_request_data(
            ReactomeProposalChangeSchema, proposal_data
        )
        if not is_valid:
            logger.warning("Invalid Reactome change proposal request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        entry_data = validated_data["entry"]
        user_name = SecurityValidation.sanitize_string(validated_data["userName"])
        user_email = validated_data["userEmail"]
        user_affiliation = SecurityValidation.sanitize_string(
            validated_data["userAffiliation"]
        )
        proposed_delete = validated_data["deleteEntry"] == "on"

        if not proposed_delete:
            return jsonify({"error": "No changes specified."}), 400

        if not SecurityValidation.validate_email_domain(user_email):
            return jsonify({"error": "Invalid email domain."}), 400

        entry_dict, ke_id, reactome_id = _parse_change_proposal_entry(
            entry_data, ("reactome_id", "reactomeId")
        )
        if entry_dict is None:
            return jsonify({"error": "Could not parse entry data."}), 400
        if not ke_id or not reactome_id:
            return jsonify({"error": "Invalid entry data format."}), 400

        if not reactome_proposal_model:
            return jsonify({"error": "Reactome mapping service unavailable"}), 503

        mapping_id = reactome_proposal_model.find_mapping_by_details(ke_id, reactome_id)
        if not mapping_id:
            return jsonify({"error": "Original mapping not found."}), 404

        provider_username = session.get("user", {}).get("username", "unknown")

        proposal_id = reactome_proposal_model.create_proposal(
            mapping_id=mapping_id,
            user_name=user_name,
            user_email=user_email,
            user_affiliation=user_affiliation,
            provider_username=provider_username,
            proposed_delete=proposed_delete,
            ke_id=ke_id,
            ke_title=entry_dict.get("ke_title"),
            reactome_id=reactome_id,
            pathway_name=entry_dict.get("pathway_name"),
        )

        if proposal_id:
            logger.info(
                "Created Reactome change proposal %s by %s for mapping %s",
                proposal_id, provider_username, mapping_id,
            )
            return jsonify({
                "message": "Proposal submitted successfully and is pending admin review.",
                "proposal_id": proposal_id,
            }), 200
        return jsonify({"error": "Failed to create proposal"}), 500

    except Exception as e:
        logger.error("Error saving Reactome proposal: %s", e)
        return jsonify({"error": "Failed to save proposal"}), 500


@api_bp.route("/suggest_pathways/<ke_id>", methods=["GET"])
@sparql_rate_limit
def suggest_pathways(ke_id):
    """
    Get pathway suggestions for a specific Key Event using multiple scoring methods

    Args:
        ke_id: Key Event ID from URL parameter

    Query Parameters:
        ke_title: Key Event title for text-based matching
        bio_level: Biological level of the KE (Molecular, Cellular, etc.)
        limit: Maximum number of suggestions (default 10)
        method_filter: Optional filter - 'all', 'gene', 'semantic' (default: 'all')

    Returns:
        JSON response with:
        - suggestions: Filtered and ranked pathway suggestions
        - method_filter: Current filter applied
        - total_count: Total number of suggestions before filtering
        - filtered_count: Number of suggestions after filtering
        - gene_based_suggestions: Pathways matched by gene overlap (if method_filter='all')
        - embedding_based_suggestions: Pathways matched by BioBERT semantic similarity (if method_filter='all')
        - combined_suggestions: Unified list with hybrid scores (if method_filter='all')
        - genes_found: Number of genes associated with the KE
        - gene_list: List of HGNC gene symbols
    """
    try:
        if not pathway_suggestion_service:
            logger.error("Pathway suggestion service not available")
            return jsonify({"error": "Suggestion service unavailable"}), 503

        # Get parameters
        ke_title = request.args.get('ke_title', '')
        bio_level = request.args.get('bio_level', '')
        limit = request.args.get('limit', 10, type=int)
        method_filter = request.args.get('method_filter', 'all')

        # Validate method filter
        valid_methods = ['all', 'gene', 'semantic']
        if method_filter not in valid_methods:
            method_filter = 'all'

        _log_method_filter_deprecation(method_filter, '/suggest_pathways')

        # Validate limit
        if limit > 50:
            limit = 50
        elif limit < 1:
            limit = 10

        # Validate KE ID format
        if not ke_id or len(ke_id.strip()) == 0:
            return jsonify({"error": "Invalid Key Event ID"}), 400

        logger.info("Getting pathway suggestions for KE: %s (bio_level: %s, method_filter: %s)", sanitize_log(ke_id), sanitize_log(bio_level), sanitize_log(method_filter))

        # Get all suggestions from service with biological level context
        all_suggestions = pathway_suggestion_service.get_pathway_suggestions(
            ke_id, ke_title, bio_level, limit
        )

        if "error" in all_suggestions:
            logger.error("Suggestion service error: %s", all_suggestions['error'])
            return jsonify(all_suggestions), 500

        # Get total count from combined suggestions
        combined = all_suggestions.get('combined_suggestions', [])
        total_count = len(combined)

        # Apply method filter using the pre-computed method-specific arrays
        if method_filter == 'all':
            # Return all suggestions with standard structure
            suggestions = combined
            response = all_suggestions
        else:
            # Use the dedicated method-specific suggestion arrays
            if method_filter == 'gene':
                suggestions = all_suggestions.get('gene_based_suggestions', [])
            elif method_filter == 'semantic':
                suggestions = all_suggestions.get('embedding_based_suggestions', [])
            else:
                suggestions = []

            response = {
                'suggestions': suggestions,
                'method_filter': method_filter,
                'total_count': total_count,
                'filtered_count': len(suggestions),
                'genes_found': all_suggestions.get('genes_found', 0),
                'gene_list': all_suggestions.get('gene_list', [])
            }

        # Add metadata
        response["request_info"] = {
            "ke_id": ke_id,
            "ke_title": ke_title,
            "limit": limit,
            "method_filter": method_filter,
            "timestamp": int(time.time())
        }

        logger.info("Returned %d suggestions for KE %s (filter: %s)", len(suggestions), sanitize_log(ke_id), sanitize_log(method_filter))
        return jsonify(response), 200

    except Exception as e:
        logger.error("Error getting pathway suggestions for %s: %s", sanitize_log(ke_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to get pathway suggestions"}), 500


@api_bp.route("/search_pathways", methods=["GET"])
@sparql_rate_limit 
def search_pathways():
    """
    Enhanced pathway search with fuzzy matching
    
    Query Parameters:
        q: Search query string (required)
        threshold: Minimum similarity threshold (0.0-1.0, default 0.4)
        limit: Maximum number of results (default 20)
        
    Returns:
        JSON response with matching pathways and relevance scores
    """
    try:
        if not pathway_suggestion_service:
            logger.error("Pathway suggestion service not available")
            return jsonify({"error": "Search service unavailable"}), 503
            
        # Get parameters
        query = request.args.get('q', '').strip()
        threshold = request.args.get('threshold', 0.4, type=float)
        limit = request.args.get('limit', 20, type=int)
        
        # Validate parameters
        if not query:
            return jsonify({"error": "Search query is required"}), 400
            
        if threshold < 0.1 or threshold > 1.0:
            threshold = 0.4
            
        if limit > 100:
            limit = 100
        elif limit < 1:
            limit = 20
            
        logger.info("Searching pathways with query: '%s', threshold: %s", sanitize_log(query), sanitize_log(threshold))
        
        # Perform search
        results = pathway_suggestion_service.search_pathways(
            query, threshold, limit
        )
        
        response = {
            "query": query,
            "threshold": threshold,
            "limit": limit,
            "results": results,
            "total_results": len(results),
            "timestamp": int(time.time())
        }
        
        logger.info("Found %d pathways matching '%s'", len(results), sanitize_log(query))
        return jsonify(response), 200
        
    except Exception as e:
        logger.error("Error searching pathways: %s", e)
        return jsonify({"error": "Failed to search pathways"}), 500


@api_bp.route("/ke_genes/<ke_id>", methods=["GET"])
@sparql_rate_limit
def get_ke_genes(ke_id):
    """
    Get genes associated with a specific Key Event
    
    Args:
        ke_id: Key Event ID from URL parameter
        
    Returns:
        JSON response with list of HGNC gene symbols
    """
    try:
        if not pathway_suggestion_service:
            logger.error("Pathway suggestion service not available")
            return jsonify({"error": "Service unavailable"}), 503
            
        # Validate KE ID
        if not ke_id or len(ke_id.strip()) == 0:
            return jsonify({"error": "Invalid Key Event ID"}), 400
            
        logger.info("Getting genes for KE: %s", sanitize_log(ke_id))
        
        # Get genes from service (Phase 28: returns List[Dict] with {ncbi, hgnc, symbol})
        genes_full = pathway_suggestion_service._get_genes_from_ke(ke_id)
        # Backward-compat: legacy `genes` is a list of HGNC symbol strings (Phase 27 frontend reads this)
        genes = [g["symbol"] for g in genes_full]

        response = {
            "ke_id": ke_id,
            "genes": genes,
            "genes_full": genes_full,
            "gene_count": len(genes_full),
            "timestamp": int(time.time())
        }

        logger.info("Found %d genes for KE %s", len(genes_full), sanitize_log(ke_id))
        return jsonify(response), 200
        
    except Exception as e:
        logger.error("Error getting genes for KE %s: %s", sanitize_log(ke_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to get KE genes"}), 500


@api_bp.route("/api/scoring-config", methods=["GET"])
@general_rate_limit
def get_scoring_config():
    """
    Serve frontend scoring configuration

    Returns:
        JSON containing ke_pathway_assessment configuration section
        Cached for 5 minutes to reduce file I/O
    """
    global _config_cache, _config_cache_time

    try:
        now = time.time()

        # Check cache
        if _config_cache and _config_cache_time:
            if now - _config_cache_time < _config_cache_ttl:
                logger.debug("Serving scoring config from cache")
                return jsonify(_config_cache), 200

        # Load fresh config
        config = ConfigLoader.load_config()

        # Extract frontend-relevant section
        ke_assessment = config.ke_pathway_assessment

        # Get quality tiers for UI badges
        quality_tiers = config.pathway_suggestion.confidence_scoring.quality_tiers

        response = {
            "version": config.metadata.get("version", "1.0.0"),
            "ke_pathway_assessment": {
                "evidence_quality": ke_assessment.evidence_quality,
                "pathway_specificity": ke_assessment.pathway_specificity,
                "ke_coverage": ke_assessment.ke_coverage,
                "biological_level": ke_assessment.biological_level,
                "confidence_thresholds": ke_assessment.confidence_thresholds,
                "max_scores": ke_assessment.max_scores,
                "connection_types": ke_assessment.connection_types,
            },
            "pathway_suggestion": {
                "quality_tiers": quality_tiers
            },
            "metadata": {
                "loaded_at": datetime.utcnow().isoformat() + "Z",
                "source": "scoring_config.yaml"
            }
        }

        # Update cache
        _config_cache = response
        _config_cache_time = now

        logger.info("Scoring configuration served to frontend")
        return jsonify(response), 200

    except Exception as e:
        logger.error("Error serving scoring config: %s", e)
        # Return defaults on error (200 with defaults, not 500)
        default_config = ConfigLoader.get_default_config()
        ke_assessment = default_config.ke_pathway_assessment

        quality_tiers = default_config.pathway_suggestion.confidence_scoring.quality_tiers

        return jsonify({
            "version": "1.0.0-default",
            "ke_pathway_assessment": {
                "evidence_quality": ke_assessment.evidence_quality,
                "pathway_specificity": ke_assessment.pathway_specificity,
                "ke_coverage": ke_assessment.ke_coverage,
                "biological_level": ke_assessment.biological_level,
                "confidence_thresholds": ke_assessment.confidence_thresholds,
                "max_scores": ke_assessment.max_scores,
                "connection_types": ke_assessment.connection_types,
            },
            "pathway_suggestion": {
                "quality_tiers": quality_tiers
            },
            "metadata": {
                "loaded_at": datetime.utcnow().isoformat() + "Z",
                "source": "default",
                "error": "Failed to load config file, using defaults"
            }
        }), 200


@api_bp.route("/get_aop_options", methods=["GET"])
@sparql_rate_limit
def get_aop_options():
    """Fetch AOP (Adverse Outcome Pathway) options from AOP-Wiki SPARQL endpoint"""
    try:
        sparql_query = """
        PREFIX aopo: <http://aopkb.org/aop_ontology#>
        PREFIX dc: <http://purl.org/dc/elements/1.1/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

        SELECT DISTINCT ?aop ?aopId ?aopTitle
        WHERE {
            ?aop a aopo:AdverseOutcomePathway ;
                 rdfs:label ?aopId ;
                 dc:title ?aopTitle .
        }
        ORDER BY ?aopId
        """
        endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

        # Check cache first
        query_hash = hashlib.md5(sparql_query.encode()).hexdigest()
        cached_response = cache_model.get_cached_response(endpoint, query_hash)

        if cached_response:
            logger.info("Serving AOP options from cache")
            return jsonify(json.loads(cached_response)), 200

        response = requests.post(
            endpoint,
            data={"query": sparql_query},
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )

        if response.status_code == 200:
            data = response.json()
            if "results" not in data or "bindings" not in data["results"]:
                logger.error("Invalid SPARQL response format for AOP options")
                return jsonify({"error": "Invalid response from AOP service"}), 500

            results = [
                {
                    "aopId": binding.get("aopId", {}).get("value", ""),
                    "aopTitle": binding.get("aopTitle", {}).get("value", ""),
                }
                for binding in data["results"]["bindings"]
                if all(key in binding for key in ["aopId", "aopTitle"])
            ]

            # Sort results by AOP ID numerically
            results.sort(key=lambda x: int(x["aopId"].replace("AOP ", "")) if x["aopId"].replace("AOP ", "").isdigit() else 0)

            # Cache the response for 24 hours
            cache_model.cache_response(endpoint, query_hash, json.dumps(results), 24)
            logger.info("Fetched and cached %d AOP options", len(results))
            return jsonify(results), 200
        else:
            logger.error(
                "SPARQL AOP Query Failed: %s - %s", response.status_code, response.text
            )
            return jsonify({"error": "Failed to fetch AOP options"}), 500
    except requests.exceptions.Timeout:
        logger.error("SPARQL AOP request timeout")
        return jsonify({"error": "Service timeout - please try again"}), 503
    except Exception as e:
        logger.error("Error fetching AOP options: %s", e)
        return jsonify({"error": "Failed to fetch AOP options"}), 500


@api_bp.route("/get_aop_kes/<aop_id>", methods=["GET"])
@sparql_rate_limit
def get_aop_kes(aop_id):
    """Fetch Key Events for a specific AOP from AOP-Wiki SPARQL endpoint"""
    try:
        # Validate AOP ID format (should be like "AOP 1" or just a number)
        if not aop_id:
            return jsonify({"error": "AOP ID is required"}), 400

        # Normalize AOP ID format
        if aop_id.isdigit():
            aop_label = f"AOP {aop_id}"
        else:
            aop_label = aop_id

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
        endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

        # Check cache first (using AOP ID in the hash for uniqueness)
        cache_key = f"aop_kes_{aop_label}"
        query_hash = hashlib.md5(cache_key.encode()).hexdigest()
        cached_response = cache_model.get_cached_response(endpoint, query_hash)

        if cached_response:
            logger.info("Serving KEs for %s from cache", sanitize_log(aop_label))
            return jsonify(json.loads(cached_response)), 200

        response = requests.post(
            endpoint,
            data={"query": sparql_query},
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )

        if response.status_code == 200:
            data = response.json()
            if "results" not in data or "bindings" not in data["results"]:
                logger.error("Invalid SPARQL response format for AOP %s KEs", sanitize_log(aop_label))
                return jsonify({"error": "Invalid response from AOP service"}), 500

            results = [
                {
                    "KElabel": binding.get("keId", {}).get("value", ""),
                    "KEtitle": binding.get("keTitle", {}).get("value", ""),
                    "biolevel": binding.get("biolevel", {}).get("value", ""),
                    "KEpage": binding.get("kePage", {}).get("value", ""),
                }
                for binding in data["results"]["bindings"]
                if all(key in binding for key in ["keId", "keTitle"])
            ]

            # Sort results by KE ID numerically
            results.sort(key=lambda x: int(x["KElabel"].replace("KE ", "")) if x["KElabel"].replace("KE ", "").isdigit() else 0)

            # Cache the response for 24 hours
            cache_model.cache_response(endpoint, query_hash, json.dumps(results), 24)
            logger.info("Fetched and cached %d KEs for %s", len(results), sanitize_log(aop_label))
            return jsonify(results), 200
        else:
            logger.error(
                "SPARQL AOP KE Query Failed: %s - %s", response.status_code, response.text
            )
            return jsonify({"error": "Failed to fetch KEs for AOP"}), 500
    except requests.exceptions.Timeout:
        logger.error("SPARQL AOP KE request timeout")
        return jsonify({"error": "Service timeout - please try again"}), 503
    except Exception as e:
        logger.error("Error fetching KEs for AOP %s: %s", sanitize_log(aop_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to fetch KEs for AOP"}), 500


# ==============================================================================
# KE Context Endpoint
# ==============================================================================


@api_bp.route("/api/ke_context/<ke_id>", methods=["GET"])
@general_rate_limit
def get_ke_context(ke_id):
    """
    Get context for a Key Event: AOP membership and existing mappings.

    Args:
        ke_id: Key Event ID (e.g. "KE 55")

    Returns:
        JSON with aop_membership, wp_mappings, go_mappings, and summary counts
    """
    try:
        if not ke_id or len(ke_id.strip()) == 0:
            return jsonify({"error": "Invalid Key Event ID"}), 400

        result = {
            "ke_id": ke_id,
            "aop_membership": [],
            "wp_mappings": [],
            "go_mappings": [],
            "summary": {"aop_count": 0, "wp_mapping_count": 0, "go_mapping_count": 0}
        }

        # Get AOP membership via SPARQL (with caching)
        try:
            ke_number = ke_id.replace("KE ", "").strip()
            aop_query = f"""
            PREFIX aopo: <http://aopkb.org/aop_ontology#>
            PREFIX dc: <http://purl.org/dc/elements/1.1/>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

            SELECT DISTINCT ?aopId ?aopTitle
            WHERE {{
                ?aop a aopo:AdverseOutcomePathway ;
                     rdfs:label ?aopId ;
                     dc:title ?aopTitle ;
                     aopo:has_key_event ?ke .
                ?ke rdfs:label "KE {ke_number}" .
            }}
            ORDER BY ?aopId
            """
            endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"
            cache_key = f"ke_context_aops_{ke_id}"
            query_hash = hashlib.md5(cache_key.encode()).hexdigest()

            cached = cache_model.get_cached_response(endpoint, query_hash) if cache_model else None
            if cached:
                result["aop_membership"] = json.loads(cached)
            else:
                response = requests.post(
                    endpoint,
                    data={"query": aop_query},
                    headers={
                        "Accept": "application/sparql-results+json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=15,
                )
                if response.status_code == 200:
                    data = response.json()
                    aops = [
                        {
                            "aop_id": b.get("aopId", {}).get("value", ""),
                            "aop_title": b.get("aopTitle", {}).get("value", ""),
                        }
                        for b in data.get("results", {}).get("bindings", [])
                        if "aopId" in b
                    ]
                    result["aop_membership"] = aops
                    if cache_model:
                        cache_model.cache_response(endpoint, query_hash, json.dumps(aops), 24)
        except Exception as e:
            logger.warning("Could not fetch AOP membership for %s: %s", sanitize_log(ke_id), e)

        # Get existing WP mappings from database
        if mapping_model:
            try:
                wp_mappings = mapping_model.get_mappings_by_ke(ke_id)
                result["wp_mappings"] = [
                    {
                        "wp_id": m["wp_id"],
                        "wp_title": m["wp_title"],
                        "confidence_level": m["confidence_level"],
                    }
                    for m in wp_mappings
                ]
            except Exception as e:
                logger.warning("Could not fetch WP mappings for %s: %s", sanitize_log(ke_id), e)

        # Get existing GO mappings from database
        if go_mapping_model:
            try:
                go_mappings = go_mapping_model.get_mappings_by_ke(ke_id)
                result["go_mappings"] = [
                    {
                        "go_id": m["go_id"],
                        "go_name": m["go_name"],
                        "confidence_level": m["confidence_level"],
                    }
                    for m in go_mappings
                ]
            except Exception as e:
                logger.warning("Could not fetch GO mappings for %s: %s", sanitize_log(ke_id), e)

        # Update summary counts
        result["summary"] = {
            "aop_count": len(result["aop_membership"]),
            "wp_mapping_count": len(result["wp_mappings"]),
            "go_mapping_count": len(result["go_mappings"]),
        }

        return jsonify(result), 200

    except Exception as e:
        logger.error("Error getting KE context for %s: %s", sanitize_log(ke_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to get KE context"}), 500


# ==============================================================================
# KE-GO Mapping Endpoints
# ==============================================================================


@api_bp.route("/suggest_go_terms/<ke_id>", methods=["GET"])
@sparql_rate_limit
def suggest_go_terms(ke_id):
    """
    Get GO Biological Process term suggestions for a specific Key Event

    Args:
        ke_id: Key Event ID from URL parameter

    Query Parameters:
        ke_title: Key Event title for text-based matching
        limit: Maximum number of suggestions (default 20)
        method_filter: 'all', 'text', or 'gene' (default: 'all')

    Returns:
        JSON response with GO term suggestions and scores
    """
    try:
        if not go_suggestion_service:
            logger.error("GO suggestion service not available")
            return jsonify({"error": "GO suggestion service unavailable"}), 503

        ke_title = request.args.get('ke_title', '')
        limit = request.args.get('limit', 20, type=int)
        method_filter = request.args.get('method_filter', 'all')
        aspect_filter = request.args.get('aspect_filter', 'all')

        # Validate method filter
        if method_filter not in ('all', 'text', 'gene'):
            method_filter = 'all'

        _log_method_filter_deprecation(method_filter, '/suggest_go_terms')

        # Validate aspect filter
        if aspect_filter not in ('all', 'bp', 'mf'):
            aspect_filter = 'all'

        # Validate limit
        limit = max(1, min(50, limit))

        if not ke_id or len(ke_id.strip()) == 0:
            return jsonify({"error": "Invalid Key Event ID"}), 400

        logger.info(
            "Getting GO suggestions for KE: %s (method_filter: %s, aspect_filter: %s)",
            sanitize_log(ke_id), sanitize_log(method_filter), sanitize_log(aspect_filter),
        )

        result = go_suggestion_service.get_go_suggestions(
            ke_id, ke_title, limit, method_filter, aspect_filter
        )

        if "error" in result:
            return jsonify(result), 500

        result["request_info"] = {
            "ke_id": ke_id,
            "ke_title": ke_title,
            "limit": limit,
            "method_filter": method_filter,
            "timestamp": int(time.time())
        }

        logger.info("Returned %d GO suggestions for KE %s", len(result.get('suggestions', [])), sanitize_log(ke_id))
        return jsonify(result), 200

    except Exception as e:
        logger.error("Error getting GO suggestions for %s: %s", sanitize_log(ke_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to get GO suggestions"}), 500


@api_bp.route("/search_go_terms", methods=["GET"])
@sparql_rate_limit
def search_go_terms():
    """
    Search GO terms with fuzzy matching

    Query Parameters:
        q: Search query string (required)
        threshold: Minimum similarity threshold (0.0-1.0, default 0.4)
        limit: Maximum number of results (default 10)

    Returns:
        JSON response with matching GO terms and relevance scores
    """
    try:
        if not go_suggestion_service:
            logger.error("GO suggestion service not available")
            return jsonify({"error": "Search service unavailable"}), 503

        query = request.args.get('q', '').strip()
        threshold = request.args.get('threshold', 0.4, type=float)
        limit = request.args.get('limit', 10, type=int)

        if not query:
            return jsonify({"error": "Search query is required"}), 400

        if threshold < 0.1 or threshold > 1.0:
            threshold = 0.4

        if limit > 100:
            limit = 100
        elif limit < 1:
            limit = 10

        logger.info("Searching GO terms with query: '%s', threshold: %s", sanitize_log(query), sanitize_log(threshold))

        results = go_suggestion_service.search_go_terms(query, threshold, limit)

        response = {
            "query": query,
            "threshold": threshold,
            "limit": limit,
            "results": results,
            "total_results": len(results),
            "timestamp": int(time.time())
        }

        logger.info("Found %d GO terms matching '%s'", len(results), sanitize_log(query))
        return jsonify(response), 200

    except Exception as e:
        logger.error("Error searching GO terms: %s", e)
        return jsonify({"error": "Failed to search GO terms"}), 500


@api_bp.route("/submit_go_mapping", methods=["POST"])
@submission_rate_limit
@login_required
def submit_go_mapping():
    """Submit a new KE-GO mapping entry"""
    try:
        submit_data = {
            "ke_id": request.form.get("ke_id"),
            "ke_title": request.form.get("ke_title"),
            "go_id": request.form.get("go_id"),
            "go_name": request.form.get("go_name"),
            "connection_type": request.form.get("connection_type"),
            "confidence_level": request.form.get("confidence_level"),
            "go_namespace": request.form.get("go_namespace", "biological_process"),
        }

        is_valid, validated_data, errors = validate_request_data(
            GoMappingSchema, submit_data
        )

        if not is_valid:
            logger.warning("Invalid GO submit request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        ke_id = SecurityValidation.sanitize_string(validated_data["ke_id"])
        ke_title = SecurityValidation.sanitize_string(validated_data["ke_title"])
        go_id = SecurityValidation.sanitize_string(validated_data["go_id"])
        go_name = SecurityValidation.sanitize_string(validated_data["go_name"])
        connection_type = validated_data["connection_type"]
        confidence_level = validated_data["confidence_level"]
        go_namespace = validated_data.get("go_namespace", "biological_process")

        created_by = session.get("user", {}).get("username", "anonymous")

        if created_by != "anonymous" and not SecurityValidation.validate_username(created_by):
            return jsonify({"error": "Authentication error"}), 401

        if not go_proposal_model:
            return jsonify({"error": "GO mapping service unavailable"}), 503

        # Capture suggestion_score from form (same pattern as /submit at api.py)
        suggestion_score_raw = request.form.get("suggestion_score")
        try:
            suggestion_score = float(suggestion_score_raw) if suggestion_score_raw else None
        except (ValueError, TypeError):
            suggestion_score = None

        # Capture optional dimension scores from form
        def _parse_int(val):
            try:
                return int(val) if val is not None else None
            except (ValueError, TypeError):
                return None

        connection_score = _parse_int(request.form.get("connection_score"))
        specificity_score = _parse_int(request.form.get("specificity_score"))
        evidence_score = _parse_int(request.form.get("evidence_score"))

        proposal_id = go_proposal_model.create_new_pair_go_proposal(
            ke_id=ke_id,
            ke_title=ke_title,
            go_id=go_id,
            go_name=go_name,
            connection_type=connection_type,
            confidence_level=confidence_level,
            provider_username=created_by,
            suggestion_score=suggestion_score,
            connection_score=connection_score,
            specificity_score=specificity_score,
            evidence_score=evidence_score,
            go_namespace=go_namespace,
        )
        # Phase 32 H-2 port: the partial-unique index on
        # ke_go_proposals(ke_id, go_id) WHERE status='pending' AND mapping_id IS NULL
        # rejects concurrent duplicate submits. Surface as 409 using the
        # existing check_go_mapping_exists_with_proposals shape (which GO
        # clients already handle via /check_go_entry) rather than Reactome's
        # verbatim {error, blocking_type} shape — see CONTEXT.md L34-39.
        if proposal_id == GoProposalModel.DUPLICATE_PENDING:
            if not go_mapping_model:
                return jsonify({"error": "GO mapping service unavailable"}), 503
            dup_payload = go_mapping_model.check_go_mapping_exists_with_proposals(
                ke_id, go_id
            )
            logger.info(
                "Duplicate-pending /submit_go_mapping blocked at DB layer: "
                "%s -> %s by %s", ke_id, go_id, created_by,
            )
            return jsonify(dup_payload), 409

        if proposal_id:
            logger.info(
                "New GO mapping proposal created: %s -> %s by %s (proposal #%s)",
                ke_id, go_id, created_by, proposal_id,
            )
            return jsonify({
                "message": "GO mapping proposal submitted and is pending admin review.",
                "proposal_id": proposal_id,
            }), 200
        else:
            return jsonify({"error": "Failed to create GO mapping proposal"}), 500

    except Exception as e:
        logger.error("Error submitting GO mapping proposal: %s", e)
        return jsonify({"error": "Failed to submit GO mapping proposal"}), 500


@api_bp.route("/check_go_entry", methods=["POST"])
@general_rate_limit
def check_go_entry():
    """Check if the KE-GO pair already exists"""
    try:
        check_data = {
            "ke_id": request.form.get("ke_id"),
            "go_id": request.form.get("go_id"),
        }

        is_valid, validated_data, errors = validate_request_data(
            GoCheckEntrySchema, check_data
        )

        if not is_valid:
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        if not go_mapping_model:
            return jsonify({"error": "GO mapping service unavailable"}), 503

        result = go_mapping_model.check_go_mapping_exists_with_proposals(
            validated_data["ke_id"], validated_data["go_id"]
        )
        return jsonify(result), 200

    except Exception as e:
        logger.error("Error checking GO entry: %s", e)
        return jsonify({"error": "Failed to check GO entry"}), 500


# ==============================================================================
# KE-Reactome Mapping Endpoints
# ==============================================================================


@api_bp.route("/submit_reactome_mapping", methods=["POST"])
@submission_rate_limit
@login_required
def submit_reactome_mapping():
    """Submit a new KE-Reactome mapping proposal (Phase 25).

    Phase 37 ASMT-04: reads step1-4 + connection_type from the form payload,
    mirrors the WP /submit handler pattern (api.py:143-268). The four step*
    values are renamed to proposed_relationship/basis/specificity/coverage
    before forwarding to the model (which already accepts them). If
    connection_type is absent it is derived server-side from step1 using the
    same step1->connection_type identity mapping as the WP evaluatePathway-
    Confidence JS function (step1 IS the raw connection type).
    """
    try:
        submit_data = {
            "ke_id": request.form.get("ke_id"),
            "ke_title": request.form.get("ke_title"),
            "reactome_id": request.form.get("reactome_id"),
            "pathway_name": request.form.get("pathway_name"),
            "species": request.form.get("species", "Homo sapiens"),
            "confidence_level": request.form.get("confidence_level"),
            "step1": request.form.get("step1"),
            "step2": request.form.get("step2"),
            "step3": request.form.get("step3"),
            "step4": request.form.get("step4"),
            "connection_type": request.form.get("connection_type"),
        }
        # Drop None values so Marshmallow's required=False semantics fire
        # (mirror WP /submit handler at api.py:164).
        submit_data = {k: v for k, v in submit_data.items() if v is not None}

        is_valid, validated_data, errors = validate_request_data(
            ReactomeMappingSchema, submit_data
        )

        if not is_valid:
            logger.warning("Invalid Reactome submit request: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        ke_id = SecurityValidation.sanitize_string(validated_data["ke_id"])
        ke_title = SecurityValidation.sanitize_string(validated_data["ke_title"])
        reactome_id = SecurityValidation.sanitize_string(validated_data["reactome_id"])
        pathway_name = SecurityValidation.sanitize_string(validated_data["pathway_name"])
        species = SecurityValidation.sanitize_string(
            validated_data.get("species", "Homo sapiens")
        )
        confidence_level = validated_data["confidence_level"]

        # Phase 37 ASMT-04: extract step answers and rename to DB column names
        # (mirror WP /submit handler at api.py:188-203).
        step1 = validated_data.get("step1")
        step2 = validated_data.get("step2")
        step3 = validated_data.get("step3")
        step4 = validated_data.get("step4")
        proposed_relationship = (
            SecurityValidation.sanitize_string(step1) if step1 else None
        )
        proposed_basis = (
            SecurityValidation.sanitize_string(step2) if step2 else None
        )
        proposed_specificity = (
            SecurityValidation.sanitize_string(step3) if step3 else None
        )
        proposed_coverage = (
            SecurityValidation.sanitize_string(step4) if step4 else None
        )

        created_by = session.get("user", {}).get("username", "anonymous")

        if created_by != "anonymous" and not SecurityValidation.validate_username(created_by):
            return jsonify({"error": "Authentication error"}), 401

        if not reactome_proposal_model:
            return jsonify({"error": "Reactome mapping service unavailable"}), 503

        suggestion_score_raw = request.form.get("suggestion_score")
        try:
            suggestion_score = float(suggestion_score_raw) if suggestion_score_raw else None
        except (ValueError, TypeError):
            suggestion_score = None

        proposal_id = reactome_proposal_model.create_new_pair_reactome_proposal(
            ke_id=ke_id,
            ke_title=ke_title,
            reactome_id=reactome_id,
            pathway_name=pathway_name,
            confidence_level=confidence_level,
            species=species,
            provider_username=created_by,
            suggestion_score=suggestion_score,
            proposed_relationship=proposed_relationship,
            proposed_basis=proposed_basis,
            proposed_specificity=proposed_specificity,
            proposed_coverage=proposed_coverage,
        )

        # Phase 25 review H-2: the partial-unique index on
        # ke_reactome_proposals(ke_id, reactome_id) WHERE status='pending'
        # rejects concurrent duplicate submits. Surface as a clear 409.
        from src.core.models import ReactomeProposalModel
        if proposal_id == ReactomeProposalModel.DUPLICATE_PENDING:
            return jsonify({
                "error": (
                    "A pending proposal for this KE-Reactome pair already "
                    "exists. Wait for it to be reviewed before submitting "
                    "another."
                ),
                "blocking_type": "pending_proposal",
            }), 409

        if proposal_id:
            logger.info(
                "New Reactome mapping proposal created: %s -> %s by %s (proposal #%s)",
                sanitize_log(ke_id), sanitize_log(reactome_id),
                sanitize_log(created_by), proposal_id,
            )
            return jsonify({
                "message": "Reactome mapping proposal submitted and is pending admin review.",
                "proposal_id": proposal_id,
            }), 200
        else:
            return jsonify({"error": "Failed to create Reactome mapping proposal"}), 500

    except Exception as e:
        logger.error("Error submitting Reactome mapping: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to submit Reactome mapping"}), 500


@api_bp.route("/check_reactome_entry", methods=["POST"])
@general_rate_limit
def check_reactome_entry():
    """Check if the KE-Reactome pair already exists (approved or pending)."""
    try:
        check_data = {
            "ke_id": request.form.get("ke_id"),
            "reactome_id": request.form.get("reactome_id"),
        }

        is_valid, validated_data, errors = validate_request_data(
            ReactomeCheckEntrySchema, check_data
        )

        if not is_valid:
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        if not reactome_mapping_model:
            return jsonify({"error": "Reactome mapping service unavailable"}), 503

        result = reactome_mapping_model.check_reactome_mapping_exists_with_proposals(
            validated_data["ke_id"], validated_data["reactome_id"]
        )
        return jsonify(result), 200

    except Exception as e:
        logger.error("Error checking Reactome entry: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to check Reactome entry"}), 500


@api_bp.route("/suggest_reactome/<ke_id>", methods=["GET"])
@sparql_rate_limit
def suggest_reactome(ke_id):
    """Get Reactome pathway suggestions for a Key Event."""
    try:
        if not reactome_suggestion_service:
            logger.error("Reactome suggestion service not available")
            return jsonify({"error": "Reactome suggestion service unavailable"}), 503

        ke_title = request.args.get('ke_title', '')
        limit = request.args.get('limit', 20, type=int)
        limit = max(1, min(50, limit))
        method_filter = request.args.get('method_filter', 'all')

        # Validate method filter (accepted values mirror WP endpoint)
        if method_filter not in ('all', 'text', 'gene'):
            method_filter = 'all'

        _log_method_filter_deprecation(method_filter, '/suggest_reactome')

        if not ke_id or len(ke_id.strip()) == 0:
            return jsonify({"error": "Invalid Key Event ID"}), 400

        logger.info(
            "Getting Reactome suggestions for KE: %s (method_filter: %s)",
            sanitize_log(ke_id), sanitize_log(method_filter),
        )

        result = reactome_suggestion_service.get_reactome_suggestions(
            ke_id, ke_title, limit
        )

        if "error" in result:
            return jsonify(result), 500

        result["request_info"] = {
            "ke_id": ke_id,
            "ke_title": ke_title,
            "limit": limit,
            "method_filter": method_filter,
            "timestamp": int(time.time()),
        }

        logger.info(
            "Returned %d Reactome suggestions for KE %s",
            len(result.get('suggestions', [])), sanitize_log(ke_id),
        )
        return jsonify(result), 200

    except Exception as e:
        logger.error(
            "Error getting Reactome suggestions for %s: %s",
            sanitize_log(ke_id), sanitize_log(str(e)),
        )
        return jsonify({"error": "Failed to get Reactome suggestions"}), 500


@api_bp.route("/search_reactome", methods=["GET"])
@sparql_rate_limit
def search_reactome():
    """Search Reactome pathways with fuzzy matching."""
    try:
        if not reactome_suggestion_service:
            logger.error("Reactome suggestion service not available")
            return jsonify({"error": "Search service unavailable"}), 503

        query = request.args.get('q', '').strip()
        threshold = request.args.get('threshold', 0.4, type=float)
        limit = request.args.get('limit', 10, type=int)

        if not query:
            return jsonify({"error": "Search query is required"}), 400

        if threshold < 0.1 or threshold > 1.0:
            threshold = 0.4

        if limit > 100:
            limit = 100
        elif limit < 1:
            limit = 10

        logger.info(
            "Searching Reactome pathways with query: '%s', threshold: %s",
            sanitize_log(query), sanitize_log(threshold),
        )

        results = reactome_suggestion_service.search_reactome_terms(
            query, threshold, limit
        )

        response = {
            "query": query,
            "threshold": threshold,
            "limit": limit,
            "results": results,
            "total_results": len(results),
            "timestamp": int(time.time()),
        }

        logger.info(
            "Found %d Reactome pathways matching '%s'",
            len(results), sanitize_log(query),
        )
        return jsonify(response), 200

    except Exception as e:
        logger.error("Error searching Reactome pathways: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to search Reactome pathways"}), 500


@api_bp.route("/flag_proposal_stale", methods=["POST"])
@submission_rate_limit
@login_required
def flag_proposal_stale():
    """Flag a pending proposal as stale for admin review."""
    try:
        proposal_id = request.form.get("proposal_id", type=int)
        mapping_type = request.form.get("mapping_type", "wp")  # "wp" or "go"

        if not proposal_id:
            return jsonify({"error": "proposal_id is required"}), 400

        flagged_by = session.get("user", {}).get("username", "unknown")

        if mapping_type == "go":
            if not go_proposal_model:
                return jsonify({"error": "GO proposal service unavailable"}), 503
            ok = go_proposal_model.flag_go_proposal_stale(proposal_id, flagged_by)
        else:
            ok = proposal_model.flag_proposal_stale(proposal_id, flagged_by)

        if ok:
            logger.info("Proposal %s flagged stale by %s", sanitize_log(proposal_id), sanitize_log(flagged_by))
            return jsonify({"message": "Proposal flagged as stale for admin review."}), 200
        else:
            return jsonify({"error": "Failed to flag proposal"}), 500
    except Exception as e:
        logger.error("Error flagging proposal stale: %s", e)
        return jsonify({"error": "Failed to flag proposal"}), 500


@api_bp.route("/api/go-scoring-config", methods=["GET"])
@general_rate_limit
def get_go_scoring_config():
    """
    Serve GO assessment scoring configuration for frontend

    Returns:
        JSON containing ke_go_assessment configuration section
    """
    try:
        config = ConfigLoader.load_config()

        ke_go = config.ke_go_assessment

        response = {
            "version": config.metadata.get("version", "1.0.0"),
            "ke_go_assessment": {
                "term_specificity": ke_go.term_specificity,
                "evidence_support": ke_go.evidence_support,
                "gene_overlap": ke_go.gene_overlap,
                "bio_level_bonus": ke_go.bio_level_bonus,
                "confidence_thresholds": ke_go.confidence_thresholds,
                "max_scores": ke_go.max_scores,
                "connection_types": ke_go.connection_types,
                "dimension_weights": ke_go.dimension_weights,
                "dimension_thresholds": ke_go.dimension_thresholds,
            },
            "metadata": {
                "loaded_at": datetime.utcnow().isoformat() + "Z",
                "source": "scoring_config.yaml"
            }
        }

        return jsonify(response), 200

    except Exception as e:
        logger.error("Error serving GO scoring config: %s", e)
        default_config = ConfigLoader.get_default_config()
        ke_go = default_config.ke_go_assessment

        return jsonify({
            "version": "1.0.0-default",
            "ke_go_assessment": {
                "term_specificity": ke_go.term_specificity,
                "evidence_support": ke_go.evidence_support,
                "gene_overlap": ke_go.gene_overlap,
                "bio_level_bonus": ke_go.bio_level_bonus,
                "confidence_thresholds": ke_go.confidence_thresholds,
                "max_scores": ke_go.max_scores,
                "connection_types": ke_go.connection_types,
                "dimension_weights": ke_go.dimension_weights,
                "dimension_thresholds": ke_go.dimension_thresholds,
            },
            "metadata": {
                "loaded_at": datetime.utcnow().isoformat() + "Z",
                "source": "default"
            }
        }), 200