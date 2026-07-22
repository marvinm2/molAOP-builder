"""
Tests for confidence filtering on the GMT/RDF exports (#206).

The defect: every /exports/gmt/* and /exports/rdf/* generator filtered with
`confidence_level.lower() == min_confidence`, an exact-tier match exposed under
a parameter named "minimum". `?min_confidence=medium` therefore returned
medium-confidence mappings *only*, silently dropping the high-confidence ones —
the opposite of what the name promises. Anyone who published a "Medium" GMT is
holding a file with the best-evidenced rows missing.

The fix keeps both behaviours but names them apart: `min_confidence` is a
threshold (the public query parameter), `confidence` is a partition (the Zenodo
deposit's mutually exclusive tier files).
"""
import pytest

from src.exporters.confidence import (
    confidence_rank,
    filter_by_exact_confidence,
    filter_by_min_confidence,
)


def _rows(*levels):
    return [{"uuid": f"u{i}", "confidence_level": lv} for i, lv in enumerate(levels)]


def _levels(rows):
    return [r["confidence_level"] for r in rows]


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("high", 3), ("High", 3), ("  HIGH  ", 3),
    ("medium", 2), ("Medium", 2),
    ("low", 1), ("Low", 1),
])
def test_confidence_rank_orders_tiers(value, expected):
    assert confidence_rank(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "nan", "none", "unrecognised"])
def test_confidence_rank_unknown_is_none(value):
    """Unknown means unknown — callers treat it as a filter no-op, not a zero."""
    assert confidence_rank(value) is None


def test_ranks_are_strictly_ordered():
    assert confidence_rank("high") > confidence_rank("medium") > confidence_rank("low")


# ---------------------------------------------------------------------------
# Threshold semantics — the actual bug
# ---------------------------------------------------------------------------

def test_medium_threshold_includes_high():
    """The #206 regression. Under the old exact-match filter this returned
    ['Medium'] and dropped the high-confidence row entirely."""
    kept = filter_by_min_confidence(_rows("High", "Medium", "Low"), "medium")
    assert sorted(_levels(kept)) == ["High", "Medium"]


def test_high_threshold_returns_high_only():
    """The one case where threshold and exact tier coincide — which is why the
    six 'High confidence' links in templates/downloads.html never exposed this."""
    kept = filter_by_min_confidence(_rows("High", "Medium", "Low"), "high")
    assert _levels(kept) == ["High"]


def test_low_threshold_is_equivalent_to_unfiltered():
    rows = _rows("High", "Medium", "Low")
    assert filter_by_min_confidence(rows, "low") == filter_by_min_confidence(rows, None)
    assert len(filter_by_min_confidence(rows, "low")) == 3


def test_tiers_are_cumulative_and_nested():
    """all >= low >= medium >= high, each a subset of the one below it."""
    rows = _rows("High", "Medium", "Low", "High", "Medium")
    unfiltered = {r["uuid"] for r in filter_by_min_confidence(rows, None)}
    low = {r["uuid"] for r in filter_by_min_confidence(rows, "low")}
    medium = {r["uuid"] for r in filter_by_min_confidence(rows, "medium")}
    high = {r["uuid"] for r in filter_by_min_confidence(rows, "high")}
    assert high < medium < unfiltered
    assert low == unfiltered


@pytest.mark.parametrize("threshold", [None, "", "bogus"])
def test_unrecognised_threshold_does_not_filter(threshold):
    rows = _rows("High", "Medium", "Low")
    assert len(filter_by_min_confidence(rows, threshold)) == 3


def test_rows_with_unknown_confidence_are_kept():
    """An export must never silently empty because a resource lacks the field."""
    rows = [
        {"uuid": "a", "confidence_level": "Low"},
        {"uuid": "b", "confidence_level": None},
        {"uuid": "c"},
        {"uuid": "d", "confidence_level": ""},
    ]
    kept = filter_by_min_confidence(rows, "high")
    assert {r["uuid"] for r in kept} == {"b", "c", "d"}


def test_null_confidence_does_not_raise():
    """The nine replaced call sites used r.get("confidence_level", "").lower(),
    which raises AttributeError on a present-but-null key."""
    filter_by_min_confidence([{"confidence_level": None}], "medium")
    filter_by_exact_confidence([{"confidence_level": None}], "medium")


def test_input_is_not_mutated():
    rows = _rows("High", "Medium")
    filter_by_min_confidence(rows, "high")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Partition semantics — what the Zenodo deposit is built on
# ---------------------------------------------------------------------------

def test_exact_confidence_selects_one_tier():
    kept = filter_by_exact_confidence(_rows("High", "Medium", "Low"), "medium")
    assert _levels(kept) == ["Medium"]


def test_exact_tiers_partition_the_input():
    """The deposit README describes _High/_Medium/_Low as disjoint buckets whose
    union is _All. Hold that property."""
    rows = _rows("High", "Medium", "Low", "High")
    buckets = [
        {r["uuid"] for r in filter_by_exact_confidence(rows, tier)}
        for tier in ("high", "medium", "low")
    ]
    assert set().union(*buckets) == {r["uuid"] for r in rows}
    for i, a in enumerate(buckets):
        for b in buckets[i + 1:]:
            assert not (a & b), "tier files must not overlap"


def test_exact_confidence_excludes_unknown():
    """Unlike the threshold, a row of unknown tier does not belong in a bucket
    labelled with a specific tier."""
    rows = [{"uuid": "a", "confidence_level": "High"}, {"uuid": "b", "confidence_level": None}]
    assert _levels(filter_by_exact_confidence(rows, "high")) == ["High"]


# ---------------------------------------------------------------------------
# The two must stay distinguishable at the generator boundary
# ---------------------------------------------------------------------------

def test_generators_reject_both_filters_at_once():
    from src.exporters.gmt_exporter import _apply_confidence

    with pytest.raises(ValueError):
        _apply_confidence(_rows("High"), min_confidence="high", confidence="high")


def test_generator_threshold_and_partition_differ():
    """The whole point of #206: at the generator boundary these must not be the
    same operation."""
    from src.exporters.gmt_exporter import _apply_confidence

    rows = _rows("High", "Medium", "Low")
    threshold = _apply_confidence(rows, min_confidence="medium")
    partition = _apply_confidence(rows, confidence="medium")
    assert sorted(_levels(threshold)) == ["High", "Medium"]
    assert _levels(partition) == ["Medium"]
