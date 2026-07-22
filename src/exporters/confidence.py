"""Confidence-tier filtering for the GMT and RDF exports.

Two distinct operations live here, deliberately named apart because conflating
them is exactly what issue #206 was:

``filter_by_min_confidence``
    A **threshold**. ``medium`` means medium *and above*. This is what the
    public ``?min_confidence=`` query parameter promises, and what a "minimum
    confidence" control in a consuming tool means.

``filter_by_exact_confidence``
    A **partition**. ``medium`` means medium only. This is what the Zenodo
    deposit's ``_High`` / ``_Medium`` / ``_Low`` ZIP layout is built on, and
    what its README documents as mutually exclusive buckets.

Until #206 the exporters implemented the partition and exposed it under the
threshold's name, so ``?min_confidence=medium`` silently dropped every
high-confidence mapping — the opposite of what the parameter promises, and a
particular problem for anyone who published a "Medium" GMT believing it
contained the best-evidenced rows.

Ranks and the unknown-value behaviour mirror the downstream molAOP Analyser's
``helpers.confidence_rank`` / ``filter_records_by_confidence`` so the two sides
of the API contract agree. Note the vocabularies still differ: the Analyser uses
``"all"`` as its no-filter sentinel, which the Builder's route whitelist does not
accept — see ``_VALID_MIN_CONFIDENCE`` in ``src/blueprints/main.py``.
"""
import logging

logger = logging.getLogger(__name__)

_RANKS = {"low": 1, "medium": 2, "high": 3}


def confidence_rank(value):
    """Map a confidence label to its ordinal rank.

    Returns 3 for High, 2 for Medium, 1 for Low, and ``None`` when the value is
    missing, blank or unrecognised. ``None`` means "unknown", which callers
    treat as a filter no-op rather than an exclusion.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in ("nan", "none"):
        return None
    return _RANKS.get(text)


def filter_by_min_confidence(mappings, min_confidence):
    """Keep mappings at or above ``min_confidence``.

    ``high`` yields high only, ``medium`` yields medium and high, and ``low``
    is equivalent to no filtering at all — the honest reading of the name.

    A falsy or unrecognised threshold returns every mapping. Rows whose own
    confidence is missing or unrecognised are **kept**: an export should never
    silently empty because a resource lacks the field.
    """
    threshold = confidence_rank(min_confidence)
    if threshold is None:
        return list(mappings)

    kept = [
        r for r in mappings
        # `or threshold` makes an unknown row-level confidence a no-op include.
        if (confidence_rank(r.get("confidence_level")) or threshold) >= threshold
    ]
    logger.debug(
        "filter_by_min_confidence: kept %d of %d mappings at min_confidence=%s",
        len(kept), len(mappings), min_confidence,
    )
    return kept


def filter_by_exact_confidence(mappings, confidence):
    """Keep only mappings at exactly this tier.

    Used to build the mutually exclusive tier files in the Zenodo deposit and
    the admin export bundle. A falsy or unrecognised tier returns every mapping.

    Unlike the threshold filter, a row whose own confidence is unknown is
    **excluded**: it demonstrably does not belong to the named bucket, and the
    deposit README states each ``_<Level>.gmt`` contains that level's mappings.
    """
    wanted = confidence_rank(confidence)
    if wanted is None:
        return list(mappings)

    return [
        r for r in mappings
        if confidence_rank(r.get("confidence_level")) == wanted
    ]
