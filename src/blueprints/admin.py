"""
Admin Blueprint
Handles administrative functions for proposal management
"""
import logging
import os
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, current_app, jsonify, render_template, request, session

from src.utils.timezone import utc_to_local
from src.utils.text import sanitize_log

from src.services.monitoring import monitor_performance
from src.services.rate_limiter import submission_rate_limit
from src.core.schemas import AdminNotesSchema, SecurityValidation, validate_request_data

logger = logging.getLogger(__name__)


def _source_version_fields(resource: str) -> dict:
    """
    Resolve the upstream source-version fields to stamp on a new approval.

    Wraps `current_app.service_container.source_version_fields_for(resource)`
    with a safety net: if the container is unavailable or raises, returns an
    empty dict so the approval still goes through (the mapping just gets
    NULL version columns rather than failing).

    Phase C of source-data versioning (DMP §7).
    """
    try:
        return current_app.service_container.source_version_fields_for(resource)
    except Exception as e:
        logger.warning(
            "Could not resolve source-version fields for %s: %s — approval "
            "will proceed with NULL version columns",
            resource, e,
        )
        return {}


def _get_admin_users():
    """Return list of provider-prefixed admin usernames from ADMIN_USERS env var.

    Entries without a ':' separator are auto-prefixed as 'github:' for
    backward compatibility (e.g. 'mmartens' becomes 'github:mmartens').
    """
    raw = os.getenv("ADMIN_USERS", "").split(",")
    result = []
    for entry in raw:
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning(
                "ADMIN_USERS entry '%s' has no provider prefix — auto-prefixing as 'github:%s'",
                entry, entry,
            )
            entry = f"github:{entry}"
        result.append(entry)
    return result


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# Global model instances (will be set by app initialization)
proposal_model = None
mapping_model = None
guest_code_model = None
go_mapping_model = None
go_proposal_model = None
cache_model_ref = None
ke_override_model = None
reactome_mapping_model = None
reactome_proposal_model = None


def set_models(proposal, mapping, guest_code=None, go_mapping=None, go_proposal=None,
               cache_model=None, ke_override=None,
               reactome_mapping=None, reactome_proposal=None):
    """Set the model instances"""
    global proposal_model, mapping_model, guest_code_model, go_mapping_model, go_proposal_model
    global cache_model_ref, ke_override_model
    global reactome_mapping_model, reactome_proposal_model
    proposal_model = proposal
    mapping_model = mapping
    guest_code_model = guest_code
    go_mapping_model = go_mapping
    go_proposal_model = go_proposal
    cache_model_ref = cache_model
    ke_override_model = ke_override
    reactome_mapping_model = reactome_mapping
    reactome_proposal_model = reactome_proposal


def _compute_confidence_from_dimensions(conn_score, spec_score, ev_score, ke_go_config):
    """
    Compute confidence level (high/medium/low) from three dimension scores.

    Uses dimension_weights from ke_go_config to compute a weighted average,
    then maps to H/M/L using dimension_thresholds.

    Args:
        conn_score: Connection score (integer, 0-3)
        spec_score: Specificity score (integer, 0-3)
        ev_score: Evidence score (integer, 0-3)
        ke_go_config: KEGoAssessmentConfig instance with dimension_weights and dimension_thresholds

    Returns:
        str: 'high', 'medium', or 'low'
    """
    w = ke_go_config.dimension_weights
    weighted_avg = (
        conn_score * w['connection']
        + spec_score * w['specificity']
        + ev_score * w['evidence']
    )
    thresholds = ke_go_config.dimension_thresholds
    if weighted_avg >= thresholds['high']:
        return 'high'
    elif weighted_avg >= thresholds['medium']:
        return 'medium'
    else:
        return 'low'


def login_required(f):
    """Decorator to require login for protected routes"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    """
    Decorator to require admin privileges for protected routes

    Checks if the current user is in the admin whitelist defined in environment variables.
    Admin usernames should be comma-separated in ADMIN_USERS env var.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Authentication required"}), 401

        current_user = session.get("user", {}).get("username")
        admin_users = _get_admin_users()

        if current_user not in admin_users:
            logger.warning("User %s attempted to access admin route", current_user)
            return jsonify({"error": "Admin access required"}), 403

        return f(*args, **kwargs)

    return decorated_function


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


@admin_bp.route("/proposals")
@admin_required
@monitor_performance
def admin_proposals():
    """
    Admin dashboard for managing proposals

    Displays all proposals with filtering and management capabilities.
    Only accessible to users listed in ADMIN_USERS environment variable.

    Returns:
        Rendered template with proposal data
    """
    try:
        # Get filter from query parameters
        status_filter = request.args.get("status", "pending")
        if status_filter == "all":
            status_filter = None

        # Get all proposals
        proposals = proposal_model.get_all_proposals(status=status_filter)
        
        # Format timestamps for local timezone
        for proposal in proposals:
            if proposal.get('created_at'):
                try:
                    utc_dt = datetime.fromisoformat(proposal['created_at'].replace('Z', '+00:00'))
                    local_dt = utc_to_local(utc_dt)
                    proposal['created_at_formatted'] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, TypeError):
                    proposal['created_at_formatted'] = proposal['created_at']

            if proposal.get('approved_at'):
                try:
                    utc_dt = datetime.fromisoformat(proposal['approved_at'].replace('Z', '+00:00'))
                    local_dt = utc_to_local(utc_dt)
                    proposal['approved_at_formatted'] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, TypeError):
                    proposal['approved_at_formatted'] = proposal['approved_at']

            if proposal.get('rejected_at'):
                try:
                    utc_dt = datetime.fromisoformat(proposal['rejected_at'].replace('Z', '+00:00'))
                    local_dt = utc_to_local(utc_dt)
                    proposal['rejected_at_formatted'] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, TypeError):
                    proposal['rejected_at_formatted'] = proposal['rejected_at']

        # Add admin status to template context
        user_info = session.get("user", {})

        return render_template(
            "admin_proposals.html",
            proposals=proposals,
            status_filter=status_filter or "all",
            user_info=user_info,
        )

    except Exception as e:
        logger.error("Error loading admin proposals: %s", e)
        return render_template(
            "admin_proposals.html",
            proposals=[],
            error="Failed to load proposals",
            user_info=session.get("user", {}),
        )


