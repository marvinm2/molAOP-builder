"""
Input validation schemas using Marshmallow
"""
import re

from marshmallow import Schema, ValidationError, fields, validate, validates


_GO_NAMESPACE_MAP = {
    "BP": "biological_process",
    "MF": "molecular_function",
    "biological_process": "biological_process",
    "molecular_function": "molecular_function",
}


# Phase 34 ASMT-02: canonical KE-WP assessment option keys. Mirrored from
# static/js/main.js:1378-1391. Plan 04 cites these in KE-MAPPING-API-REFERENCE.md.
# These four whitelists power the `step1..step4` form fields the mapper UI
# already submits; the /submit handler renames them to the DB column names
# (proposed_relationship/basis/specificity/coverage) when forwarding to the
# model layer.
KE_WP_RELATIONSHIP_OPTIONS = ("causative", "responsive", "bidirectional", "unclear")
KE_WP_BASIS_OPTIONS = ("known", "likely", "possible", "uncertain")
KE_WP_SPECIFICITY_OPTIONS = ("specific", "includes", "loose")
KE_WP_COVERAGE_OPTIONS = ("complete", "keysteps", "minor")

# The stored `connection_type` is a different, narrower vocabulary than step1's
# (issue #264). `bidirectional` and `unclear` are assessment answers, not
# connection types, and MappingSchema.connection_type rejects both — so a
# relationship answer must be translated before it reaches the column.
KE_WP_CONNECTION_TYPES = ("causative", "responsive", "other", "undefined")
RELATIONSHIP_TO_CONNECTION_TYPE = {
    "causative": "causative",
    "responsive": "responsive",
    "bidirectional": "other",
    "unclear": "undefined",
}


def connection_type_for_relationship(relationship):
    """Translate a step1 relationship answer to the stored connection_type.

    Mirrors ``mapConnectionTypeForServer`` in static/js/main.js. Lives here so
    every writer shares it: the four model-layer dual-write sites reached this
    column independently and none of them applied the map, which is how 17% of
    the corpus came to hold out-of-vocabulary values (#264).

    Returns None for a None relationship so callers can fall back to an
    explicitly supplied connection_type. An unrecognised answer maps to
    ``undefined`` rather than passing through, matching the JS default — the
    column's vocabulary is closed and a writer must not widen it.
    """
    if relationship is None:
        return None
    return RELATIONSHIP_TO_CONNECTION_TYPE.get(relationship, "undefined")


# KE-GO uses its own connection vocabulary (not the KE-WP one above) and the shared
# confidence tiers. Defined once here because two writers validate against them: the
# submit schema below, and the admin approve route, where a reviewer may refine both
# before the live mapping is written.
GO_CONNECTION_TYPES = ("describes", "involves", "related", "context")
GO_CONFIDENCE_LEVELS = ("low", "medium", "high")
# The three KE-GO assessment dimensions are scored High/Medium/Low = 3/2/1, in
# the submitter form (static/js/main.js) and the reviewer panel
# (static/js/admin_proposals.js) alike. Named here so the revision schema can
# validate against the range a curator can actually express (#245); the column
# comments say 0-3, but nothing has ever offered a 0.
GO_DIMENSION_SCORES = (1, 2, 3)


class GoNamespaceField(fields.Field):
    """Marshmallow field that normalizes GO namespace short codes and full names."""

    def _deserialize(self, value, attr, data, **kwargs):
        if value not in _GO_NAMESPACE_MAP:
            raise ValidationError(
                f"Invalid go_namespace '{value}'. "
                "Accepted values: BP, MF, biological_process, molecular_function."
            )
        return _GO_NAMESPACE_MAP[value]


class KEWPAssessmentAnswersMixin(Schema):
    """The four KE-WP assessment answers, as optional fields.

    Inherited by both the new-pair submit schema and the revision-proposal
    schema (#245) so the two cannot drift: a correction is the same judgement
    as a creation and must be validated against the same whitelists.

    Optional on both paths, for different reasons. On creation, absence means a
    legacy v1 submission from a non-UI form-poster. On revision, a deletion
    proposal asserts nothing about confidence and so answers nothing; the
    handler requires the four only when a revision actually proposes a change.
    """

    step1 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_RELATIONSHIP_OPTIONS),
            error="Invalid step1 option (relationship)",
        ),
    )
    step2 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_BASIS_OPTIONS),
            error="Invalid step2 option (basis)",
        ),
    )
    step3 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_SPECIFICITY_OPTIONS),
            error="Invalid step3 option (specificity)",
        ),
    )
    step4 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_COVERAGE_OPTIONS),
            error="Invalid step4 option (coverage)",
        ),
    )


