"""
Tests for GO suggestion title-match / proxy weighting (#193).

For a generic KE, the generic process term should be surfaced, not evicted by a
specific descendant or a "regulation of X" proxy. These tests exercise the pure
ranking helpers with synthetic data (no BioBERT), so they are fast/deterministic:

- _title_match_kind: exact / near / None classification.
- _apply_title_and_proxy_weighting: exact-title term floats to #1 over a
  higher-scoring child; "regulation of X" proxy is demoted; "response to X" is not.
- _filter_redundant_ancestors: a near-title-match ancestor is not pruned by a
  higher-scoring descendant.
"""
import types

from src.suggestions.go import GoSuggestionService


def svc():
    return object.__new__(GoSuggestionService)


# --- _title_match_kind ------------------------------------------------------

def test_title_match_kind():
    s = svc()
    assert s._title_match_kind("cell death", "cell death") == "exact"
    assert s._title_match_kind("dna damage", "dna damage response") == "near"
    assert s._title_match_kind("apoptotic process", "regulation of apoptotic process") == "near"
    assert s._title_match_kind("cell death", "cell growth") is None
    assert s._title_match_kind("", "cell death") is None


# --- _apply_title_and_proxy_weighting --------------------------------------

def test_exact_title_beats_higher_scoring_child():
    s = svc()
    sugg = [
        {"go_id": "GO:child", "go_name": "lymphocyte apoptotic process", "hybrid_score": 0.95},
        {"go_id": "GO:0006915", "go_name": "apoptotic process", "hybrid_score": 0.88},
    ]
    out = s._apply_title_and_proxy_weighting(sugg, "Increase, apoptotic process")
    assert out[0]["go_id"] == "GO:0006915", "exact-title generic term should rank first"
    assert out[0].get("title_match") == "exact"


def test_regulation_proxy_is_demoted_but_response_is_not():
    s = svc()
    sugg = [
        {"go_id": "GO:reg", "go_name": "regulation of apoptotic process", "hybrid_score": 0.90},
        {"go_id": "GO:resp", "go_name": "response to oxidative stress", "hybrid_score": 0.90},
    ]
    s._apply_title_and_proxy_weighting(list(sugg), "apoptotic process")
    reg = next(x for x in sugg if x["go_id"] == "GO:reg")
    resp = next(x for x in sugg if x["go_id"] == "GO:resp")
    # regulation-of proxy penalised; response-to left alone (canonical for stress KEs)
    assert reg["hybrid_score"] < 0.90
    assert resp["hybrid_score"] == 0.90


# --- _filter_redundant_ancestors near-match protection ----------------------

def test_near_title_ancestor_not_pruned():
    s = svc()
    ns = types.SimpleNamespace(
        config=None,
        hierarchy={
            # child GO:0034599 has ancestor GO:0006979 (the generic term)
            "GO:0034599": {"ancestors": {"GO:0006979"}},
            "GO:0006979": {"ancestors": set()},
        },
    )
    sugg = [
        {"go_id": "GO:0034599", "go_name": "cellular response to oxidative stress", "hybrid_score": 0.96},
        {"go_id": "GO:0006979", "go_name": "response to oxidative stress", "hybrid_score": 0.90},
    ]
    out = s._filter_redundant_ancestors(sugg, ns, ke_title="Increase, Oxidative stress")
    ids = {x["go_id"] for x in out}
    assert "GO:0006979" in ids, "near-title-match ancestor must survive redundancy pruning"


def test_unprotected_ancestor_still_pruned():
    s = svc()
    ns = types.SimpleNamespace(
        config=None,
        hierarchy={
            "GO:child": {"ancestors": {"GO:anc"}},
            "GO:anc": {"ancestors": set()},
        },
    )
    sugg = [
        {"go_id": "GO:child", "go_name": "some very specific process", "hybrid_score": 0.90},
        {"go_id": "GO:anc", "go_name": "unrelated broad process", "hybrid_score": 0.85},
    ]
    out = s._filter_redundant_ancestors(sugg, ns, ke_title="something else entirely")
    ids = {x["go_id"] for x in out}
    assert "GO:anc" not in ids, "a non-title-matching ancestor should still be pruned"