@admin_bp.route("/proposals/<int:proposal_id>")
@admin_required
@monitor_performance
def admin_proposal_detail(proposal_id: int):
    """
    View detailed information about a specific proposal

    Args:
        proposal_id: ID of the proposal to view

    Returns:
        JSON data with proposal details
    """
    try:
        proposal = proposal_model.get_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "Proposal not found"}), 404

        # Format timestamps for local timezone
        if proposal.get('created_at'):
            try:
                utc_dt = datetime.fromisoformat(proposal['created_at'].replace('Z', '+00:00'))
                local_dt = utc_to_local(utc_dt)
                proposal['created_at_formatted'] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except (ValueError, TypeError):
                proposal['created_at_formatted'] = proposal['created_at']

        if proposal.get('approved_at'):
            try:
                utc_dt = datetime.fromisoformat(proposal['approved_at'].replace('Z', '+00:00'))
                local_dt = utc_to_local(utc_dt)
                proposal['approved_at_formatted'] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except (ValueError, TypeError):
                proposal['approved_at_formatted'] = proposal['approved_at']

        if proposal.get('rejected_at'):
            try:
                utc_dt = datetime.fromisoformat(proposal['rejected_at'].replace('Z', '+00:00'))
                local_dt = utc_to_local(utc_dt)
                proposal['rejected_at_formatted'] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except (ValueError, TypeError):
                proposal['rejected_at_formatted'] = proposal['rejected_at']

        return jsonify(proposal)

    except Exception as e:
        logger.error("Error getting proposal %s: %s", sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to load proposal"}), 500


@admin_bp.route("/proposals/<int:proposal_id>/approve", methods=["POST"])
@admin_required
@submission_rate_limit
def approve_proposal(proposal_id: int):
    """
    Approve a proposal and apply the changes to the mapping

    Args:
        proposal_id: ID of the proposal to approve

    Returns:
        JSON response indicating success/failure
    """
    try:
        # Extract only the required fields for validation (exclude CSRF token)
        admin_data = {"admin_notes": request.form.get("admin_notes", "")}

        # Debug logging
        logger.info("Admin approve request data: %s", sanitize_log(str(admin_data)))

        # Validate admin notes input
        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )

        if not is_valid:
            logger.warning("Invalid admin notes in approve: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        # Get sanitized admin notes
        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        # Validate admin username
        if not SecurityValidation.validate_username(admin_username):
            logger.error("Invalid admin username in approve: %s", admin_username)
            return jsonify({"error": "Authentication error"}), 401

        # Get proposal details
        proposal = proposal_model.get_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "Proposal not found"}), 404

        if proposal["status"] != "pending":
            return jsonify({"error": f"Proposal is already {proposal['status']}"}), 400

        # Phase 34 ASMT-02: read assessment-question answers off the
        # proposal row, thread through both the create_mapping (new-pair)
        # and update_mapping (revision) call sites. Existing proposals
        # submitted before this phase have NULL values here, which the
        # model layer interprets as assessment_version='v1'
        # (see _classify_assessment_version in src/core/models.py).
        proposed_relationship = proposal.get("proposed_relationship")
        proposed_basis = proposal.get("proposed_basis")
        proposed_specificity = proposal.get("proposed_specificity")
        proposed_coverage = proposal.get("proposed_coverage")

        # Apply the proposed changes
        success = True
        mapping_id = proposal["mapping_id"]

        if proposal["proposed_delete"]:
            # Delete the mapping
            success = mapping_model.delete_mapping(mapping_id, admin_username)
            action = "deleted"
        elif mapping_id is None:
            # New-pair proposal: create the mapping then write provenance
            approved_at = datetime.utcnow().isoformat()
            proposal_score = proposal.get("suggestion_score")
            # Phase C: stamp the new mapping with the current snapshot's
            # WikiPathways + AOP-Wiki release info from data/source_versions.json.
            wp_version_fields = _source_version_fields("wp")
            new_mapping_id = mapping_model.create_mapping(
                ke_id=proposal["ke_id"],
                ke_title=proposal["ke_title"],
                wp_id=proposal["wp_id"],
                wp_title=proposal["wp_title"],
                connection_type=proposal.get("new_pair_connection_type") or proposal.get("proposed_connection_type"),
                confidence_level=proposal.get("new_pair_confidence_level") or proposal.get("proposed_confidence"),
                created_by=proposal.get("provider_username") or admin_username,
                # Phase 34 ASMT-02: assessment answers carried from proposal.
                proposed_relationship=proposed_relationship,
                proposed_basis=proposed_basis,
                proposed_specificity=proposed_specificity,
                proposed_coverage=proposed_coverage,
                **wp_version_fields,
            )
            if new_mapping_id:
                success = mapping_model.update_mapping(
                    mapping_id=new_mapping_id,
                    approved_by_curator=admin_username,
                    approved_at_curator=approved_at,
                    suggestion_score=proposal_score,
                    proposed_by=proposal.get("provider_username"),
                    # Phase 34 ASMT-02: re-thread assessment so the
                    # update path's assessment_version classifier also
                    # sees the populated values (defense-in-depth — the
                    # create_mapping call above already wrote them).
                    proposed_relationship=proposed_relationship,
                    proposed_basis=proposed_basis,
                    proposed_specificity=proposed_specificity,
                    proposed_coverage=proposed_coverage,
                )
            else:
                success = False
            action = "created"
        else:
            # Existing mapping revision proposal: update the mapping with provenance
            approved_at = datetime.utcnow().isoformat()
            proposal_score = proposal.get("suggestion_score")   # REAL or None
            # Phase C: revision approvals refresh the version stamp too — they
            # represent a re-confirmation of the mapping against the current
            # snapshot, so the snapshot the curator was reviewing wins.
            wp_version_fields = _source_version_fields("wp")
            success = mapping_model.update_mapping(
                mapping_id=mapping_id,
                connection_type=proposal["proposed_connection_type"],
                confidence_level=proposal["proposed_confidence"],
                updated_by=admin_username,
                approved_by_curator=admin_username,
                approved_at_curator=approved_at,
                suggestion_score=proposal_score,       # carry score from proposal
                proposed_by=proposal.get("provider_username"),
                # Phase 34 ASMT-02: assessment answers carried from proposal.
                proposed_relationship=proposed_relationship,
                proposed_basis=proposed_basis,
                proposed_specificity=proposed_specificity,
                proposed_coverage=proposed_coverage,
                **wp_version_fields,
            )
            action = "updated"

        if success:
            # Update proposal status
            proposal_model.update_proposal_status(
                proposal_id=proposal_id,
                status="approved",
                admin_username=admin_username,
                admin_notes=admin_notes,
            )

            logger.info(
                "Proposal %s approved by %s, mapping %s", sanitize_log(proposal_id), sanitize_log(admin_username), action
            )
            return (
                jsonify(
                    {
                        "message": f"Proposal approved successfully. Mapping {action}.",
                        "action": action,
                    }
                ),
                200,
            )
        else:
            return jsonify({"error": f"Failed to {action.rstrip('d')} mapping"}), 500

    except Exception as e:
        logger.error("Error approving proposal %s: %s", sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to approve proposal"}), 500


@admin_bp.route("/proposals/<int:proposal_id>/reject", methods=["POST"])
@admin_required
@submission_rate_limit
def reject_proposal(proposal_id: int):
    """
    Reject a proposal with optional admin notes

    Args:
        proposal_id: ID of the proposal to reject

    Returns:
        JSON response indicating success/failure
    """
    try:
        # Extract only the required fields for validation (exclude CSRF token)
        admin_data = {"admin_notes": request.form.get("admin_notes", "")}

        # Debug logging
        logger.info("Admin reject request data: %s", sanitize_log(str(admin_data)))

        # Validate admin notes input
        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )

        if not is_valid:
            logger.warning("Invalid admin notes in reject: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        # Get sanitized admin notes
        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        # Validate admin username
        if not SecurityValidation.validate_username(admin_username):
            logger.error("Invalid admin username in reject: %s", admin_username)
            return jsonify({"error": "Authentication error"}), 401

        # Get proposal details
        proposal = proposal_model.get_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "Proposal not found"}), 404

        if proposal["status"] != "pending":
            return jsonify({"error": f"Proposal is already {proposal['status']}"}), 400

        # Update proposal status to rejected
        success = proposal_model.update_proposal_status(
            proposal_id=proposal_id,
            status="rejected",
            admin_username=admin_username,
            admin_notes=admin_notes or "No reason provided",
        )

        if success:
            logger.info("Proposal %s rejected by %s", sanitize_log(proposal_id), sanitize_log(admin_username))
            return jsonify({"message": "Proposal rejected successfully."}), 200
        else:
            return jsonify({"error": "Failed to reject proposal"}), 500

    except Exception as e:
        logger.error("Error rejecting proposal %s: %s", sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to reject proposal"}), 500


@admin_bp.route("/go-proposals")
@admin_required
@monitor_performance
def admin_go_proposals():
    """
    Admin dashboard for managing KE-GO proposals.

    Displays all GO proposals with filtering and management capabilities.
    """
    try:
        status_filter = request.args.get("status", "pending")
        if status_filter == "all":
            status_filter = None

        proposals = go_proposal_model.get_all_go_proposals(status=status_filter)

        # Format timestamps for local timezone
        for proposal in proposals:
            if proposal.get("created_at"):
                try:
                    utc_dt = datetime.fromisoformat(proposal["created_at"].replace("Z", "+00:00"))
                    local_dt = utc_to_local(utc_dt)
                    proposal["created_at_formatted"] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, TypeError):
                    proposal["created_at_formatted"] = proposal["created_at"]

        return render_template(
            "admin_go_proposals.html",
            proposals=proposals,
            status_filter=status_filter or "all",
        )

    except Exception as e:
        logger.error("Error loading GO proposals dashboard: %s", sanitize_log(str(e)))
        return render_template(
            "admin_go_proposals.html",
            proposals=[],
            status_filter="pending",
            error=str(e),
        )


