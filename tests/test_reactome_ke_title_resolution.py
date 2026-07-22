"""
Regression tests for #209: /suggest_reactome was effectively KE-independent when
the caller omitted the optional `ke_title` query parameter.

The Reactome name channel is the only suggestion channel that has no precomputed
per-KE vector to fall back on — it encodes whatever text it is handed. With no
`ke_title`, that text was the empty string, so every Key Event got the *same*
name-channel vector and the channel's whole weight went to ranking pathways by
their distance from a constant. Live on main-9d7e24a (name_weight 0.85) the
top-5 was byte-identical for all seven mapped Key Events of AOP 472.

These tests use a deterministic stub encoder rather than BioBERT: the defect is
about *which text reaches the encoder*, not about embedding quality.
"""
import numpy as np
import pytest
from unittest.mock import patch

from src.core.config_loader import ConfigLoader
from src.suggestions.reactome import ReactomeSuggestionService


# ---------------------------------------------------------------------------
# Deterministic stub encoder
# ---------------------------------------------------------------------------
#
# Three pathways in a 3-dimensional name space:
#   R-HSA-DNA    -> [1, 0, 0]        specific, matches "DNA damage"
#   R-HSA-INFLAM -> [0, 1, 0]        specific, matches "Inflammation"
#   R-HSA-HUB    -> ~[.58, .58, .58] generic, closest to the empty-string vector
#
# The definition channel is deliberately flat (every pathway scores 1.0), so
# ranking is decided by the name channel alone and the assertions isolate it.

_NAME_VECTORS = {
    'R-HSA-DNA': np.array([1.0, 0.0, 0.0]),
    'R-HSA-INFLAM': np.array([0.0, 1.0, 0.0]),
    'R-HSA-HUB': np.array([1.0, 1.0, 1.0]) / np.sqrt(3),
}
_DEF_VECTOR = np.array([0.0, 0.0, 1.0])

_KE_TITLES = {
    'KE 1194': 'Increase, DNA damage',
    'KE 149': 'Increase, Inflammation',
}


class _StubEmbeddingService:
    """Mimics BiologicalEmbeddingService's two lookup regimes.

    - use_description=True  -> precomputed vector keyed by ke_id (KE-conditioned)
    - use_description=False -> live encode() of the text it is handed, which is
      exactly the path that produced a constant vector for a blank title.
    """

    def __init__(self):
        self.encoded_texts = []

    def encode(self, text):
        self.encoded_texts.append(text)
        stripped = (text or '').strip(' ,;:-').lower()
        if 'dna' in stripped:
            return _NAME_VECTORS['R-HSA-DNA']
        if 'inflammation' in stripped:
            return _NAME_VECTORS['R-HSA-INFLAM']
        # No usable text: a uniform vector, nearest to the generic hub pathway.
        return _NAME_VECTORS['R-HSA-HUB']

    def get_ke_embedding_for_matching(self, ke_id, ke_text, use_description=True):
        if use_description:
            return _DEF_VECTOR
        return self.encode(ke_text)

    def _transform_similarity_batch(self, sims):
        return np.asarray(sims, dtype=float)


def _make_svc(ke_metadata_index=None, name_weight=0.5):
    cfg = ConfigLoader.load_config()
    cfg.reactome_suggestion.name_weight = name_weight
    cfg.reactome_suggestion.min_threshold = 0.0
    cfg.reactome_suggestion.embedding_min_threshold = -1.0
    cfg.reactome_suggestion.max_results = 10

    with patch.object(ReactomeSuggestionService, '_load_npz_into', return_value=None), \
            patch.object(ReactomeSuggestionService, '_load_json_into', return_value=None):
        svc = ReactomeSuggestionService(
            config=cfg,
            embedding_service=_StubEmbeddingService(),
            cache_model=None,
        )

    # Set post-construction rather than via the constructor so these tests
    # exercise the *ranking* behaviour on any revision of the service, instead
    # of failing on an unknown keyword argument.
    svc._ke_metadata_index = ke_metadata_index

    svc.reactome_name_embeddings = dict(_NAME_VECTORS)
    svc.reactome_embeddings = {rid: _DEF_VECTOR for rid in _NAME_VECTORS}
    svc.reactome_metadata = {rid: {'name': rid} for rid in _NAME_VECTORS}
    svc.reactome_gene_annotations = {}
    svc._get_genes_from_ke = lambda ke_id: []
    return svc


def _metadata_index():
    return {
        ke_id: {'KElabel': ke_id, 'KEtitle': title}
        for ke_id, title in _KE_TITLES.items()
    }


