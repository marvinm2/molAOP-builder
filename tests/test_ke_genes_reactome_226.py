"""Reactome mappings must contribute genes to /api/ke-genes and /api/ke-gene-counts (#226).

Both endpoints resolved WikiPathways and GO mappings and stopped there, so an
approved Reactome mapping contributed zero genes to the union and produced no
entry in ``groups`` — while the GMT exports, fed from the same annotation file,
carried its full gene set. Two Builder surfaces disagreeing about the same
mapping is the hazard; the endpoint is the one framed as the authoritative
per-KE gene view, and its count is what a curator uses to judge whether a Key
Event can be tested at all.

The `type=reactome` case is worse than an undercount: the mapper page ships a
Reactome tab that passes `?type=reactome`, which matched no branch and returned
an empty union for every Key Event.

Tests monkeypatch the blueprint-module globals directly, per the
tests/test_main_blueprint.py pattern — the client fixture's temp-DB rebind does
not reach globals bound once at create_app() time.
"""
import pytest

import src.blueprints.main as main_module


WP_GENES = {"WP707": ["AAA", "BBB", "SHARED"]}
GO_GENES = {"GO:0097300": ["CCC", "SHARED"]}
REACTOME_GENES = {"R-HSA-5218859": ["DDD", "EEE", "SHARED"]}

KE = "KE 1097"


class _FakeModel:
    def __init__(self, rows):
        self._rows = rows

    def get_all_mappings(self):
        return list(self._rows)


@pytest.fixture
def wired(monkeypatch):
    """Wire all three mapping models plus deterministic gene sources."""
    monkeypatch.setattr(main_module, "mapping_model", _FakeModel([
        {"ke_id": KE, "wp_id": "WP707", "wp_title": "DNA damage response",
         "confidence_level": "high"},
    ]))
    monkeypatch.setattr(main_module, "go_mapping_model", _FakeModel([
        {"ke_id": KE, "go_id": "GO:0097300", "go_name": "programmed necrotic cell death",
         "confidence_level": "medium"},
    ]))
    monkeypatch.setattr(main_module, "reactome_mapping_model", _FakeModel([
        {"ke_id": KE, "reactome_id": "R-HSA-5218859",
         "pathway_name": "RIPK1-mediated regulated necrosis",
         "confidence_level": "medium"},
    ]))
    # Patch the underlying corpus readers, not the blueprint's wrappers, so the
    # assertions describe endpoint behaviour rather than the current plumbing.
    monkeypatch.setattr(
        "src.services.go_annotation_index.get_go_annotations_merged",
        lambda **kwargs: GO_GENES,
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter._load_reactome_annotations",
        lambda path=None: REACTOME_GENES,
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter._fetch_pathway_genes_batch",
        lambda wp_ids, cache_model=None: {k: v for k, v in WP_GENES.items() if k in wp_ids},
    )


def test_ke_genes_union_includes_reactome(client, wired):
    """The default (type=all) union is WP + GO + Reactome, de-duplicated."""
    body = client.get("/api/ke-genes/%s" % KE).get_json()
    assert body["genes"] == ["AAA", "BBB", "CCC", "DDD", "EEE", "SHARED"]


def test_ke_genes_emits_a_reactome_group(client, wired):
    """A Reactome mapping gets its own group, named and typed like the others."""
    body = client.get("/api/ke-genes/%s" % KE).get_json()
    groups = {g["type"]: g for g in body["groups"]}
    assert set(groups) == {"wp", "go", "reactome"}

    reactome = groups["reactome"]
    assert reactome["id"] == "R-HSA-5218859"
    assert reactome["name"] == "RIPK1-mediated regulated necrosis"
    assert reactome["confidence_level"] == "medium"
    assert reactome["genes"] == ["DDD", "EEE", "SHARED"]


def test_ke_genes_type_reactome_is_not_empty(client, wired):
    """?type=reactome matched no branch at all — the mapper's Reactome tab."""
    body = client.get("/api/ke-genes/%s?type=reactome" % KE).get_json()
    assert body["genes"] == ["DDD", "EEE", "SHARED"]
    assert [g["type"] for g in body["groups"]] == ["reactome"]


def test_ke_genes_type_filters_stay_exclusive(client, wired):
    """Adding Reactome must not leak into the single-resource views."""
    wp = client.get("/api/ke-genes/%s?type=wp" % KE).get_json()
    assert [g["type"] for g in wp["groups"]] == ["wp"]

    go = client.get("/api/ke-genes/%s?type=go" % KE).get_json()
    assert [g["type"] for g in go["groups"]] == ["go"]


def test_ke_gene_counts_include_reactome(client, wired):
    """The count is the size of the same union /api/ke-genes returns."""
    counts = client.get("/api/ke-gene-counts").get_json()
    assert counts[KE] == 6

    genes = client.get("/api/ke-genes/%s" % KE).get_json()["genes"]
    assert counts[KE] == len(genes)


def test_ke_gene_counts_type_reactome_is_not_empty(client, wired):
    counts = client.get("/api/ke-gene-counts?type=reactome").get_json()
    assert counts == {KE: 3}


def test_reactome_only_ke_is_no_longer_invisible(client, monkeypatch):
    """A KE mapped in Reactome alone reported no genes and no count at all."""
    monkeypatch.setattr(main_module, "mapping_model", _FakeModel([]))
    monkeypatch.setattr(main_module, "go_mapping_model", _FakeModel([]))
    monkeypatch.setattr(main_module, "reactome_mapping_model", _FakeModel([
        {"ke_id": KE, "reactome_id": "R-HSA-5218859",
         "pathway_name": "RIPK1-mediated regulated necrosis",
         "confidence_level": "medium"},
    ]))
    monkeypatch.setattr(
        "src.services.go_annotation_index.get_go_annotations_merged",
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        "src.exporters.gmt_exporter._load_reactome_annotations",
        lambda path=None: REACTOME_GENES,
    )

    assert client.get("/api/ke-gene-counts").get_json() == {KE: 3}
    assert client.get("/api/ke-genes/%s" % KE).get_json()["genes"] == [
        "DDD", "EEE", "SHARED"
    ]


def test_reactome_loader_is_the_exporters_own(monkeypatch):
    """The endpoints and the GMT export must read one annotation source.

    Anything else re-opens the class of bug this issue is: two surfaces able to
    disagree about what a mapping contributes.
    """
    monkeypatch.setattr(
        "src.exporters.gmt_exporter._load_reactome_annotations",
        lambda path=None: {"sentinel": ["ZZZ"]},
    )
    assert main_module._load_reactome_annotations_for_api() == {"sentinel": ["ZZZ"]}


def test_annotation_loaders_degrade_to_empty(monkeypatch):
    """A missing corpus empties the gene rows; it must not 500 the endpoint."""
    def boom(*args, **kwargs):
        raise RuntimeError("corpus unavailable")

    monkeypatch.setattr("src.exporters.gmt_exporter._load_reactome_annotations", boom)
    monkeypatch.setattr(
        "src.services.go_annotation_index.get_go_annotations_merged", boom
    )
    assert main_module._load_reactome_annotations_for_api() == {}
    assert main_module._load_go_annotations_for_api() == {}


def test_gene_group_badge_has_a_reactome_style():
    """renderGeneGroups builds the badge class from group.type."""
    import os
    css_path = os.path.join(os.path.dirname(__file__), "..", "static", "css", "aop-graph.css")
    with open(css_path, "r", encoding="utf-8") as fh:
        css = fh.read()
    assert ".gene-group__type-badge--reactome" in css