@admin_bp.route("/go-proposals/<int:proposal_id>")
@admin_required
@monitor_performance
def admin_go_proposal_detail(proposal_id: int):
    """Return JSON details for a specific GO proposal (used by detail modal)."""
    try:
        proposal = go_proposal_model.get_go_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "GO proposal not found"}), 404

        if proposal.get("created_at"):
            try:
                utc_dt = datetime.fromisoformat(proposal["created_at"].replace("Z", "+00:00"))
                local_dt = utc_to_local(utc_dt)
                proposal["created_at_formatted"] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except (ValueError, TypeError):
                proposal["created_at_formatted"] = proposal["created_at"]

        if proposal.get("approved_at"):
            try:
                utc_dt = datetime.fromisoformat(proposal["approved_at"].replace("Z", "+00:00"))
                local_dt = utc_to_local(utc_dt)
                proposal["approved_at_formatted"] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except (ValueError, TypeError):
                proposal["approved_at_formatted"] = proposal["approved_at"]

        if proposal.get("rejected_at"):
            try:
                utc_dt = datetime.fromisoformat(proposal["rejected_at"].replace("Z", "+00:00"))
                local_dt = utc_to_local(utc_dt)
                proposal["rejected_at_formatted"] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except (ValueError, TypeError):
                proposal["rejected_at_formatted"] = proposal["rejected_at"]

        return jsonify(proposal)

    except Exception as e:
        logger.error("Error getting GO proposal %s: %s", sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to load GO proposal"}), 500


@admin_bp.route("/go-proposals/<int:proposal_id>/approve", methods=["POST"])
@admin_required
@submission_rate_limit
def approve_go_proposal(proposal_id: int):
    """
    Approve a KE-GO proposal and apply it to the live data.

    Handles three proposal kinds:
      * deletion  (proposed_delete): remove the existing mapping;
      * revision  (mapping_id set):  update the existing mapping's
                                     confidence / connection type;
      * new-pair  (mapping_id NULL): create a fresh mapping.
    Deletion and revision proposals arrive via /submit_go_proposal (issue
    #197); new-pair proposals via /submit_go_mapping.
    """
    try:
        admin_data = {"admin_notes": request.form.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in GO approve: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error("Invalid admin username in GO approve: %s", admin_username)
            return jsonify({"error": "Authentication error"}), 401

        proposal = go_proposal_model.get_go_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "GO proposal not found"}), 404

        if proposal["status"] != "pending":
            return jsonify({"error": f"GO proposal is already {proposal['status']}"}), 400

        # Issue #197: deletion / revision branches for change proposals raised
        # against an existing approved mapping (mapping_id set). New-pair
        # proposals (mapping_id NULL) fall through to the create path below.
        mapping_id = proposal.get("mapping_id")
        if proposal.get("proposed_delete"):
            if not mapping_id:
                return jsonify({"error": "Deletion proposal has no target mapping"}), 400
            if not go_mapping_model.delete_mapping(mapping_id, admin_username):
                return jsonify({"error": "Failed to delete GO mapping"}), 500
            go_proposal_model.update_go_proposal_status(
                proposal_id=proposal_id,
                status="approved",
                admin_username=admin_username,
                admin_notes=admin_notes,
            )
            logger.info(
                "GO proposal %s approved by %s, mapping %s deleted",
                sanitize_log(proposal_id), sanitize_log(admin_username), sanitize_log(mapping_id),
            )
            return jsonify({
                "message": "GO proposal approved successfully. Mapping deleted.",
                "action": "deleted",
            }), 200

        if mapping_id:
            approved_at = datetime.utcnow().isoformat()
            # NB: no updated_by — the ke_go_mappings schema has no such column
            # (unlike WP mappings); attribution rides on approved_by_curator.
            success = go_mapping_model.update_go_mapping(
                mapping_id=mapping_id,
                connection_type=proposal.get("proposed_connection_type"),
                confidence_level=proposal.get("proposed_confidence"),
                approved_by_curator=admin_username,
                approved_at_curator=approved_at,
                proposed_by=proposal.get("provider_username"),
            )
            if not success:
                return jsonify({"error": "Failed to update GO mapping"}), 500
            go_proposal_model.update_go_proposal_status(
                proposal_id=proposal_id,
                status="approved",
                admin_username=admin_username,
                admin_notes=admin_notes,
            )
            logger.info(
                "GO proposal %s approved by %s, mapping %s updated",
                sanitize_log(proposal_id), sanitize_log(admin_username), sanitize_log(mapping_id),
            )
            return jsonify({
                "message": "GO proposal approved successfully. Mapping updated.",
                "action": "updated",
            }), 200

        # New-pair proposal (mapping_id IS NULL)
        approved_at = datetime.utcnow().isoformat()
        proposal_score = proposal.get("suggestion_score")

        # Read dimension scores from form (optional integers)
        def _parse_int(val):
            try:
                return int(val) if val is not None else None
            except (ValueError, TypeError):
                return None

        connection_score = _parse_int(request.form.get("connection_score"))
        specificity_score = _parse_int(request.form.get("specificity_score"))
        evidence_score = _parse_int(request.form.get("evidence_score"))

        # If all three dimension scores are present, compute confidence server-side
        if connection_score is not None and specificity_score is not None and evidence_score is not None:
            try:
                services = current_app.service_container
                ke_go_config = services.scoring_config.ke_go_assessment
            except Exception:
                ke_go_config = None

            if ke_go_config is not None:
                confidence_level = _compute_confidence_from_dimensions(
                    connection_score, specificity_score, evidence_score, ke_go_config
                )
            else:
                confidence_level = (
                    proposal.get("new_pair_confidence_level")
                    or proposal.get("proposed_confidence")
                )
            assessment_version = "v2"
        else:
            # Legacy flow — no dimension scores provided
            confidence_level = (
                proposal.get("new_pair_confidence_level")
                or proposal.get("proposed_confidence")
            )
            connection_score = None
            specificity_score = None
            evidence_score = None
            assessment_version = "v1"

        # Phase C: stamp the new GO mapping with current GO + AOP-Wiki versions.
        go_version_fields = _source_version_fields("go")
        new_mapping_id = go_mapping_model.create_mapping(
            ke_id=proposal["ke_id"],
            ke_title=proposal["ke_title"],
            go_id=proposal["go_id"],
            go_name=proposal["go_name"],
            connection_type=proposal.get("new_pair_connection_type") or proposal.get("proposed_connection_type"),
            confidence_level=confidence_level,
            created_by=proposal.get("provider_username") or admin_username,
            connection_score=connection_score,
            specificity_score=specificity_score,
            evidence_score=evidence_score,
            assessment_version=assessment_version,
            go_namespace=proposal.get("go_namespace", "biological_process"),
            **go_version_fields,
        )

        if new_mapping_id:
            go_mapping_model.update_go_mapping(
                mapping_id=new_mapping_id,
                approved_by_curator=admin_username,
                approved_at_curator=approved_at,
                suggestion_score=proposal_score,
                proposed_by=proposal.get("provider_username"),
            )
            go_proposal_model.update_go_proposal_status(
                proposal_id=proposal_id,
                status="approved",
                admin_username=admin_username,
                admin_notes=admin_notes,
            )
            logger.info(
                "GO proposal %s approved by %s, mapping %s created",
                sanitize_log(proposal_id), sanitize_log(admin_username), sanitize_log(new_mapping_id),
            )
            return jsonify({
                "message": "GO proposal approved successfully. Mapping created.",
                "action": "created",
            }), 200
        else:
            return jsonify({"error": "Failed to create GO mapping (pair may already exist)"}), 500

    except Exception as e:
        logger.error("Error approving GO proposal %s: %s", sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to approve GO proposal"}), 500


@admin_bp.route("/go-proposals/<int:proposal_id>/reject", methods=["POST"])
@admin_required
@submission_rate_limit
def reject_go_proposal(proposal_id: int):
    """Reject a KE-GO proposal with optional admin notes."""
    try:
        admin_data = {"admin_notes": request.form.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in GO reject: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error("Invalid admin username in GO reject: %s", admin_username)
            return jsonify({"error": "Authentication error"}), 401

        proposal = go_proposal_model.get_go_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "GO proposal not found"}), 404

        if proposal["status"] != "pending":
            return jsonify({"error": f"GO proposal is already {proposal['status']}"}), 400

        success = go_proposal_model.update_go_proposal_status(
            proposal_id=proposal_id,
            status="rejected",
            admin_username=admin_username,
            admin_notes=admin_notes or "No reason provided",
        )

        if success:
            logger.info("GO proposal %s rejected by %s", sanitize_log(proposal_id), sanitize_log(admin_username))
            return jsonify({"message": "GO proposal rejected successfully."}), 200
        else:
            return jsonify({"error": "Failed to reject GO proposal"}), 500

    except Exception as e:
        logger.error("Error rejecting GO proposal %s: %s", sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to reject GO proposal"}), 500


@admin_bp.route("/reactome-proposals")
@admin_required
@monitor_performance
def admin_reactome_proposals():
    """Admin dashboard for managing KE-Reactome proposals."""
    try:
        status_filter = request.args.get("status", "pending")
        if status_filter == "all":
            status_filter = None

        proposals = reactome_proposal_model.get_all_proposals(status=status_filter)

        for proposal in proposals:
            if proposal.get("created_at"):
                try:
                    utc_dt = datetime.fromisoformat(proposal["created_at"].replace("Z", "+00:00"))
                    local_dt = utc_to_local(utc_dt)
                    proposal["created_at_formatted"] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, TypeError):
                    proposal["created_at_formatted"] = proposal["created_at"]

        return render_template(
            "admin_reactome_proposals.html",
            proposals=proposals,
            status_filter=status_filter or "all",
        )

    except Exception as e:
        logger.error("Error loading Reactome proposals dashboard: %s", sanitize_log(str(e)))
        return render_template(
            "admin_reactome_proposals.html",
            proposals=[],
            status_filter="pending",
            error=str(e),
        )