class MappingSchema(KEWPAssessmentAnswersMixin):
    """Schema for KE-WP mapping submissions.

    Phase 34 ASMT-02 option-key whitelists (canonical, mirrored from
    static/js/main.js:1378-1391):

    - ``connection_type``: ``causative``, ``responsive``, ``other``, ``undefined``
    - ``confidence_level``: ``low``, ``medium``, ``high``
    - ``step1`` (relationship): ``causative``, ``responsive``, ``bidirectional``, ``unclear``
    - ``step2`` (basis): ``known``, ``likely``, ``possible``, ``uncertain``
    - ``step3`` (specificity): ``specific``, ``includes``, ``loose``
    - ``step4`` (coverage): ``complete``, ``keysteps``, ``minor``

    The four ``step*`` fields are optional — a submission without any
    ``step*`` value remains backward-compatible and produces a v1 (legacy)
    mapping. Out-of-whitelist values raise a Marshmallow ``ValidationError``
    which the /submit handler surfaces as HTTP 400.

    The /submit handler renames ``step1..step4`` to the DB column conventions
    (``proposed_relationship/basis/specificity/coverage``) when forwarding to
    ``ProposalModel.create_new_pair_proposal``. Plan 04 cites the four
    whitelists verbatim in ``KE-MAPPING-API-REFERENCE.md``.
    """

    ke_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(r"^KE\s+\d+$", error="KE ID must be in format 'KE number'"),
        ],
    )
    ke_title = fields.Str(required=True, validate=validate.Length(min=1, max=500))
    wp_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(r"^WP\d+$", error="WP ID must be in format 'WPnumber'"),
        ],
    )
    wp_title = fields.Str(required=True, validate=validate.Length(min=1, max=500))
    connection_type = fields.Str(
        required=True,
        validate=validate.OneOf(
            list(KE_WP_CONNECTION_TYPES),
            error="Invalid connection type",
        ),
    )
    confidence_level = fields.Str(
        required=True,
        validate=validate.OneOf(
            ["low", "medium", "high"], error="Invalid confidence level"
        ),
    )
    # step1..step4 come from KEWPAssessmentAnswersMixin — the same four fields
    # the revision path validates against, so creating and correcting a mapping
    # cannot drift apart (#245).


class ProposalSchema(KEWPAssessmentAnswersMixin):
    """Schema for KE-WP revision/deletion proposals.

    #245: a revision carries the same four assessment answers as a creation
    (``step1..step4``, renamed to ``proposed_relationship/basis/specificity/
    coverage`` by the handler) and the tier is *derived* from them server-side.
    There is no direct confidence control — a tier asserted without stated
    grounds is not reviewable, which is the whole point of the assessment.

    A deletion proposal makes no assertion about confidence and so carries no
    answers; the handler requires the assessment only for a non-deletion.
    """

    entry = fields.Str(required=True)  # JSON string of entry data
    userName = fields.Str(
        required=True,
        validate=[
            validate.Length(min=1, max=100),
            validate.Regexp(
                r"^[a-zA-Z0-9\s\-\.\'_]+$",
                error="Name can only contain letters, numbers, spaces, hyphens, dots, apostrophes, and underscores",
            ),
        ],
    )
    userEmail = fields.Email(required=True)
    userAffiliation = fields.Str(
        required=True, validate=validate.Length(min=1, max=200)
    )
    deleteEntry = fields.Str(missing="", validate=validate.OneOf(["", "on"]))

    @validates("entry")
    def validate_entry_json(self, value):
        """Validate that entry is valid JSON with required fields"""
        import json

        try:
            # Handle double-serialized JSON
            if value.startswith('"') and value.endswith('"'):
                value = json.loads(value)  # First deserialization
            entry_data = json.loads(
                value.replace("'", '"')
            )  # Second deserialization with quote fix

            if not isinstance(entry_data, dict):
                raise ValidationError("Entry must be a JSON object")

            # Each entry must carry a KE id and a pathway id. The v1 API
            # serialises the WikiPathways id as `pathway_id`; older code paths
            # use `wp_id` / `WPID`. Accept any of the three.
            field_aliases = {
                "ke_id": ("ke_id", "KEID"),
                "wp_id": ("wp_id", "WPID", "pathway_id"),
            }
            missing_fields = []

            for field, aliases in field_aliases.items():
                if not any(entry_data.get(alias) for alias in aliases):
                    missing_fields.append(field)

            if missing_fields:
                raise ValidationError(
                    f"Entry missing required fields: {missing_fields}"
                )

        except json.JSONDecodeError as e:
            raise ValidationError(f"Entry must be valid JSON: {str(e)}")


