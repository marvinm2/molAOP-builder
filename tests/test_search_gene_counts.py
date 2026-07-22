"""
Tests for resolved gene-set size on the search endpoints (#210).

Gene-set size is a hard gate downstream: the molAOP Analyser refuses to test a
Key Event whose mapped genes number fewer than five. A mapping can therefore be
semantically perfect and still leave its Key Event silently excluded from every
analysis, with nothing in the curation UI warning about it. KE 1097 -> GO:0097300
is the worked example — the correct term, five genes, three of them measured.

The search serializers emitted no size field at all, so the number had to be read
off the container by hand. There was also no test file touching search_go_terms.
"""
import pytest

from src.suggestions.go import GoSuggestionService
from src.services.go_annotation_index import reset_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def go_service(tmp_path, monkeypatch):
    """A GO service with tiny in-memory corpora and no embeddings loaded.

    GO:0008219 mirrors the real shape after #208: a well-populated general term
    whose direct annotation count is tiny. GO:0097300 is the five-gene term from
    the issue, and GO:0000001 has no annotations at all.
    """
    svc = GoSuggestionService.__new__(GoSuggestionService)
    svc.go_metadata = {
        "GO:0008219": {"name": "cell death", "definition": "Any biological process that results in permanent cessation."},
        "GO:0097300": {"name": "programmed necrotic cell death", "definition": "A necrotic cell death process."},
        "GO:0000001": {"name": "orphan process", "definition": "Nothing annotated here."},
    }
    svc.go_mf_metadata = {}
    svc.go_gene_annotations = {
        "GO:0008219": [f"G{i}" for i in range(891)],
        "GO:0097300": ["A", "B", "C", "D", "E"],
    }
    svc.go_mf_gene_annotations = {}

    direct = {"GO:0008219": 7, "GO:0097300": 5}
    monkeypatch.setattr(
        "src.services.go_annotation_index.get_go_direct_counts",
        lambda ns="bp", **kw: direct if ns == "bp" else {},
    )
    return svc


def _by_id(results):
    return {r["go_id"]: r for r in results}


# ---------------------------------------------------------------------------
# The counts are present at all
# ---------------------------------------------------------------------------

def test_fuzzy_search_emits_gene_count(go_service):
    hit = _by_id(go_service.search_go_terms("cell death"))["GO:0008219"]
    assert hit["go_gene_count"] == 891


def test_go_id_lookup_emits_gene_count(go_service):
    """The direct-ID branch is a separate serializer and was missed just as easily."""
    results = go_service.search_go_terms("GO:0097300")
    assert len(results) == 1
    assert results[0]["go_gene_count"] == 5


def test_zero_annotation_term_reports_zero_not_missing(go_service):
    """The frontend distinguishes "zero genes" from "unknown" — zero must be an
    explicit 0, or a genuinely untestable term renders no warning at all."""
    hit = _by_id(go_service.search_go_terms("orphan process"))["GO:0000001"]
    assert hit["go_gene_count"] == 0


# ---------------------------------------------------------------------------
# Propagated vs direct — the #208 interaction
# ---------------------------------------------------------------------------

def test_reports_propagated_and_direct_separately(go_service):
    """Both numbers, because either alone misleads. The propagated count is what
    governs testability; the direct count is how much is annotated to the term
    itself. Showing only the direct count is what made correct general terms
    look untestable before #208 — GO:0008219 measures 7 direct against 891
    propagated."""
    hit = _by_id(go_service.search_go_terms("cell death"))["GO:0008219"]
    assert hit["go_gene_count"] == 891
    assert hit["go_gene_count_direct"] == 7


def test_gene_count_is_the_propagated_one(go_service):
    """Guard against the counts being swapped: the field the UI thresholds on
    must be the larger, propagated figure."""
    hit = _by_id(go_service.search_go_terms("cell death"))["GO:0008219"]
    assert hit["go_gene_count"] > hit["go_gene_count_direct"]


# ---------------------------------------------------------------------------
# Degradation — a count must never break search
# ---------------------------------------------------------------------------

def test_search_survives_missing_direct_counts(go_service, monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("no annotations on this deployment")

    monkeypatch.setattr("src.services.go_annotation_index.get_go_direct_counts", _boom)
    hit = _by_id(go_service.search_go_terms("cell death"))["GO:0008219"]
    assert hit["go_gene_count"] == 891
    assert "go_gene_count_direct" not in hit


def test_search_survives_missing_annotations_entirely(go_service):
    go_service.go_gene_annotations = {}
    results = go_service.search_go_terms("cell death")
    assert results, "search must still return results without an annotation corpus"
    assert _by_id(results)["GO:0008219"]["go_gene_count"] == 0


# ---------------------------------------------------------------------------
# Reactome search
# ---------------------------------------------------------------------------

def test_reactome_search_emits_gene_count():
    """/suggest_reactome already emitted this; search did not — which is exactly
    the path curators fell back on while Reactome ranking was broken (#209)."""
    from src.suggestions.reactome import ReactomeSuggestionService

    svc = ReactomeSuggestionService.__new__(ReactomeSuggestionService)
    svc.reactome_metadata = {
        "R-HSA-5218859": {"name": "Regulated Necrosis", "description": "Necrosis."},
    }
    svc.reactome_gene_annotations = {"R-HSA-5218859": [f"G{i}" for i in range(61)]}

    fuzzy = svc.search_reactome_terms("necrosis")
    assert fuzzy[0]["reactome_pathway_gene_count"] == 61

    by_id = svc.search_reactome_terms("R-HSA-5218859")
    assert by_id[0]["reactome_pathway_gene_count"] == 61


def test_reactome_search_survives_missing_annotations():
    from src.suggestions.reactome import ReactomeSuggestionService

    svc = ReactomeSuggestionService.__new__(ReactomeSuggestionService)
    svc.reactome_metadata = {"R-HSA-1": {"name": "Necrosis pathway", "description": ""}}
    svc.reactome_gene_annotations = {}

    results = svc.search_reactome_terms("necrosis")
    assert results
    assert results[0]["reactome_pathway_gene_count"] == 0