@admin_bp.route("/reactome-proposals/<int:proposal_id>")
@admin_required
@monitor_performance
def admin_reactome_proposal_detail(proposal_id: int):
    """Return JSON details for a specific Reactome proposal (used by detail modal)."""
    try:
        proposal = reactome_proposal_model.get_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "Reactome proposal not found"}), 404

        for ts_field in ("created_at", "approved_at", "rejected_at"):
            if proposal.get(ts_field):
                try:
                    utc_dt = datetime.fromisoformat(proposal[ts_field].replace("Z", "+00:00"))
                    local_dt = utc_to_local(utc_dt)
                    proposal[ts_field + "_formatted"] = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                except (ValueError, TypeError):
                    proposal[ts_field + "_formatted"] = proposal[ts_field]

        return jsonify(proposal)

    except Exception as e:
        logger.error("Error getting Reactome proposal %s: %s",
                     sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to load Reactome proposal"}), 500


@admin_bp.route("/reactome-proposals/<int:proposal_id>/approve", methods=["POST"])
@admin_required
@submission_rate_limit
def approve_reactome_proposal(proposal_id: int):
    """
    Approve a Reactome proposal and apply it to the live data.

    Handles deletion (proposed_delete) and confidence-revision proposals
    raised against an existing mapping (mapping_id set, issue #197), plus the
    original new-pair path (mapping_id NULL) where confidence is straight
    pass-through from proposal -> mapping (D-02).
    """
    try:
        admin_data = {"admin_notes": request.form.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in Reactome approve: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error("Invalid admin username in Reactome approve: %s",
                         sanitize_log(str(admin_username)))
            return jsonify({"error": "Authentication error"}), 401

        proposal = reactome_proposal_model.get_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "Reactome proposal not found"}), 404

        if proposal["status"] != "pending":
            return jsonify({"error": f"Reactome proposal is already {proposal['status']}"}), 400

        approved_at = datetime.utcnow().isoformat()

        # Issue #197: deletion / confidence-revision branches for change
        # proposals against an existing mapping (mapping_id set). New-pair
        # proposals (mapping_id NULL) fall through to the create path below.
        mapping_id = proposal.get("mapping_id")
        if proposal.get("proposed_delete"):
            if not mapping_id:
                return jsonify({"error": "Deletion proposal has no target mapping"}), 400
            if not reactome_mapping_model.delete_mapping(mapping_id):
                return jsonify({"error": "Failed to delete Reactome mapping"}), 500
            reactome_proposal_model.update_proposal_status(
                proposal_id=proposal_id,
                status="approved",
                admin_username=admin_username,
                admin_notes=admin_notes,
            )
            logger.info(
                "Reactome proposal %s approved by %s, mapping %s deleted",
                sanitize_log(proposal_id), sanitize_log(admin_username), sanitize_log(mapping_id),
            )
            return jsonify({
                "message": "Reactome proposal approved successfully. Mapping deleted.",
                "action": "deleted",
            }), 200

        if mapping_id:
            # Reactome confidence is locked at proposal creation (D-02) and
            # Reactome has no connection type, so a non-deletion change against
            # an existing mapping has nothing to apply. /submit_reactome_proposal
            # only creates deletion proposals, so this is a defensive guard.
            return jsonify({
                "error": "Reactome change proposals support deletion only",
            }), 400

        # Phase 25 review H-1: write the mapping in one INSERT with every
        # carry-field populated up front (eliminates the create_mapping +
        # update_reactome_mapping two-step that could leave NULL provenance
        # on partial failure). Phase 34 (ASMT-10): create_approved_mapping
        # now loads all carry-fields from the proposal row internally via
        # REACTOME_PROPOSAL_CARRY_FIELDS — pass only the approval context.
        # Phase C: source-version fields are stamped from the deployed
        # manifest (NOT from the proposal), so they ride alongside the
        # approval context kwargs.
        reactome_version_fields = _source_version_fields("reactome")
        new_mapping_id = reactome_mapping_model.create_approved_mapping(
            proposal_id=proposal_id,
            approved_by_curator=admin_username,
            approved_at_curator=approved_at,
            **reactome_version_fields,
        )

        if not new_mapping_id:
            return jsonify({"error": "Failed to create Reactome mapping (pair may already exist)"}), 500

        if not reactome_proposal_model.update_proposal_status(
            proposal_id=proposal_id,
            status="approved",
            admin_username=admin_username,
            admin_notes=admin_notes,
        ):
            # Roll the mapping back so the proposal can be re-approved
            # later. Otherwise the proposal stays "pending" and a retry
            # would hit the UNIQUE constraint on (ke_id, reactome_id).
            logger.error(
                "Reactome proposal %s status flip failed after mapping %s "
                "created; rolling back mapping",
                sanitize_log(proposal_id), sanitize_log(new_mapping_id),
            )
            reactome_mapping_model.delete_mapping(new_mapping_id)
            return jsonify({
                "error": "Failed to update proposal status; mapping rolled back",
            }), 500

        logger.info(
            "Reactome proposal %s approved by %s, mapping %s created",
            sanitize_log(proposal_id), sanitize_log(admin_username), sanitize_log(new_mapping_id),
        )
        return jsonify({
            "message": "Reactome proposal approved successfully. Mapping created.",
            "action": "created",
        }), 200

    except Exception as e:
        logger.error("Error approving Reactome proposal %s: %s",
                     sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to approve Reactome proposal"}), 500


@admin_bp.route("/reactome-proposals/<int:proposal_id>/reject", methods=["POST"])
@admin_required
@submission_rate_limit
def reject_reactome_proposal(proposal_id: int):
    """Reject a Reactome proposal with optional admin notes."""
    try:
        admin_data = {"admin_notes": request.form.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in Reactome reject: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error("Invalid admin username in Reactome reject: %s",
                         sanitize_log(str(admin_username)))
            return jsonify({"error": "Authentication error"}), 401

        proposal = reactome_proposal_model.get_proposal_by_id(proposal_id)
        if not proposal:
            return jsonify({"error": "Reactome proposal not found"}), 404

        if proposal["status"] != "pending":
            return jsonify({"error": f"Reactome proposal is already {proposal['status']}"}), 400

        success = reactome_proposal_model.update_proposal_status(
            proposal_id=proposal_id,
            status="rejected",
            admin_username=admin_username,
            admin_notes=admin_notes or "No reason provided",
        )

        if success:
            logger.info("Reactome proposal %s rejected by %s",
                        sanitize_log(proposal_id), sanitize_log(admin_username))
            return jsonify({"message": "Reactome proposal rejected successfully."}), 200
        else:
            return jsonify({"error": "Failed to reject Reactome proposal"}), 500

    except Exception as e:
        logger.error("Error rejecting Reactome proposal %s: %s",
                     sanitize_log(proposal_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to reject Reactome proposal"}), 500


# ---------------------------------------------------------------------------
# Bulk-approve routes (Phase 38 ADMIN-01/06)
# ---------------------------------------------------------------------------


@admin_bp.route("/proposals/bulk-approve", methods=["POST"])
@admin_required
@submission_rate_limit
def bulk_approve_proposals():
    """
    Approve a batch of WP new-pair proposals in a single transaction.

    Accepts a JSON body: {"ids": [int, ...], "admin_notes": "optional string"}.
    Only new-pair proposals (mapping_id IS NULL, not proposed_delete) are
    handled here; revision/delete proposals go through the single-approve
    route.

    Returns: {"approved": [mapping_uuid_str, ...], "failed": [{"id": int, "reason": str}, ...]}

    Whole-batch rollback on any transaction error (H-1 invariant).
    """
    try:
        if not request.is_json:
            return jsonify({"error": "JSON body required"}), 400

        admin_data = {"admin_notes": request.json.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in WP bulk-approve: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error(
                "Invalid admin username in WP bulk-approve: %s",
                sanitize_log(str(admin_username)),
            )
            return jsonify({"error": "Authentication error"}), 401

        # Coerce and validate the ID list
        raw_ids = request.json.get("ids", [])
        failed = []
        int_ids = []
        for pid in raw_ids:
            if not isinstance(pid, int):
                failed.append({"id": pid, "reason": "invalid id"})
            else:
                int_ids.append(pid)

        # PRE-FLIGHT: read-only checks; never enter the transaction for invalid IDs
        valid_proposals = []
        for pid in int_ids:
            p = proposal_model.get_proposal_by_id(pid)
            if not p:
                failed.append({"id": pid, "reason": "not found"})
            elif p["status"] != "pending":
                failed.append({"id": pid, "reason": f"already {p['status']}"})
            elif p.get("proposed_delete") or p.get("mapping_id") is not None:
                # Bulk path only handles new-pair proposals
                failed.append({"id": pid, "reason": "not a new-pair proposal"})
            else:
                valid_proposals.append((pid, p))

        if not valid_proposals:
            return jsonify({"approved": [], "failed": failed}), 200

        # SINGLE TRANSACTION
        wp_version_fields = _source_version_fields("wp")
        conn = mapping_model.db.get_connection()
        approved_uuids = []
        try:
            for pid, proposal in valid_proposals:
                mapping_uuid = mapping_model._approve_on_conn(
                    conn, proposal, admin_username, **wp_version_fields
                )
                proposal_model._update_status_on_conn(
                    conn, pid, "approved", admin_username, admin_notes
                )
                approved_uuids.append(mapping_uuid)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("WP bulk-approve transaction failed: %s", exc)
            failed.extend(
                {"id": pid, "reason": "transaction failed"}
                for pid, _ in valid_proposals
            )
            return jsonify({"approved": [], "failed": failed}), 500
        finally:
            conn.close()

        # AUDIT (ADMIN-06)
        logger.info(
            "AUDIT bulk-approve wp: admin=%s approved=%s",
            sanitize_log(admin_username), approved_uuids,
        )

        return jsonify({"approved": approved_uuids, "failed": failed}), 200

    except Exception as e:
        logger.error("Error in WP bulk-approve: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to bulk-approve WP proposals"}), 500


@admin_bp.route("/go-proposals/bulk-approve", methods=["POST"])
@admin_required
@submission_rate_limit
def bulk_approve_go_proposals():
    """
    Approve a batch of GO new-pair proposals in a single transaction.

    Accepts a JSON body: {"ids": [int, ...], "admin_notes": "optional string"}.
    Uses stored confidence fallback (no admin re-score widget), assessment_version="v1".

    Returns: {"approved": [mapping_uuid_str, ...], "failed": [{"id": int, "reason": str}, ...]}

    Whole-batch rollback on any transaction error (H-1 invariant).
    """
    try:
        if not request.is_json:
            return jsonify({"error": "JSON body required"}), 400

        admin_data = {"admin_notes": request.json.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in GO bulk-approve: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error(
                "Invalid admin username in GO bulk-approve: %s",
                sanitize_log(str(admin_username)),
            )
            return jsonify({"error": "Authentication error"}), 401

        # Coerce and validate the ID list
        raw_ids = request.json.get("ids", [])
        failed = []
        int_ids = []
        for pid in raw_ids:
            if not isinstance(pid, int):
                failed.append({"id": pid, "reason": "invalid id"})
            else:
                int_ids.append(pid)

        # PRE-FLIGHT: read-only checks
        valid_proposals = []
        for pid in int_ids:
            p = go_proposal_model.get_go_proposal_by_id(pid)
            if not p:
                failed.append({"id": pid, "reason": "not found"})
            elif p["status"] != "pending":
                failed.append({"id": pid, "reason": f"already {p['status']}"})
            else:
                valid_proposals.append((pid, p))

        if not valid_proposals:
            return jsonify({"approved": [], "failed": failed}), 200

        # SINGLE TRANSACTION
        go_version_fields = _source_version_fields("go")
        conn = go_mapping_model.db.get_connection()
        approved_uuids = []
        try:
            for pid, proposal in valid_proposals:
                mapping_uuid = go_mapping_model._approve_on_conn(
                    conn, proposal, admin_username, **go_version_fields
                )
                go_proposal_model._update_status_on_conn(
                    conn, pid, "approved", admin_username, admin_notes
                )
                approved_uuids.append(mapping_uuid)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("GO bulk-approve transaction failed: %s", exc)
            failed.extend(
                {"id": pid, "reason": "transaction failed"}
                for pid, _ in valid_proposals
            )
            return jsonify({"approved": [], "failed": failed}), 500
        finally:
            conn.close()

        # AUDIT (ADMIN-06)
        logger.info(
            "AUDIT bulk-approve go: admin=%s approved=%s",
            sanitize_log(admin_username), approved_uuids,
        )

        return jsonify({"approved": approved_uuids, "failed": failed}), 200

    except Exception as e:
        logger.error("Error in GO bulk-approve: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to bulk-approve GO proposals"}), 500


@admin_bp.route("/reactome-proposals/bulk-approve", methods=["POST"])
@admin_required
@submission_rate_limit
def bulk_approve_reactome_proposals():
    """
    Approve a batch of Reactome new-pair proposals in a single transaction.

    Accepts a JSON body: {"ids": [int, ...], "admin_notes": "optional string"}.
    Confidence is straight pass-through from proposal -> mapping (D-02).

    Returns: {"approved": [mapping_uuid_str, ...], "failed": [{"id": int, "reason": str}, ...]}

    Whole-batch rollback on any transaction error (H-1 invariant).
    """
    try:
        if not request.is_json:
            return jsonify({"error": "JSON body required"}), 400

        admin_data = {"admin_notes": request.json.get("admin_notes", "")}

        is_valid, validated_data, errors = validate_request_data(
            AdminNotesSchema, admin_data
        )
        if not is_valid:
            logger.warning("Invalid admin notes in Reactome bulk-approve: %s", errors)
            return jsonify({"error": "Invalid input data", "details": errors}), 400

        admin_notes = SecurityValidation.sanitize_string(
            validated_data["admin_notes"], max_length=1000
        )
        admin_username = session.get("user", {}).get("username")

        if not SecurityValidation.validate_username(admin_username):
            logger.error(
                "Invalid admin username in Reactome bulk-approve: %s",
                sanitize_log(str(admin_username)),
            )
            return jsonify({"error": "Authentication error"}), 401

        # Coerce and validate the ID list
        raw_ids = request.json.get("ids", [])
        failed = []
        int_ids = []
        for pid in raw_ids:
            if not isinstance(pid, int):
                failed.append({"id": pid, "reason": "invalid id"})
            else:
                int_ids.append(pid)

        # PRE-FLIGHT: read-only checks
        valid_proposals = []
        for pid in int_ids:
            p = reactome_proposal_model.get_proposal_by_id(pid)
            if not p:
                failed.append({"id": pid, "reason": "not found"})
            elif p["status"] != "pending":
                failed.append({"id": pid, "reason": f"already {p['status']}"})
            else:
                valid_proposals.append((pid, p))

        if not valid_proposals:
            return jsonify({"approved": [], "failed": failed}), 200

        # SINGLE TRANSACTION
        reactome_version_fields = _source_version_fields("reactome")
        conn = reactome_mapping_model.db.get_connection()
        approved_uuids = []
        try:
            for pid, _proposal in valid_proposals:
                mapping_uuid = reactome_mapping_model._create_approved_on_conn(
                    conn, pid, admin_username, **reactome_version_fields
                )
                reactome_proposal_model._update_status_on_conn(
                    conn, pid, "approved", admin_username, admin_notes
                )
                approved_uuids.append(mapping_uuid)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("Reactome bulk-approve transaction failed: %s", exc)
            failed.extend(
                {"id": pid, "reason": "transaction failed"}
                for pid, _ in valid_proposals
            )
            return jsonify({"approved": [], "failed": failed}), 500
        finally:
            conn.close()

        # AUDIT (ADMIN-06)
        logger.info(
            "AUDIT bulk-approve reactome: admin=%s approved=%s",
            sanitize_log(admin_username), approved_uuids,
        )

        return jsonify({"approved": approved_uuids, "failed": failed}), 200

    except Exception as e:
        logger.error("Error in Reactome bulk-approve: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to bulk-approve Reactome proposals"}), 500


@admin_bp.route("/guest-codes")
@admin_required
@monitor_performance
def admin_guest_codes():
    """Admin page for managing guest access codes"""
    try:
        codes = guest_code_model.get_all_codes() if guest_code_model else []
        return render_template(
            "admin_guest_codes.html",
            codes=codes,
            user_info=session.get("user", {}),
        )
    except Exception as e:
        logger.error("Error loading guest codes: %s", e)
        return render_template(
            "admin_guest_codes.html",
            codes=[],
            error="Failed to load guest codes",
            user_info=session.get("user", {}),
        )


@admin_bp.route("/guest-codes/create", methods=["POST"])
@admin_required
@submission_rate_limit
def create_guest_code():
    """Create a new guest access code"""
    try:
        label = request.form.get("label", "").strip()
        expiry_hours = request.form.get("expiry_hours", "24")
        max_uses = request.form.get("max_uses", "1")

        # Validate label format
        if not label or not re.match(r"^[a-zA-Z0-9_-]{3,50}$", label):
            return jsonify({
                "error": "Label must be 3-50 characters, alphanumeric with hyphens and underscores only."
            }), 400

        # Validate expiry hours
        try:
            expiry_hours = int(expiry_hours)
            if expiry_hours < 1 or expiry_hours > 720:
                return jsonify({"error": "Expiry must be between 1 and 720 hours."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid expiry hours."}), 400

        # Validate max uses
        try:
            max_uses = int(max_uses)
            if max_uses < 1 or max_uses > 100:
                return jsonify({"error": "Max uses must be between 1 and 100."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid max uses."}), 400

        admin_username = session.get("user", {}).get("username")
        expires_at = (datetime.utcnow() + timedelta(hours=expiry_hours)).isoformat()

        code = guest_code_model.create_code(
            label=label,
            created_by=admin_username,
            expires_at=expires_at,
            max_uses=max_uses,
        )

        if code:
            logger.info(
                "Guest code created by %s for label=%s", sanitize_log(admin_username), sanitize_log(label)
            )
            return jsonify({"message": "Guest code created.", "code": code}), 201
        else:
            return jsonify({"error": "Failed to create guest code."}), 500

    except Exception as e:
        logger.error("Error creating guest code: %s", sanitize_log(str(e)))
        return jsonify({"error": "Failed to create guest code."}), 500


@admin_bp.route("/guest-codes/<int:code_id>/revoke", methods=["POST"])
@admin_required
@submission_rate_limit
def revoke_guest_code(code_id: int):
    """Revoke a guest access code"""
    try:
        admin_username = session.get("user", {}).get("username")
        success = guest_code_model.revoke_code(code_id, admin_username)

        if success:
            logger.info("Guest code %d revoked by %s", sanitize_log(code_id), sanitize_log(admin_username))
            return jsonify({"message": "Guest code revoked."}), 200
        else:
            return jsonify({"error": "Failed to revoke guest code."}), 500

    except Exception as e:
        logger.error("Error revoking guest code %d: %s", sanitize_log(code_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to revoke guest code."}), 500


@admin_bp.route("/exports", methods=["GET"])
@admin_required
def admin_exports():
    """Admin-facing dashboard for export regeneration + Zenodo publishing.

    Surfaces current live mapping counts side-by-side with the last-recorded
    Zenodo deposit (DOI, version, per-resource counts), and exposes two
    actions: rebuild the on-disk export cache, and mint a new Zenodo
    deposit. Both buttons POST to existing routes; this page is the only
    place that ties them together for an admin user.
    """
    import json as json_lib
    import os
    from pathlib import Path

    from src.exporters.zenodo_assembly import counts as count_rows
    from src.exporters.zenodo_uploader import resolve_zenodo_token

    meta_path = Path("data/zenodo_meta.json")
    zenodo_meta = {}
    if meta_path.exists():
        try:
            zenodo_meta = json_lib.loads(meta_path.read_text())
        except Exception as e:
            logger.warning("Could not parse %s: %s", meta_path, e)

    wp = mapping_model.get_all_mappings() if mapping_model else []
    go = go_mapping_model.get_all_mappings() if go_mapping_model else []
    rx = reactome_mapping_model.get_all_mappings() if reactome_mapping_model else []
    live_counts = {
        "wp": count_rows(wp),
        "go": count_rows(go),
        "reactome": count_rows(rx),
    }

    return render_template(
        "admin_exports.html",
        zenodo_meta=zenodo_meta,
        live_counts=live_counts,
        zenodo_token_configured=bool(resolve_zenodo_token("ZENODO_API_TOKEN")),
    )


@admin_bp.route("/exports/regenerate", methods=["POST"])
@admin_required
def regenerate_exports():
    """Clear and rebuild all cached export files (GMT + Turtle)."""
    import shutil
    from pathlib import Path
    from src.exporters.gmt_exporter import generate_ke_wp_gmt, generate_ke_go_gmt
    from src.exporters.rdf_exporter import generate_ke_wp_turtle, generate_ke_go_turtle

    cache_dir = Path("static/exports")
    try:
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        cache_dir.mkdir(parents=True)

        wp_mappings = mapping_model.get_all_mappings() if mapping_model else []
        go_mappings = go_mapping_model.get_all_mappings() if go_mapping_model else []

        import datetime
        today = datetime.date.today().isoformat()

        files_written = []
        for conf_label, conf_filter in [("All", None), ("High", "high"), ("Medium", "medium"), ("Low", "low")]:
            # KE-WP GMT
            gmt_wp = generate_ke_wp_gmt(wp_mappings, cache_model=cache_model_ref, min_confidence=conf_filter)
            if gmt_wp:
                p = cache_dir / f"KE-WP_{today}_{conf_label}.gmt"
                p.write_text(gmt_wp, encoding="utf-8")
                files_written.append(p.name)
            # KE-GO GMT
            gmt_go = generate_ke_go_gmt(go_mappings, min_confidence=conf_filter)
            if gmt_go:
                p = cache_dir / f"KE-GO_{today}_{conf_label}.gmt"
                p.write_text(gmt_go, encoding="utf-8")
                files_written.append(p.name)

        # Turtle exports (no confidence filtering for RDF — include all)
        ttl_wp = generate_ke_wp_turtle(wp_mappings)
        if ttl_wp:
            p = cache_dir / "ke-wp-mappings.ttl"
            p.write_text(ttl_wp, encoding="utf-8")
            files_written.append(p.name)

        ttl_go = generate_ke_go_turtle(go_mappings)
        if ttl_go:
            p = cache_dir / "ke-go-mappings.ttl"
            p.write_text(ttl_go, encoding="utf-8")
            files_written.append(p.name)

        logger.info("Export cache rebuilt: %s", files_written)
        return jsonify({"status": "ok", "files": files_written, "message": f"Rebuilt {len(files_written)} export file(s). Note: KE-WP GMT generation requires WikiPathways SPARQL — may be slow on first run."})
    except Exception:
        logger.exception("Export regeneration failed")
        return jsonify({"status": "error", "message": "Export regeneration failed"}), 500


@admin_bp.route("/exports/publish-zenodo", methods=["POST"])
@admin_required
def publish_zenodo():
    """Mint (or version-bump) a Zenodo deposit in the v3 per-resource ZIP shape.

    Assembles three per-resource ZIPs (KE-WikiPathways.zip, KE-GO.zip,
    KE-Reactome.zip) directly from the model layer using the shared
    `zenodo_assembly` helpers — same artefacts the CLI script produces.
    On successful publish, persists deposit metadata to
    `data/zenodo_meta.json` (with EACCES fallback to /tmp/).
    """
    import datetime
    import json as json_lib
    import os
    from pathlib import Path

    from src.exporters.zenodo_assembly import (
        assemble_deposit_files,
        build_metadata,
        counts as count_rows,
    )
    from src.exporters.zenodo_uploader import (
        zenodo_publish,
        persist_meta_with_fallback,
        META_FALLBACK_PATH,
    )

    if not os.environ.get("ZENODO_API_TOKEN"):
        return jsonify({"status": "error", "message": "ZENODO_API_TOKEN not configured"}), 503

    meta_path = Path("data/zenodo_meta.json")
    try:
        zenodo_meta = json_lib.loads(meta_path.read_text()) if meta_path.exists() else {}
    except Exception:
        zenodo_meta = {}
    existing_id = zenodo_meta.get("deposition_id")

    # Pull current approved mappings straight from the model layer — same
    # source the CLI script uses. The flat static/exports/ cache is no
    # longer the input; it's a separate UI download surface.
    wp = mapping_model.get_all_mappings() if mapping_model else []
    go = go_mapping_model.get_all_mappings() if go_mapping_model else []
    rx = reactome_mapping_model.get_all_mappings() if reactome_mapping_model else []
    if not (wp or go or rx):
        return jsonify({
            "status": "error",
            "message": "No approved mappings found across WP / GO / Reactome — nothing to publish.",
        }), 400

    # Optional source-versions manifest for the per-resource sidecar and
    # the README snapshot block. Missing manifest is fine — deposit still
    # publishes, just without the snapshot pin.
    source_versions = {}
    sv_path = Path("data/source_versions.json")
    if sv_path.exists():
        try:
            source_versions = json_lib.loads(sv_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not parse %s — deposit will omit snapshot block: %s", sv_path, e)

    today = datetime.date.today().isoformat()
    upload_files = assemble_deposit_files(
        today, wp, go, rx,
        source_versions=source_versions,
        gmt_kwargs_wp={"cache_model": cache_model_ref} if cache_model_ref else None,
    )
    metadata = build_metadata(today, source_versions=source_versions)

    try:
        result = zenodo_publish(upload_files, metadata, existing_deposition_id=existing_id)
        zenodo_meta.update({
            "deposition_id": result["deposition_id"],
            "doi": result["doi"],
            "concept_doi": result.get("concept_doi", zenodo_meta.get("concept_doi", result["doi"])),
            "published_at": today,
            "version": metadata["version"],
            "counts": {"wp": count_rows(wp), "go": count_rows(go), "reactome": count_rows(rx)},
        })
        written_path = persist_meta_with_fallback(meta_path, zenodo_meta)
        logger.info("Zenodo publish complete: DOI=%s", result["doi"])
        response = {
            "status": "ok",
            "doi": result["doi"],
            "concept_doi": result.get("concept_doi"),
            "deposition_id": result["deposition_id"],
            "counts": zenodo_meta["counts"],
        }
        if written_path == META_FALLBACK_PATH:
            response["meta_path_fallback"] = str(META_FALLBACK_PATH)
            response["meta_path_message"] = (
                "Zenodo publish succeeded but data/zenodo_meta.json was not "
                "writable (gluster uid mismatch — issue #158). Pending payload "
                f"saved to {META_FALLBACK_PATH}; copy it back into place."
            )
        return jsonify(response)
    except EnvironmentError:
        logger.exception("Zenodo publish blocked by environment (token / config missing)")
        return jsonify({
            "status": "error",
            "message": "Zenodo publish blocked — check server logs for the missing token or config",
        }), 503
    except Exception:
        logger.exception("Zenodo publish failed")
        return jsonify({
            "status": "error",
            "message": "Zenodo publish failed — see server logs for details",
        }), 500


@admin_bp.route("/ke-descriptions")
@admin_required
@monitor_performance
def admin_ke_descriptions():
    """Admin page for KE description coverage audit and per-KE override toggles."""
    try:
        # Load KE metadata from service container
        services = current_app.service_container
        ke_metadata = services.ke_metadata or []

        # Compute coverage stats
        total_kes = len(ke_metadata)
        kes_with_desc = sum(
            1 for ke in ke_metadata
            if ke.get("KEdescription") and ke["KEdescription"].strip()
        )
        coverage_pct = round((kes_with_desc / total_kes * 100), 1) if total_kes else 0

        # Load per-KE overrides
        overrides = ke_override_model.get_all_overrides() if ke_override_model else {}

        # Read global toggle from config
        config = services.scoring_config
        global_toggle = getattr(
            getattr(
                getattr(config, 'pathway_suggestion', None),
                'embedding_based_matching', None
            ),
            'use_ke_description', True
        ) if config else True

        return render_template(
            "admin_ke_descriptions.html",
            ke_metadata=ke_metadata,
            total_kes=total_kes,
            kes_with_desc=kes_with_desc,
            coverage_pct=coverage_pct,
            overrides=overrides,
            global_toggle=global_toggle,
        )
    except Exception as e:
        logger.error("Error loading KE descriptions page: %s", e)
        return render_template(
            "admin_ke_descriptions.html",
            ke_metadata=[],
            total_kes=0,
            kes_with_desc=0,
            coverage_pct=0,
            overrides={},
            global_toggle=True,
            error="Failed to load KE description data",
        )


@admin_bp.route("/ke-descriptions/<path:ke_id>/toggle", methods=["POST"])
@admin_required
def toggle_ke_description(ke_id: str):
    """Toggle description usage for a specific KE."""
    try:
        if not ke_override_model:
            return jsonify({"error": "Override model not available"}), 500

        data = request.get_json(silent=True) or {}
        disabled = bool(data.get("disabled", False))

        # Validate ke_id exists in metadata
        services = current_app.service_container
        ke_metadata = services.ke_metadata or []
        valid_ids = {ke["KElabel"] for ke in ke_metadata}
        if ke_id not in valid_ids:
            return jsonify({"error": f"KE ID '{ke_id}' not found"}), 404

        admin_username = session.get("user", {}).get("username", "unknown")
        success = ke_override_model.toggle_override(ke_id, disabled, admin_username)

        if success:
            return jsonify({
                "success": True,
                "ke_id": ke_id,
                "description_disabled": disabled,
            })
        else:
            return jsonify({"error": "Failed to save override"}), 500

    except Exception as e:
        logger.error("Error toggling KE description for %s: %s", sanitize_log(ke_id), sanitize_log(str(e)))
        return jsonify({"error": "Failed to toggle description"}), 500
