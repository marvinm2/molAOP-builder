"""Server-side scoring of the four-question KE-pathway assessment.

The browser scores the assessment as the curator fills it in
(``evaluateConfidence`` in ``static/js/main.js``), but the tier it computes
cannot be trusted on submission: the biological-level bonus is read from a
client instance variable that several code paths clear, so a form restored
after a session expiry scores one tier low and the downgraded value is what
reaches the database (issue #237).

This module is the authority. It reads the same ``ke_pathway_assessment``
block of ``scoring_config.yaml`` that the browser is served over
``/api/scoring-config``, and it takes the biological level from the server's
own KE metadata rather than from the request.

Parity with the JS is deliberate and load-bearing — the curator must not see
one tier in the form and get another in the database. The rules that are easy
to get wrong when reading the two side by side:

* ``step1`` (relationship) is **not** scored. It only supplies the connection
  type.
* An unanswered question contributes ``0`` rather than aborting the scoring,
  matching the ``|| 0`` in the JS.
* The biological-level test is a case-insensitive **substring** match against
  each qualifying level, not equality.
* Thresholds are inclusive (``>=``), and ``low`` is the implicit else.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Returned when the tier cannot be computed, so callers can distinguish
# "scored to low" from "not scorable" and fall back rather than downgrade.
UNSCORABLE = None


def score_ke_pathway_assessment(
    basis: Optional[str],
    specificity: Optional[str],
    coverage: Optional[str],
    biolevel: Optional[str],
    config: Any,
) -> float:
    """Return the raw assessment score before it is bucketed into a tier.

    Args:
        basis: the ``step2`` answer (evidence quality).
        specificity: the ``step3`` answer (pathway specificity).
        coverage: the ``step4`` answer (KE coverage).
        biolevel: the Key Event's biological level, from server-side KE
            metadata. Falsy means no bonus is applied.
        config: a ``KEPathwayAssessmentConfig``.
    """
    score = 0.0
    score += config.evidence_quality.get(basis, 0)
    score += config.pathway_specificity.get(specificity, 0)
    score += config.ke_coverage.get(coverage, 0)

    if biolevel:
        qualifying = config.biological_level.get("qualifying_levels", [])
        needle = biolevel.lower()
        if any(level in needle for level in qualifying):
            score += config.biological_level.get("bonus", 0)

    return score


def tier_for_score(score: float, config: Any) -> str:
    """Bucket a raw assessment score into ``high`` / ``medium`` / ``low``."""
    thresholds = config.confidence_thresholds
    if score >= thresholds["high"]:
        return "high"
    if score >= thresholds["medium"]:
        return "medium"
    return "low"


def compute_ke_pathway_confidence(
    basis: Optional[str],
    specificity: Optional[str],
    coverage: Optional[str],
    biolevel: Optional[str],
    config: Any,
) -> str:
    """Score the assessment and return the confidence tier it implies."""
    return tier_for_score(
        score_ke_pathway_assessment(basis, specificity, coverage, biolevel, config),
        config,
    )


def resolve_biolevel(
    ke_id: str, ke_meta_index: Optional[Dict[str, dict]]
) -> Optional[str]:
    """Look up a Key Event's biological level in the server's KE metadata.

    Returns ``None`` when the index is unavailable or does not carry the Key
    Event — which is the signal to leave the client's tier alone rather than
    score without the bonus. A missing KE would otherwise be scored as though
    it were above the qualifying levels, reproducing #237 server-side.
    """
    if not ke_meta_index:
        return None
    entry = ke_meta_index.get(ke_id)
    if not entry:
        return None
    return entry.get("biolevel") or None


def recompute_confidence_level(
    ke_id: str,
    basis: Optional[str],
    specificity: Optional[str],
    coverage: Optional[str],
    submitted_level: str,
    ke_meta_index: Optional[Dict[str, dict]],
    config: Any,
) -> str:
    """Return the tier to store for a submission.

    The server's own scoring wins whenever it can be computed. It cannot be
    when the assessment is incomplete (a legacy ``v1`` submission answers none
    of the four questions) or when the Key Event is absent from the metadata
    snapshot — which happens both in tests and in production, since the
    snapshot lags AOP-Wiki (#239). In those cases the submitted tier stands.
    """
    if not (basis and specificity and coverage):
        return submitted_level

    biolevel = resolve_biolevel(ke_id, ke_meta_index)
    if biolevel is None:
        logger.warning(
            "No biological level for %s; keeping the submitted confidence %r "
            "rather than scoring without the bonus",
            ke_id,
            submitted_level,
        )
        return submitted_level

    computed = compute_ke_pathway_confidence(
        basis, specificity, coverage, biolevel, config
    )
    if computed != submitted_level:
        logger.info(
            "Recomputed confidence for %s: submitted %r, stored %r "
            "(basis=%r specificity=%r coverage=%r biolevel=%r)",
            ke_id,
            submitted_level,
            computed,
            basis,
            specificity,
            coverage,
            biolevel,
        )
    return computed


# ---------------------------------------------------------------------------
# KE-GO: a different instrument, scored the same way twice
# ---------------------------------------------------------------------------
#
# KE-GO does not use the four questions above. It scores three dimensions —
# connection, specificity, evidence — each High/Medium/Low = 3/2/1, and takes a
# weighted average against its own thresholds, with no biological-level bonus.
# The two instruments are not interchangeable and their thresholds differ
# (5.0/2.5 additive here, 2.5/1.5 averaged there).
#
# This lives beside the KE-WP scorer because it has the same job and the same
# hazard: the reviewer panel and the submitter form each carry a copy of the
# arithmetic in JavaScript, reconciled through /api/go-scoring-config, and the
# server now has writers in two blueprints. One definition, three readers.


def compute_go_confidence(
    connection_score: Optional[int],
    specificity_score: Optional[int],
    evidence_score: Optional[int],
    config: Any,
) -> Optional[str]:
    """Return the KE-GO tier for three dimension scores, or None if incomplete.

    Returning None rather than defaulting to ``low`` keeps "not scored" separable
    from "scored badly" — a distinction the WP scorer also makes, and for the
    same reason: a missing answer must never be silently read as a weak one.
    """
    if None in (connection_score, specificity_score, evidence_score):
        return UNSCORABLE

    weights = config.dimension_weights
    weighted_avg = (
        connection_score * weights["connection"]
        + specificity_score * weights["specificity"]
        + evidence_score * weights["evidence"]
    )
    thresholds = config.dimension_thresholds
    if weighted_avg >= thresholds["high"]:
        return "high"
    if weighted_avg >= thresholds["medium"]:
        return "medium"
    return "low"