class _MappingChangeProposalSchema(Schema):
    """Shared base for KE-GO / KE-Reactome change/deletion proposals.

    Mirrors ProposalSchema's contact + change fields but validates the entry
    against a resource-specific id field (go_id / reactome_id) rather than the
    WikiPathways pathway_id. Subclasses set ``_id_field`` and ``_id_aliases``.
    """

    _id_field = None
    _id_aliases = ()

    entry = fields.Str(required=True)  # JSON string of entry data
    userName = fields.Str(
        required=True,
        validate=[
            validate.Length(min=1, max=100),
            validate.Regexp(
                r"^[a-zA-Z0-9\s\-\.\'_]+$",
                error="Name can only contain letters, numbers, spaces, hyphens, dots, apostrophes, and underscores",
            ),
        ],
    )
    userEmail = fields.Email(required=True)
    userAffiliation = fields.Str(
        required=True, validate=validate.Length(min=1, max=200)
    )
    deleteEntry = fields.Str(missing="", validate=validate.OneOf(["", "on"]))

    @validates("entry")
    def validate_entry_json(self, value):
        """Validate that entry is valid JSON carrying ke_id + the resource id."""
        import json

        try:
            if value.startswith('"') and value.endswith('"'):
                value = json.loads(value)
            entry_data = json.loads(value.replace("'", '"'))

            if not isinstance(entry_data, dict):
                raise ValidationError("Entry must be a JSON object")

            field_aliases = {
                "ke_id": ("ke_id", "KEID"),
                self._id_field: self._id_aliases,
            }
            missing_fields = [
                field
                for field, aliases in field_aliases.items()
                if not any(entry_data.get(alias) for alias in aliases)
            ]
            if missing_fields:
                raise ValidationError(
                    f"Entry missing required fields: {missing_fields}"
                )
        except json.JSONDecodeError as e:
            raise ValidationError(f"Entry must be valid JSON: {str(e)}")


class GoProposalChangeSchema(_MappingChangeProposalSchema):
    """Schema for KE-GO change/deletion proposals (#197, reworked for #245).

    A GO revision asks what creating a GO mapping asks — a connection type plus
    the three dimension scores — and the tier is derived from them, matching how
    the KE-WP path derives its tier from four answers. GO's instrument is its
    own: three weighted dimensions and a four-term connection vocabulary, not
    the WP four-question form. Aligning the two *interfaces* does not mean
    merging the two *instruments*.

    This replaces a ``changeType`` that validated against the **WikiPathways**
    vocabulary (``causative``/``responsive``/``undefined``) — none of which is a
    valid GO connection type — so a GO revision could not propose a valid
    connection type at all, and could propose three invalid ones.
    """

    _id_field = "go_id"
    _id_aliases = ("go_id", "GOID")

    changeType = fields.Str(
        missing="",
        validate=validate.OneOf([""] + list(GO_CONNECTION_TYPES)),
    )
    connection_score = fields.Int(
        required=False, allow_none=True,
        validate=validate.OneOf(GO_DIMENSION_SCORES),
    )
    specificity_score = fields.Int(
        required=False, allow_none=True,
        validate=validate.OneOf(GO_DIMENSION_SCORES),
    )
    evidence_score = fields.Int(
        required=False, allow_none=True,
        validate=validate.OneOf(GO_DIMENSION_SCORES),
    )


