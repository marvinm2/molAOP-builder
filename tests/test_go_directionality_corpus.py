"""
Tests for the KE->GO term-picking fixes (#193):

1. is_directional_go_label — the runtime guard flags signed GO labels but keeps
   neutral "regulation of X", bare "X activation", and MF "... activity" terms.
2. GoSuggestionService._search_metadata — exact-label matches rank first and
   signed/directional terms are dropped from search results.
3. precompute_go_hierarchy — the corpus-build helper + generic whitelist are
   self-consistent (whitelist is non-directional, valid GO IDs).
"""
import re

import pytest

from src.utils.text import is_directional_go_label
from src.suggestions.go import GoSuggestionService


# ---------------------------------------------------------------------------
# 1. Directional-label guard
# ---------------------------------------------------------------------------

DIRECTIONAL = [
    "positive regulation of apoptotic process",
    "negative regulation of cell growth",
    "activation of protein kinase activity",
    "inhibition of MAPK cascade",
    "induction of apoptosis",
    "positive regulation of gene expression via chromosomal CpG island demethylation",
    "up-regulation of transcription",
    "downregulation of signaling",
]

NEUTRAL = [
    "cell death",
    "apoptotic process",
    "inflammatory response",
    "regulation of apoptotic process",   # neutral "regulation of X" — kept
    "T cell activation",                 # bare "X activation" — kept
    "complement activation",
    "oxidoreductase activity",           # MF "... activity" — kept
    "response to oxidative stress",
    "reactive oxygen species metabolic process",
]


@pytest.mark.parametrize("name", DIRECTIONAL)
def test_directional_labels_flagged(name):
    assert is_directional_go_label(name) is True


@pytest.mark.parametrize("name", NEUTRAL)
def test_neutral_labels_not_flagged(name):
    assert is_directional_go_label(name) is False


def test_empty_label_not_flagged():
    assert is_directional_go_label("") is False
    assert is_directional_go_label(None) is False


# ---------------------------------------------------------------------------
# 2. GO search exact-match ordering + directional exclusion
# ---------------------------------------------------------------------------

def _bare_service():
    """A GoSuggestionService without running __init__ (no corpus files loaded)."""
    return object.__new__(GoSuggestionService)


def test_search_exact_label_ranks_first_over_fuzzy_near_miss():
    svc = _bare_service()
    metadata = {
        "GO:0008219": {"name": "cell death", "definition": "the process by which a cell ceases to function"},
        "GO:0016049": {"name": "cell growth", "definition": "increase in cell size"},
        "GO:0012501": {"name": "programmed cell death", "definition": "a form of cell death"},
    }
    query_clean = svc._clean_text("cell death")
    results = svc._search_metadata(metadata, query_clean, 0.2, "BP")
    assert results, "expected matches"
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    # Exact label "cell death" must be first, above "cell growth".
    assert results[0]["go_id"] == "GO:0008219"
    names = [r["go_name"] for r in results]
    assert names.index("cell death") < names.index("cell growth")


def test_search_drops_directional_terms():
    svc = _bare_service()
    metadata = {
        "GO:0006915": {"name": "apoptotic process", "definition": "programmed cell death"},
        "GO:0043065": {"name": "positive regulation of apoptotic process", "definition": "..."},
    }
    query_clean = svc._clean_text("apoptotic process")
    results = svc._search_metadata(metadata, query_clean, 0.2, "BP")
    ids = {r["go_id"] for r in results}
    assert "GO:0006915" in ids
    assert "GO:0043065" not in ids, "signed term must be excluded from search"


# ---------------------------------------------------------------------------
# 3. Corpus-build filter + whitelist self-consistency
# ---------------------------------------------------------------------------

def test_corpus_build_whitelist_is_neutral_and_wellformed():
    from scripts import precompute_go_hierarchy as pgh

    assert pgh.GENERIC_BP_WHITELIST, "whitelist should be non-empty"
    go_id_re = re.compile(r"^GO:\d{7}$")
    for go_id, label in pgh.GENERIC_BP_WHITELIST.items():
        assert go_id_re.match(go_id), f"malformed GO id: {go_id}"
        # A whitelisted generic term must itself be neutral.
        assert not pgh.is_directional_label(label), f"whitelisted term is directional: {label}"


def test_corpus_build_directional_helper_matches_runtime():
    """The build-time and runtime directional filters must agree (kept in sync)."""
    from scripts import precompute_go_hierarchy as pgh

    for name in DIRECTIONAL:
        assert pgh.is_directional_label(name) is True
    for name in NEUTRAL:
        assert pgh.is_directional_label(name) is False