def _top_id(svc, ke_id, ke_title=''):
    scored = svc._compute_embedding_scores(ke_id, ke_title)
    scored.sort(key=lambda x: x['hybrid_score'], reverse=True)
    return scored[0]['reactome_id']


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKeTitleResolution:
    """resolve_ke_title() precedence rules."""

    def test_supplied_title_wins(self):
        svc = _make_svc(ke_metadata_index=_metadata_index())
        assert svc.resolve_ke_title('KE 1194', 'Caller supplied') == 'Caller supplied'

    def test_blank_title_falls_back_to_metadata(self):
        svc = _make_svc(ke_metadata_index=_metadata_index())
        assert svc.resolve_ke_title('KE 1194', '') == 'Increase, DNA damage'
        assert svc.resolve_ke_title('KE 149', '   ') == 'Increase, Inflammation'

    def test_callable_index_is_accepted(self):
        svc = _make_svc(ke_metadata_index=lambda: _metadata_index())
        assert svc.resolve_ke_title('KE 149', '') == 'Increase, Inflammation'

    def test_unknown_ke_yields_empty_string(self):
        svc = _make_svc(ke_metadata_index=_metadata_index())
        assert svc.resolve_ke_title('KE 999999', '') == ''

    def test_constructor_accepts_the_index(self):
        """The container passes the index as a constructor argument."""
        with patch.object(ReactomeSuggestionService, '_load_npz_into', return_value=None), \
                patch.object(ReactomeSuggestionService, '_load_json_into', return_value=None):
            svc = ReactomeSuggestionService(
                config=ConfigLoader.load_config(),
                ke_metadata_index=_metadata_index(),
            )
        assert svc.resolve_ke_title('KE 149', '') == 'Increase, Inflammation'

    def test_no_index_yields_empty_string(self):
        svc = _make_svc(ke_metadata_index=None)
        assert svc.resolve_ke_title('KE 1194', '') == ''


class TestSuggestionsAreKeDiscriminative:
    """The core #209 regression: no ke_title must not collapse the ranking."""

    def test_different_kes_get_different_top_hit_without_ke_title(self):
        """Fails before the fix: both Key Events return the generic hub pathway."""
        svc = _make_svc(ke_metadata_index=_metadata_index())

        top_dna = _top_id(svc, 'KE 1194')
        top_inflam = _top_id(svc, 'KE 149')

        assert top_dna == 'R-HSA-DNA', (
            f"KE 1194 (DNA damage) should rank the DNA pathway first, got {top_dna}"
        )
        assert top_inflam == 'R-HSA-INFLAM', (
            f"KE 149 (Inflammation) should rank the inflammation pathway first, "
            f"got {top_inflam}"
        )
        assert top_dna != top_inflam

    def test_name_channel_never_encodes_blank_text(self):
        """The name channel must not be handed an empty string to encode."""
        svc = _make_svc(ke_metadata_index=_metadata_index())
        svc._compute_embedding_scores('KE 1194', '')

        encoded = svc.embedding_service.encoded_texts
        assert encoded, "Expected the name channel to encode some Key Event text"
        assert all(t.strip(' ,;:-') for t in encoded), (
            f"Name channel encoded blank text: {encoded!r}"
        )

    def test_payload_reports_the_resolved_title(self):
        svc = _make_svc(ke_metadata_index=_metadata_index())
        result = svc.get_reactome_suggestions('KE 149', '', limit=5)
        assert result['ke_title'] == 'Increase, Inflammation'

    def test_supplied_title_still_drives_ranking(self):
        """An explicit ke_title (the UI path) keeps working unchanged."""
        svc = _make_svc(ke_metadata_index=None)
        assert _top_id(svc, 'KE 1194', 'Increase, DNA damage') == 'R-HSA-DNA'
        assert _top_id(svc, 'KE 149', 'Increase, Inflammation') == 'R-HSA-INFLAM'


class TestNameChannelDisabledWithoutTitle:
    """Safety net: with no title from any source, do not rank on a constant."""

    def test_name_channel_is_skipped_when_no_title_is_resolvable(self):
        svc = _make_svc(ke_metadata_index=None)
        scored = svc._compute_embedding_scores('KE 999999', '')

        assert scored, "Expected description-channel suggestions to still be returned"
        assert svc.embedding_service.encoded_texts == [], (
            "Name channel should be skipped entirely, not encode a blank string"
        )
        assert all('name_similarity' not in item for item in scored), (
            "No name_similarity should be reported when the name channel is off"
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