class ReactomeProposalChangeSchema(
    _MappingChangeProposalSchema, KEWPAssessmentAnswersMixin
):
    """Schema for KE-Reactome revision/deletion proposals (#197, #245).

    Reactome shares KE-WP's four-question instrument — ``ke_reactome_mappings``
    already carries the same four ``proposed_*`` columns, written at creation —
    so a revision asks the same four questions and the tier is derived from
    them, exactly as on the KE-WP path.

    **This reverses the deletion-only half of D-02.** That decision locked the
    tier at proposal creation, which left a Reactome mapping's confidence
    permanently uncorrectable: the only remedy for a wrong tier was to delete
    the mapping and re-create it, losing its uuid and provenance. What D-02
    usefully guarantees — that approval never consumes admin-supplied dimension
    scores, and that ``ke_reactome_mappings`` grows no ``connection_score`` /
    ``specificity_score`` / ``evidence_score`` / ``connection_type`` columns —
    is unchanged and still pinned by
    ``tests/test_reactome_admin.py::test_approve_no_dimension_score_columns_used``.

    Reactome genuinely has no connection type (#248): the relationship answer
    lives in ``proposed_relationship`` like the other three, and there is no
    derived column for it to populate.
    """

    _id_field = "reactome_id"
    _id_aliases = ("reactome_id", "reactomeId")


class GoMappingSchema(Schema):
    """Schema for KE-GO mapping submissions"""

    ke_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(r"^KE\s+\d+$", error="KE ID must be in format 'KE number'"),
        ],
    )
    ke_title = fields.Str(required=True, validate=validate.Length(min=1, max=500))
    go_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=10, max=20),
            validate.Regexp(
                r"^GO:\d{7}$", error="GO ID must be in format 'GO:0000000'"
            ),
        ],
    )
    go_name = fields.Str(required=True, validate=validate.Length(min=1, max=500))
    connection_type = fields.Str(
        required=True,
        validate=validate.OneOf(
            GO_CONNECTION_TYPES,
            error="Invalid connection type",
        ),
    )
    confidence_level = fields.Str(
        required=True,
        validate=validate.OneOf(
            GO_CONFIDENCE_LEVELS, error="Invalid confidence level"
        ),
    )
    go_namespace = GoNamespaceField(load_default="biological_process")


class GoCheckEntrySchema(Schema):
    """Schema for checking existing GO entries"""

    ke_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(r"^KE\s+\d+$", error="KE ID must be in format 'KE number'"),
        ],
    )
    go_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=10, max=20),
            validate.Regexp(
                r"^GO:\d{7}$", error="GO ID must be in format 'GO:0000000'"
            ),
        ],
    )


class ReactomeMappingSchema(Schema):
    """Schema for KE-Reactome mapping submissions (Phase 25).

    Phase 37 ASMT-04: four assessment-question answer fields (step1-4) and
    an optional connection_type mirror MappingSchema (WP) for sibling parity.
    All four step fields are optional for backward-compat with v1 (legacy)
    proposals that predate the assessment UI. Out-of-whitelist values raise a
    Marshmallow ValidationError surfaced as HTTP 400 by the submit handler.

    The submit handler renames step1..step4 to the DB column conventions
    (proposed_relationship/basis/specificity/coverage) and derives
    connection_type from step1 if absent.
    """

    ke_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(
                r"^KE\s+\d+$", error="KE ID must be in format 'KE number'"
            ),
        ],
    )
    ke_title = fields.Str(required=True, validate=validate.Length(min=1, max=500))
    reactome_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=8, max=30),
            validate.Regexp(
                r"^R-HSA-\d+$",
                error="Reactome ID must be in format 'R-HSA-NNNN'",
            ),
        ],
    )
    pathway_name = fields.Str(
        required=True, validate=validate.Length(min=1, max=500)
    )
    species = fields.Str(
        load_default="Homo sapiens", validate=validate.Length(max=100)
    )
    confidence_level = fields.Str(
        required=True,
        validate=validate.OneOf(
            ["low", "medium", "high"], error="Invalid confidence level"
        ),
    )
    # Phase 37 ASMT-04: four assessment-question answers — optional for
    # back-compat; same KE_WP_*_OPTIONS constants as MappingSchema (single
    # source of truth, canonical option-key whitelists).
    step1 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_RELATIONSHIP_OPTIONS),
            error="Invalid step1 option (relationship)",
        ),
    )
    step2 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_BASIS_OPTIONS),
            error="Invalid step2 option (basis)",
        ),
    )
    step3 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_SPECIFICITY_OPTIONS),
            error="Invalid step3 option (specificity)",
        ),
    )
    step4 = fields.Str(
        required=False,
        allow_none=True,
        validate=validate.OneOf(
            list(KE_WP_COVERAGE_OPTIONS),
            error="Invalid step4 option (coverage)",
        ),
    )
    # No connection_type (#248). It was declared here and validated, then
    # discarded — `ke_reactome_mappings` has no such column and the proposal
    # model no such parameter, so the field only ever created the impression
    # that Reactome had a connection type. Its relationship answer is `step1`,
    # stored as `proposed_relationship` like the other three.


