"""Phase 37 (#238) — the gene-set-size filter bounds ranking, not selection.

The `[10, 500]` filter was applied when `pathway_metadata.json` was written, so
it silently governed the manual dropdown and `/search_pathways` as well as the
ranker it was written for. WP699 (*Aflatoxin B1 metabolism*, 7 genes) was
unselectable for the Key Event it is named after, and nine pathways carrying
approved mappings could be read back but never re-selected.

The fixtures deliberately sit **across** the boundary rather than inside it —
WP699 at 7 genes is below the floor and WP5434 at 511 is eleven over the
ceiling — because the failure mode is that everything looks correct for
pathways within the bounds.
"""
import json

import pytest

from src.suggestions.pathway import PathwaySuggestionService


# Real IDs and real sizes, from the measurements in #238.
BELOW_FLOOR = {"pathwayID": "WP699", "pathwayTitle": "Aflatoxin B1 metabolism",
               "pathwayDescription": "Metabolism of aflatoxin B1.",
               "inSuggestionCorpus": False}
ABOVE_CEILING = {"pathwayID": "WP5434", "pathwayTitle": "Fatty acid metabolism",
                 "pathwayDescription": "A large metabolic map.",
                 "inSuggestionCorpus": False}
IN_BOUNDS = {"pathwayID": "WP2873",
             "pathwayTitle": "Aryl hydrocarbon receptor pathway",
             "pathwayDescription": "AhR signalling.",
             "inSuggestionCorpus": True}
LEGACY = {"pathwayID": "WP100", "pathwayTitle": "Glutathione metabolism",
          "pathwayDescription": "Legacy row with no corpus flag."}

ALL_PATHWAYS = [BELOW_FLOOR, ABOVE_CEILING, IN_BOUNDS, LEGACY]


@pytest.fixture
def service(monkeypatch, tmp_path):
    counts = tmp_path / "counts.json"
    counts.write_text(json.dumps({"WP699": 7, "WP5434": 511, "WP2873": 45}))

    svc = PathwaySuggestionService(
        wikipathways_annotations_path=str(tmp_path / "absent.json"),
        wikipathways_gene_counts_path=str(counts),
    )
    monkeypatch.setattr(
        svc, "_get_all_pathways_for_search", lambda: [dict(p) for p in ALL_PATHWAYS]
    )
    return svc


def test_search_corpus_holds_every_pathway(service):
    """Selection must not be bounded by the ranker's filter."""
    ids = {p["pathwayID"] for p in service._get_all_pathways_for_search()}
    assert ids == {"WP699", "WP5434", "WP2873", "WP100"}


def test_ranker_excludes_out_of_bounds_pathways(service):
    """The filter still does its job where its rationale holds."""
    ids = {p["pathwayID"] for p in service._get_rankable_pathways()}
    assert "WP699" not in ids
    assert "WP5434" not in ids
    assert "WP2873" in ids


def test_pathway_without_the_flag_stays_rankable(service):
    """A data mount carrying a corpus written before #238 has no
    inSuggestionCorpus key. Treating that as 'not rankable' would silently
    empty the suggestion list, which is worse than the bug being fixed."""
    assert "WP100" in {p["pathwayID"] for p in service._get_rankable_pathways()}


@pytest.mark.parametrize(
    "query,expected",
    [("WP699", "WP699"), ("wp5434", "WP5434"), ("699", "WP699")],
)
def test_out_of_bounds_pathway_is_findable_by_id(service, query, expected):
    """The clearest case in the issue: WP699 is the mechanism of KE 409 and
    is named after it, and could not be reached by any route."""
    results = service.search_pathways(query)
    assert [r["pathwayID"] for r in results] == [expected]


def test_out_of_bounds_pathway_is_findable_by_name(service):
    results = service.search_pathways("Aflatoxin B1 metabolism")
    assert "WP699" in {r["pathwayID"] for r in results}


def test_search_result_says_why_a_pathway_is_not_suggested(service):
    """The silence is the defect, not the limit — a curator must be able to
    tell 'not offered' from 'does not exist'."""
    result = service.search_pathways("WP699")[0]
    assert result["inSuggestionCorpus"] is False
    assert result["pathway_total_genes"] == 7


def test_gene_count_resolves_for_an_excluded_pathway(service):
    """The annotations snapshot omits out-of-bounds pathways by construction,
    so the count has to come from the second snapshot (#238)."""
    assert service._gene_count_for("WP5434") == 511
    assert service._gene_count_for("WP699") == 7


def test_unknown_pathway_still_reports_none(service):
    """None, not 0 — reporting a fabricated zero was the #223 trap."""
    assert service._gene_count_for("WP99999") is None
