"""Phase 37 (#236, partial) — stripping a direction word must not leave debris.

`remove_directionality_terms()` is the only transformation applied to a Key
Event title before it is embedded, on both arms: live on a cache miss
(`src/suggestions/pathway.py`) and offline when the artifacts are built
(`scripts/precompute_ke_embeddings.py`).

AOP-Wiki titles are overwhelmingly `<direction>, <entity>`, so removing the
direction word left the separator behind and `"Activation, AhR"` was embedded
as `", AhR"`. The function's own docstring has always documented the tidied
form, so these cases pin the contract it already claimed.

This is the small, independent part of #236. The larger fix — querying both
the acronym and the expanded vocabulary and taking a per-pathway max, since
expansion helps 8 campaign pairs and hurts 11 — is separate and needs the
lexicon at `curation/inputs/acronym_expansions.tsv`.
"""
import pytest

from src.utils.text import remove_directionality_terms


@pytest.mark.parametrize(
    "title,expected",
    [
        # The three examples the docstring has always claimed.
        ("Increase, CYP2E1", "CYP2E1"),
        ("Activation of EGFR signaling", "EGFR signaling"),
        ("Decreased mitochondrial function", "mitochondrial function"),
        # Real titles from the D3.2 liver-network campaign.
        ("Activation, AhR", "AhR"),
        ("Inhibition, Bile Salt Export Pump (ABCB11)",
         "Bile Salt Export Pump (ABCB11)"),
        ("Increased, De Novo FA synthesis", "De Novo FA synthesis"),
    ],
)
def test_no_separator_survives_the_strip(title, expected):
    assert remove_directionality_terms(title) == expected


@pytest.mark.parametrize(
    "title",
    ["Activation, AhR", "Increase, CYP2E1", "Increased, De Novo FA synthesis"],
)
def test_result_never_starts_with_punctuation(title):
    """The specific defect: a leading comma reached the encoder, where BioBERT
    matched on character shape."""
    assert not remove_directionality_terms(title).startswith((",", ";", ":", "-"))


def test_a_word_beginning_with_a_connector_is_not_truncated():
    """The leading-connector strip is word-bounded, so 'ofloxacin' keeps its
    first two letters."""
    assert remove_directionality_terms("ofloxacin resistance") == "ofloxacin resistance"
    assert remove_directionality_terms("Intestinal barrier") == "Intestinal barrier"


def test_a_bare_connector_is_left_alone():
    """Only a connector with something after it is dropped — stripping the
    whole string would leave nothing to embed."""
    assert remove_directionality_terms("of") == "of"


@pytest.mark.parametrize("value", ["", "N/A", "   "])
def test_degenerate_input_is_returned_unchanged(value):
    """The function falls back to the original when cleaning empties it, and
    that behaviour must survive the tidy step."""
    result = remove_directionality_terms(value)
    assert result == value.strip() or result == value


def test_the_datasets_own_control_pair_now_agrees():
    """KE 89 and KE 458 describe the same biology and differ only in whether
    the acronym is spelled out. Neither should carry a separator afterwards —
    the remaining difference between them is the acronym itself, which is what
    the rest of #236 is about."""
    spelled = remove_directionality_terms("Synthesis, De Novo Fatty Acid (FA)")
    acronym = remove_directionality_terms("Increased, De Novo FA synthesis")
    for value in (spelled, acronym):
        assert not value.startswith(",")
        assert value == value.strip()
