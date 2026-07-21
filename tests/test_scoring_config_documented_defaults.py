"""
Guard against silent scoring-config drift (#192).

Two parameters had diverged three ways at once: the deployed
``scoring_config.yaml`` said one thing, the code fallback defaults said
another, and the CHANGELOG described a third behaviour. Nothing flagged it,
so the tool's actual ranking method could not be established from the repo —
which matters, because these values are part of the method being written up,
not incidental configuration.

The resolution (#192) was that the DEPLOYED values are correct and the
documentation was stale. These tests pin all three surfaces together, so a
future change to any one of them fails until the others follow:

  1. The YAML values, for both go_bp and go_mf.
  2. The code fallback defaults used when the config is absent.
  3. The behavioural consequence — ic_weight 0.0 means the IC multiplier is
     identically 1.0 and cannot re-rank anything.

If you intend to change a value here, change it in scoring_config.yaml, in
src/suggestions/go.py, in docs/SCORING_CONFIG.md, and in this file — that is
the point of the test, not an obstacle to it.
"""

import os

import pytest
import yaml

from src import PROJECT_ROOT
from src.suggestions.go import GoSuggestionService, _NamespaceData


# The single source of truth for what the deployed instance does.
DOCUMENTED = {
    "ic_weight": 0.0,
    "redundancy_threshold": 0.10,
}


@pytest.fixture(scope="module")
def raw_config():
    with open(os.path.join(PROJECT_ROOT, "scoring_config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _hierarchy_block(raw_config, namespace):
    """Locate the go_bp / go_mf hierarchy block wherever it is nested."""
    def walk(node):
        if isinstance(node, dict):
            if namespace in node and isinstance(node[namespace], dict):
                block = node[namespace].get("hierarchy")
                if isinstance(block, dict):
                    return block
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        return None

    block = walk(raw_config)
    assert block is not None, f"no {namespace}.hierarchy block in scoring_config.yaml"
    return block


@pytest.mark.parametrize("namespace", ["go_bp", "go_mf"])
@pytest.mark.parametrize("param", sorted(DOCUMENTED))
def test_deployed_config_matches_documented_value(raw_config, namespace, param):
    """1. The YAML matches what the docs and docstrings claim, for both namespaces."""
    block = _hierarchy_block(raw_config, namespace)

    assert block[param] == pytest.approx(DOCUMENTED[param]), (
        f"{namespace}.hierarchy.{param} is {block[param]} but documented as "
        f"{DOCUMENTED[param]}. Update docs/SCORING_CONFIG.md, the go.py "
        f"docstring, and DOCUMENTED in this test together with the config."
    )


def test_ic_boost_is_a_no_op_when_config_absent():
    """2+3. The code fallback also disables IC, and disabling it changes no score."""
    service = GoSuggestionService.__new__(GoSuggestionService)  # no data loading
    ns_data = _NamespaceData(
        embeddings=None,
        name_embeddings=None,
        metadata={},
        annotations={},
        hierarchy={"GO:0006915": {"ic_score": 0.9, "depth": 7}},
        config=None,  # forces the code fallback default
    )
    suggestions = [{"go_id": "GO:0006915", "hybrid_score": 0.5000}]

    result = service._apply_ic_boost(suggestions, ns_data)

    assert result[0]["hybrid_score"] == pytest.approx(0.5), (
        "IC boost altered the score with no config present — the code fallback "
        "default for ic_weight is no longer 0.0 and has drifted from the deployed config."
    )
    # The hierarchy pipeline still earns its keep: depth is attached for the UI.
    assert result[0]["depth"] == 7


def test_redundancy_threshold_code_default_matches_config():
    """2. The redundancy-filter fallback default matches the deployed value.

    Read through behaviour rather than source inspection: with no config, an
    ancestor is pruned unless it beats its child by more than the threshold.
    A 0.15 margin must survive a 0.10 threshold and would be pruned by 0.20.
    """
    service = GoSuggestionService.__new__(GoSuggestionService)
    ancestor, child = "GO:0008219", "GO:0012501"
    ns_data = _NamespaceData(
        embeddings=None,
        name_embeddings=None,
        metadata={},
        annotations={},
        hierarchy={
            ancestor: {"ancestors": set()},
            child: {"ancestors": {ancestor}},
        },
        config=None,
    )
    suggestions = [
        {"go_id": ancestor, "hybrid_score": 0.69, "go_name": "cell death"},
        {"go_id": child, "hybrid_score": 0.60, "go_name": "programmed cell death"},
    ]

    kept = {s["go_id"] for s in service._filter_redundant_ancestors(suggestions, ns_data)}

    assert ancestor in kept, (
        "ancestor beating its child by 15% was pruned — the redundancy_threshold "
        f"code default has drifted above the documented {DOCUMENTED['redundancy_threshold']}."
    )
