"""
Tests for ontology-propagated GO gene annotations (#208).

`data/go_bp_gene_annotations.json` holds direct GAF annotations only, so gene
sets violated the GO true-path rule — a gene annotated to a term is annotated to
all of its ancestors. Measured on the shipped corpus before the fix:

    GO:0006915  apoptotic process       667
    GO:0012501  programmed cell death    35   <- its parent
    GO:0008219  cell death                7   <- its grandparent

Parents smaller than their children, and only 16 of GO:0006915's 667 genes even
present in its parent. The consequence for curation is the point: picking the
most descriptive faithful term is correct KE->GO practice, and it produced a
gene set below the Analyser's 5-gene testability floor.
"""
import json
import os

import pytest

from src.services.go_annotation_index import (
    build_closure,
    get_go_annotations,
    get_go_annotations_merged,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def toy_ontology(tmp_path):
    """A diamond plus a part_of edge.

           root
          /    \\
      left      right
          \\    /
           leaf        (leaf is_a both left and right)
           |
        component      (part_of leaf)

    `ancestors` is the transitive closure, matching what
    precompute_go_hierarchy.py writes.
    """
    hierarchy = {
        "GO:root":  {"ancestors": []},
        "GO:left":  {"ancestors": ["GO:root"]},
        "GO:right": {"ancestors": ["GO:root"]},
        "GO:leaf":  {"ancestors": ["GO:left", "GO:right", "GO:root"]},
        "GO:comp":  {"ancestors": ["GO:leaf", "GO:left", "GO:right", "GO:root"]},
    }
    direct = {
        "GO:leaf": ["A", "B"],
        "GO:comp": ["C"],
        "GO:right": ["D"],
        "GO:obsolete": ["Z"],   # absent from the hierarchy
    }
    h = tmp_path / "hier.json"
    d = tmp_path / "direct.json"
    h.write_text(json.dumps(hierarchy))
    d.write_text(json.dumps(direct))
    return {"hierarchy": hierarchy, "direct": direct,
            "hierarchy_path": str(h), "annotations_path": str(d),
            "missing_path": str(tmp_path / "nope.json")}


# ---------------------------------------------------------------------------
# The true-path rule
# ---------------------------------------------------------------------------

def test_genes_propagate_to_all_ancestors(toy_ontology):
    c = build_closure(toy_ontology["direct"], toy_ontology["hierarchy"])
    assert set(c["GO:leaf"]) == {"A", "B", "C"}
    assert set(c["GO:root"]) == {"A", "B", "C", "D"}


def test_diamond_ancestor_counted_once(toy_ontology):
    """A term reachable by two paths must not accumulate duplicates."""
    c = build_closure(toy_ontology["direct"], toy_ontology["hierarchy"])
    assert c["GO:root"] == sorted(set(c["GO:root"]))


def test_part_of_edges_propagate(toy_ontology):
    """GO:comp is part_of GO:leaf; its gene must reach leaf and above."""
    c = build_closure(toy_ontology["direct"], toy_ontology["hierarchy"])
    assert "C" in c["GO:leaf"]
    assert "C" in c["GO:root"]


def test_parent_is_always_a_superset_of_its_children(toy_ontology):
    """The invariant the bug violated. Every child set must be contained in
    every one of its ancestors' sets."""
    c = build_closure(toy_ontology["direct"], toy_ontology["hierarchy"])
    for go_id, entry in toy_ontology["hierarchy"].items():
        child = set(c.get(go_id, []))
        for ancestor in entry["ancestors"]:
            assert child <= set(c.get(ancestor, [])), (
                f"{go_id} ({len(child)} genes) is not contained in its "
                f"ancestor {ancestor} ({len(c.get(ancestor, []))} genes)"
            )


def test_terms_absent_from_hierarchy_are_skipped(toy_ontology):
    """Obsolete / out-of-namespace annotation keys have nowhere to propagate."""
    c = build_closure(toy_ontology["direct"], toy_ontology["hierarchy"])
    assert "GO:obsolete" not in c
    assert not any("Z" in genes for genes in c.values())


def test_closure_never_shrinks_a_set(toy_ontology):
    c = build_closure(toy_ontology["direct"], toy_ontology["hierarchy"])
    for go_id, genes in toy_ontology["direct"].items():
        if go_id in toy_ontology["hierarchy"]:
            assert set(genes) <= set(c[go_id])


# ---------------------------------------------------------------------------
# Source selection and degradation
# ---------------------------------------------------------------------------

def test_prefers_the_precomputed_propagated_file(tmp_path, toy_ontology):
    """The precomputed file wins, because it carries the obsolete-term remap
    that a load-time closure cannot reconstruct without the OBO."""
    pre = tmp_path / "pre.json"
    pre.write_text(json.dumps({"GO:root": ["PRECOMPUTED"]}))

    out = get_go_annotations(
        "bp",
        propagated_path=str(pre),
        annotations_path=toy_ontology["annotations_path"],
        hierarchy_path=toy_ontology["hierarchy_path"],
        use_cache=False,
    )
    assert out == {"GO:root": ["PRECOMPUTED"]}


def test_falls_back_to_load_time_closure(toy_ontology):
    """data/*.json is gitignored, so CI and fresh checkouts have no precomputed
    file and must still get propagated sets."""
    out = get_go_annotations(
        "bp",
        propagated_path=toy_ontology["missing_path"],
        annotations_path=toy_ontology["annotations_path"],
        hierarchy_path=toy_ontology["hierarchy_path"],
        use_cache=False,
    )
    assert set(out["GO:root"]) == {"A", "B", "C", "D"}


def test_falls_back_to_direct_when_hierarchy_missing(toy_ontology):
    """Wrong under the true-path rule, but it is what the app did before #208 —
    a floor, not an exception."""
    out = get_go_annotations(
        "bp",
        propagated_path=toy_ontology["missing_path"],
        annotations_path=toy_ontology["annotations_path"],
        hierarchy_path=toy_ontology["missing_path"],
        use_cache=False,
    )
    assert out == toy_ontology["direct"]


def test_all_sources_missing_returns_empty(toy_ontology):
    out = get_go_annotations(
        "bp",
        propagated_path=toy_ontology["missing_path"],
        annotations_path=toy_ontology["missing_path"],
        hierarchy_path=toy_ontology["missing_path"],
        use_cache=False,
    )
    assert out == {}


def test_repeated_reads_of_an_unchanged_file_are_cached(tmp_path):
    bp = tmp_path / "bp.json"
    bp.write_text(json.dumps({"GO:1": ["A"]}))
    first = get_go_annotations("bp", propagated_path=str(bp))
    assert get_go_annotations("bp", propagated_path=str(bp)) is first
    reset_cache()
    assert get_go_annotations("bp", propagated_path=str(bp)) is not first


def test_cache_reloads_when_the_corpus_file_changes(tmp_path):
    """data/ is a bind mount, so the corpus can be refreshed under the process.

    Holding the first load forever made the GMT export layer stamp a *new*
    corpus fingerprint onto bytes built from the *old* corpus, and never retry.
    """
    bp = tmp_path / "bp.json"
    bp.write_text(json.dumps({"GO:1": ["A"]}))
    assert get_go_annotations("bp", propagated_path=str(bp)) == {"GO:1": ["A"]}

    bp.write_text(json.dumps({"GO:1": ["CHANGED"]}))
    os.utime(bp, (0, 0))  # same size is not enough; prove it is not size alone
    assert get_go_annotations("bp", propagated_path=str(bp)) == {"GO:1": ["CHANGED"]}


def test_direct_counts_reload_when_the_corpus_file_changes(tmp_path):
    from src.services.go_annotation_index import get_go_direct_counts

    bp = tmp_path / "bp_direct.json"
    bp.write_text(json.dumps({"GO:1": ["A", "B"]}))
    assert get_go_direct_counts("bp", annotations_path=str(bp)) == {"GO:1": 2}

    bp.write_text(json.dumps({"GO:1": ["A", "B", "C"]}))
    os.utime(bp, (0, 0))
    assert get_go_direct_counts("bp", annotations_path=str(bp)) == {"GO:1": 3}


def test_merged_includes_both_namespaces(tmp_path):
    bp = tmp_path / "bp.json"
    mf = tmp_path / "mf.json"
    bp.write_text(json.dumps({"GO:bp": ["A"]}))
    mf.write_text(json.dumps({"GO:mf": ["B"]}))

    import src.services.go_annotation_index as idx

    real = idx.get_go_annotations

    def fake(namespace="bp", **kwargs):
        return real(namespace, propagated_path=str(bp if namespace == "bp" else mf),
                    use_cache=False)

    idx.get_go_annotations = fake
    try:
        merged = get_go_annotations_merged()
    finally:
        idx.get_go_annotations = real

    assert merged == {"GO:bp": ["A"], "GO:mf": ["B"]}
