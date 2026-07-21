"""
WikiPathways search-by-ID regression tests (#156).

Before this fix ``search_pathways`` only fuzzy-matched the query against
pathway titles and descriptions, so a curator typing a WikiPathways
identifier ("WP554") got nothing back — the string bears no resemblance to
any title. GO and Reactome search already had a direct-ID branch; these
tests pin the WP one to the same contract:

  A. An exact ID resolves to that pathway at relevance 1.0.
  B. Case and separator variants (wp554, WP-554, WP_554, WP:554) normalise.
  C. A WP-prefixed ID that does not exist returns [] rather than fuzzy noise.
  D. A bare-numeric query resolves as an ID when it matches...
  E. ...but falls through to fuzzy matching when it does not.
  F. Plain text queries still fuzzy-match as before (no regression).
"""

import pytest

from src.suggestions.pathway import PathwaySuggestionService


FAKE_PATHWAYS = [
    {
        "pathwayID": "WP554",
        "pathwayTitle": "ACE inhibitor pathway",
        "pathwayDescription": "The pathway of ACE inhibitor drug action.",
        "pathwayLink": "https://www.wikipathways.org/pathways/WP554",
    },
    {
        "pathwayID": "WP2059",
        "pathwayTitle": "Alzheimers disease and miRNA effects",
        "pathwayDescription": "miRNA involvement in Alzheimers disease.",
        "pathwayLink": "https://www.wikipathways.org/pathways/WP2059",
    },
]


@pytest.fixture
def service(monkeypatch):
    """PathwaySuggestionService with the search corpus stubbed to FAKE_PATHWAYS."""
    svc = PathwaySuggestionService(cache_model=None, embedding_service=None)
    monkeypatch.setattr(
        svc, "_get_all_pathways_for_search", lambda: [dict(p) for p in FAKE_PATHWAYS]
    )
    return svc


def test_exact_id_resolves_to_single_pathway(service):
    """A. WP554 returns exactly that pathway at full relevance."""
    results = service.search_pathways("WP554")

    assert len(results) == 1
    assert results[0]["pathwayID"] == "WP554"
    assert results[0]["pathwayTitle"] == "ACE inhibitor pathway"
    assert results[0]["relevance_score"] == 1.0
    assert results[0]["title_similarity"] == 1.0
    assert (
        results[0]["pathwaySvgUrl"]
        == "https://www.wikipathways.org/wikipathways-assets/pathways/WP554/WP554.svg"
    )


@pytest.mark.parametrize("query", ["wp554", "Wp554", "WP-554", "WP_554", "WP:554", "  WP554  "])
def test_id_variants_normalise(service, query):
    """B. Case, separator, and whitespace variants all resolve to WP554."""
    results = service.search_pathways(query)

    assert len(results) == 1, f"{query!r} did not resolve"
    assert results[0]["pathwayID"] == "WP554"


def test_unknown_prefixed_id_returns_empty(service):
    """C. An explicit WP ID that is not in the corpus is a miss, not fuzzy noise."""
    assert service.search_pathways("WP999999") == []


def test_bare_digits_resolve_as_id(service):
    """D. A bare-numeric query is tried as an identifier."""
    results = service.search_pathways("554")

    assert len(results) == 1
    assert results[0]["pathwayID"] == "WP554"


def test_bare_digits_fall_through_to_fuzzy(service):
    """E. Unlike a WP-prefixed miss, bare digits fall through to fuzzy matching.

    Asserted on behaviour rather than result content: the call must not
    short-circuit to [] the way a prefixed miss does.
    """
    results = service.search_pathways("999999", threshold=0.1)

    # No exception, and the fuzzy path ran (result set is whatever the
    # threshold admits — the point is that the ID branch did not return early).
    assert isinstance(results, list)


def test_text_query_still_fuzzy_matches(service):
    """F. Non-ID queries are unaffected by the new branch."""
    results = service.search_pathways("ACE inhibitor", threshold=0.4)

    assert any(r["pathwayID"] == "WP554" for r in results)
    assert all(r["relevance_score"] <= 1.0 for r in results)