class ReactomeCheckEntrySchema(Schema):
    """Schema for checking existing KE-Reactome entries (Phase 25)."""

    ke_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(
                r"^KE\s+\d+$", error="KE ID must be in format 'KE number'"
            ),
        ],
    )
    reactome_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=8, max=30),
            validate.Regexp(
                r"^R-HSA-\d+$",
                error="Reactome ID must be in format 'R-HSA-NNNN'",
            ),
        ],
    )


class AdminNotesSchema(Schema):
    """Schema for admin notes in proposal management"""

    admin_notes = fields.Str(missing="", validate=validate.Length(max=1000))


class CheckEntrySchema(Schema):
    """Schema for checking existing entries"""

    ke_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(r"^KE\s+\d+$", error="KE ID must be in format 'KE number'"),
        ],
    )
    wp_id = fields.Str(
        required=True,
        validate=[
            validate.Length(min=3, max=50),
            validate.Regexp(r"^WP\d+$", error="WP ID must be in format 'WPnumber'"),
        ],
    )


class SecurityValidation:
    """Additional security validation utilities"""

    @staticmethod
    def sanitize_string(value: str, max_length: int = 500) -> str:
        """Sanitize string input by removing potentially harmful characters"""
        if not isinstance(value, str):
            return str(value)

        # Remove null bytes and control characters except common whitespace
        sanitized = "".join(
            char for char in value if ord(char) >= 32 or char in "\t\n\r"
        )

        # Limit length
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length]

        return sanitized.strip()

    @staticmethod
    def validate_username(username: str) -> bool:
        """Validate OAuth provider-prefixed or guest username format"""
        if not isinstance(username, str):
            return False

        # Guest usernames: guest-<label> where label is alphanumeric with hyphens/underscores
        if username.startswith("guest-"):
            guest_label = username[6:]
            return bool(re.match(r"^[a-zA-Z0-9_-]{3,50}$", guest_label))

        # Strip OAuth provider prefix (e.g. "github:", "orcid:", "ls:", "surf:")
        if ":" in username:
            username = username.split(":", 1)[1]

        # Username rules: alphanumeric, hyphens, max 39 chars, no consecutive hyphens
        # Also allow ORCID format (0000-0001-2345-6789)
        if re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", username):
            return True
        pattern = r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$"
        return bool(re.match(pattern, username))

    @staticmethod
    def validate_email_domain(email: str) -> bool:
        """Basic email domain validation (additional to Marshmallow's email validation)"""
        if not isinstance(email, str) or "@" not in email:
            return False

        domain = email.split("@")[1]
        # Basic domain validation - at least one dot and valid characters
        return bool(
            re.match(
                r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$",
                domain,
            )
        )


def validate_request_data(schema_class, data):
    """
    Validate request data using the provided schema

    Args:
        schema_class: Marshmallow schema class to use for validation
        data: Data to validate (typically request.form or request.json)

    Returns:
        tuple: (is_valid: bool, validated_data: dict, errors: dict)
    """
    schema = schema_class()

    try:
        validated_data = schema.load(data)
        return True, validated_data, {}
    except ValidationError as e:
        return False, {}, e.messages
