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

from src.core.config_loader import ConfigLoader
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


# ---------------------------------------------------------------------------
# WikiPathways search and suggestions (#223)
# ---------------------------------------------------------------------------

WP_SNAPSHOT = {
    # Ferroptosis — the pathway the curator could not size before committing
    # KE 1097 to it. 64 genes, matching the ke-wp GMT export line exactly.
    "WP4313": [f"G{i}" for i in range(64)],
    # DNA damage response, KE 1194. 69 genes, likewise GMT-exact.
    "WP707": [f"H{i}" for i in range(69)],
}

WP_METADATA = [
    {
        "pathwayID": "WP4313",
        "pathwayTitle": "Ferroptosis",
        "pathwayDescription": "Ferroptosis is a type of programmed cell death.",
        "pathwayLink": "https://identifiers.org/wikipathways/WP4313",
        "ontologyTags": ["ferroptosis pathway"],
        "publications": [],
    },
    {
        "pathwayID": "WP707",
        "pathwayTitle": "DNA damage response",
        "pathwayDescription": "Response to DNA damage.",
        "pathwayLink": "https://identifiers.org/wikipathways/WP707",
        "ontologyTags": [],
        "publications": [],
    },
    {
        # In the search corpus but outside the snapshot's [10, 500] gene band —
        # size genuinely unknown, and reporting 0 would be a fabrication.
        "pathwayID": "WP528",
        "pathwayTitle": "Acetylcholine synthesis",
        "pathwayDescription": "A small pathway.",
        "pathwayLink": "https://identifiers.org/wikipathways/WP528",
        "ontologyTags": [],
        "publications": [],
    },
]


@pytest.fixture
def wp_service(monkeypatch):
    """A pathway service with a tiny in-memory corpus and no network."""
    from src.suggestions.pathway import PathwaySuggestionService

    svc = PathwaySuggestionService.__new__(PathwaySuggestionService)
    svc.cache_model = None
    svc.embedding_service = None
    svc.ke_override_model = None
    svc.wikipathways_endpoint = "https://sparql.wikipathways.org/sparql"
    svc.wikipathways_gene_annotations = dict(WP_SNAPSHOT)
    svc.config = ConfigLoader.get_default_config()
    monkeypatch.setattr(
        PathwaySuggestionService,
        "_get_all_pathways_for_search",
        lambda self: [dict(p) for p in WP_METADATA],
    )
    return svc


def _wp_by_id(results):
    return {r["pathwayID"]: r for r in results}


def test_wp_id_lookup_emits_gene_count(wp_service):
    """WP4313 is the worked example: the curator had no way to size it at all
    before committing KE 1097 to it, and had to fall back to reading the
    corpus by hand."""
    results = wp_service.search_pathways("WP4313")
    assert len(results) == 1
    assert results[0]["pathway_total_genes"] == 64


def test_wp_fuzzy_search_emits_gene_count(wp_service):
    hit = _wp_by_id(wp_service.search_pathways("DNA damage response"))["WP707"]
    assert hit["pathway_total_genes"] == 69


def test_wp_unknown_size_is_null_not_zero(wp_service):
    """The snapshot is filtered to [10, 500] genes, so an absent pathway has an
    unknown size, not an empty gene set. This is where WikiPathways differs from
    GO and Reactome, whose annotation files cover their whole corpus. Emitting 0
    would make the frontend warn about a pathway that may hold hundreds of
    genes."""
    hit = _wp_by_id(wp_service.search_pathways("Acetylcholine synthesis"))["WP528"]
    assert hit["pathway_total_genes"] is None


def test_wp_search_survives_missing_annotation_file(wp_service):
    """data/*.json is gitignored and arrives through a bind mount. A deployment
    without it must lose the chip, not the search — and must not report 0 genes
    for all 803 pathways, which would warn on every candidate."""
    wp_service.wikipathways_gene_annotations = {}
    results = wp_service.search_pathways("WP4313")
    assert len(results) == 1
    assert results[0]["pathway_total_genes"] is None


def test_wp_embedding_suggestions_emit_gene_count(wp_service):
    """Under v1.5 the gene weight is 0, so an embedding-only suggestion is the
    norm — and it was the one kind of suggestion carrying no size at all: the
    field was seeded as a literal 0 placeholder and only ever filled from the
    gene path's SPARQL round-trip."""
    class _FakeEmbeddings:
        def compute_ke_pathways_batch_similarity(self, **kwargs):
            return [
                {
                    "pathwayID": p["pathwayID"],
                    "pathwayTitle": p["pathwayTitle"],
                    "pathwayDescription": p["pathwayDescription"],
                    "pathwayLink": p["pathwayLink"],
                    "pathwaySvgUrl": "",
                    "combined_similarity": 0.9,
                    "title_similarity": 0.9,
                    "description_similarity": 0.9,
                }
                for p in WP_METADATA
            ]

    wp_service.embedding_service = _FakeEmbeddings()
    suggestions = wp_service._get_embedding_based_suggestions(
        "Event:1097", "Occurrence, renal proximal tubular necrosis", "", "Cellular"
    )
    by_id = _wp_by_id(suggestions)
    assert by_id["WP4313"]["pathway_total_genes"] == 64
    assert by_id["WP707"]["pathway_total_genes"] == 69
    assert by_id["WP528"]["pathway_total_genes"] is None


def test_wp_gene_based_count_falls_back_to_snapshot(wp_service):
    """The gene path defaulted an unresolved pathway to a literal 100, which
    feeds pathway_specificity and the confidence score. The snapshot is a real
    number and should be preferred over that placeholder."""
    import json as _json

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"results": {"bindings": []}}

    def _fake_post(url, **kwargs):
        return _Resp()

    import src.suggestions.pathway as pathway_mod
    original_post = pathway_mod.requests.post
    pathway_mod.requests.post = _fake_post
    try:
        results = [
            {
                "pathwayID": "WP4313",
                "matching_gene_count": 4,
                "pathwayTitle": "Ferroptosis",
            }
        ]
        wp_service._process_gene_pathway_results = lambda data, genes: results
        wp_service._get_pathway_gene_counts = lambda ids: {}
        out = wp_service._find_pathways_by_genes(
            [{"symbol": "GPX4", "ncbi": "2879", "hgnc": "4556"}], limit=5
        )
        assert out[0]["pathway_total_genes"] == 64
        assert out[0]["pathway_specificity"] == round(4 / 64, 3)
    finally:
        pathway_mod.requests.post = original_post
    del _json


def test_wp_annotation_loader_tolerates_missing_file(tmp_path):
    from src.suggestions.pathway import PathwaySuggestionService

    svc = PathwaySuggestionService.__new__(PathwaySuggestionService)
    svc.wikipathways_gene_annotations = {}
    svc._load_wikipathways_annotations(str(tmp_path / "nope.json"))
    assert svc.wikipathways_gene_annotations == {}


def test_wp_annotation_loader_reads_absolute_path(tmp_path):
    import json as _json
    from src.suggestions.pathway import PathwaySuggestionService

    path = tmp_path / "wp.json"
    path.write_text(_json.dumps({"WP4313": ["A", "B", "C"]}))

    svc = PathwaySuggestionService.__new__(PathwaySuggestionService)
    svc.wikipathways_gene_annotations = {}
    svc._load_wikipathways_annotations(str(path))
    assert svc._gene_count_for("WP4313") == 3
