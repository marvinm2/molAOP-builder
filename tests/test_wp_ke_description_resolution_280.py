"""
Regression tests for #280: WikiPathways suggestions dropped the Key Event
description on a cache miss.

get_pathway_suggestions() hardcoded ``ke_description = ""``, so the description
reached the model only through the precomputed title+description KE vector. A
Key Event absent from that artifact (the newest ones, after a metadata refresh)
fell back to encoding the title alone while the description toggle reported
"on". The description is now resolved server-side from KE metadata, the same
way the Reactome suggester resolves a missing title (#209).
"""
from unittest.mock import patch

import numpy as np
import pytest

from src.core.config_loader import ConfigLoader
from src.services.embedding import BiologicalEmbeddingService
from src.suggestions.pathway import PathwaySuggestionService

KE_ID = 'KE 2400'
KE_TITLE = 'Increase, Mitochondrial dysfunction'
KE_DESC = 'Loss of mitochondrial membrane potential and impaired ATP synthesis.'


def _metadata_index():
    return {
        KE_ID: {'KElabel': KE_ID, 'KEtitle': KE_TITLE, 'KEdescription': KE_DESC},
        'KE 1': {'KElabel': 'KE 1', 'KEtitle': 'No description', 'KEdescription': ''},
    }


class _RecordingEmbeddingService:
    """Records what the pathway suggester hands the batch similarity call."""

    def __init__(self):
        self.calls = []

    def compute_ke_pathways_batch_similarity(self, **kwargs):
        self.calls.append(kwargs)
        return []


class _DisabledOverrides:
    def __init__(self, ke_ids):
        self._ke_ids = set(ke_ids)

    def get_disabled_ke_ids(self):
        return self._ke_ids


def _make_svc(embedding_service, ke_metadata_index=None, ke_override_model=None):
    svc = PathwaySuggestionService(
        config=ConfigLoader.load_config(),
        embedding_service=embedding_service,
        ke_override_model=ke_override_model,
    )
    # Set post-construction so the behavioural tests run on any revision of
    # the service rather than failing on an unknown keyword argument.
    svc._ke_metadata_index = ke_metadata_index
    svc._get_rankable_pathways = lambda: [
        {'pathwayID': 'WP1', 'pathwayTitle': 'Mitochondrial ATP synthesis'}
    ]
    svc._get_genes_from_ke = lambda ke_id: []
    svc._compute_ontology_tag_scores = lambda *a, **k: []
    return svc


class TestDescriptionReachesTheEmbeddingCall:

    def test_description_resolved_from_metadata(self):
        """Fails before the fix: the suggester always passed an empty string."""
        emb = _RecordingEmbeddingService()
        svc = _make_svc(emb, ke_metadata_index=_metadata_index())

        svc.get_pathway_suggestions(KE_ID, KE_TITLE, limit=5)

        assert emb.calls, "embedding suggestions were not computed"
        assert emb.calls[0]['ke_description'] == KE_DESC

    def test_callable_index_is_accepted(self):
        emb = _RecordingEmbeddingService()
        svc = _make_svc(emb, ke_metadata_index=lambda: _metadata_index())

        svc.get_pathway_suggestions(KE_ID, KE_TITLE, limit=5)

        assert emb.calls[0]['ke_description'] == KE_DESC

    def test_supplied_description_is_not_replaced(self):
        """A caller-supplied description passes through untouched, so scores
        computed from it do not move."""
        emb = _RecordingEmbeddingService()
        svc = _make_svc(emb, ke_metadata_index=_metadata_index())

        svc._get_embedding_based_suggestions(KE_ID, KE_TITLE, 'Caller text', None, 5)

        assert emb.calls[0]['ke_description'] == 'Caller text'

    def test_per_ke_toggle_off_does_not_resolve(self):
        """A KE whose description an admin switched off stays title-only."""
        emb = _RecordingEmbeddingService()
        svc = _make_svc(
            emb,
            ke_metadata_index=_metadata_index(),
            ke_override_model=_DisabledOverrides({KE_ID}),
        )

        svc.get_pathway_suggestions(KE_ID, KE_TITLE, limit=5)

        assert emb.calls[0]['use_description'] is False
        assert emb.calls[0]['ke_description'] == ''

    def test_global_toggle_off_does_not_resolve(self):
        """With the description switched off globally, a cache-miss encode
        must stay title-only even though metadata has a description."""
        emb = _RecordingEmbeddingService()
        svc = _make_svc(emb, ke_metadata_index=_metadata_index())
        matching = svc.config.pathway_suggestion.embedding_based_matching
        matching.use_ke_description = False

        svc.get_pathway_suggestions(KE_ID, KE_TITLE, limit=5)

        assert emb.calls[0]['use_description'] is False
        assert emb.calls[0]['ke_description'] == ''

    @pytest.mark.parametrize('index', [None, {}, _metadata_index()])
    def test_missing_description_yields_empty_string(self, index):
        emb = _RecordingEmbeddingService()
        svc = _make_svc(emb, ke_metadata_index=index)

        svc.get_pathway_suggestions('KE 1', 'No description', limit=5)
        svc.get_pathway_suggestions('KE 999999', 'Unknown', limit=5)

        assert [c['ke_description'] for c in emb.calls] == ['', '']


class TestCacheMissEncodesTheDescription:
    """End to end through the real batch similarity code with BioBERT stubbed."""

    @pytest.fixture
    def embedding_service(self):
        svc = BiologicalEmbeddingService.__new__(BiologicalEmbeddingService)
        svc.embeddings_degraded = []
        svc.ke_embeddings = {}
        svc.ke_embeddings_with_desc = {}  # KE_ID absent: the #225 drift case
        svc.ke_embeddings_title_only = {}
        svc.pathway_embeddings = {}
        svc.pathway_title_embeddings = {}
        svc.score_transform_config = {'skip_precomputed_for_titles': True}
        svc.title_weight = 0.5
        svc.desc_weight = 0.5
        svc._extract_entities = lambda text: text
        svc._transform_similarity_batch = lambda sims: np.asarray(sims, dtype=float)
        svc.encoded = []

        def fake_encode(text):
            svc.encoded.append(text)
            return np.array([1.0, 0.0, 0.0])

        svc.encode = fake_encode
        return svc

    def test_cache_miss_encodes_title_and_description(self, embedding_service):
        """Fails before the fix: only the title was encoded on a miss."""
        svc = _make_svc(embedding_service, ke_metadata_index=_metadata_index())

        svc.get_pathway_suggestions(KE_ID, KE_TITLE, limit=5)

        assert any(KE_DESC in text for text in embedding_service.encoded), (
            f"description never reached the encoder: {embedding_service.encoded!r}"
        )

    def test_cache_hit_scores_unchanged(self, embedding_service):
        """A KE with a precomputed title+description vector scores the same
        with or without the metadata lookup, and the description never
        reaches the encoder."""
        embedding_service.ke_embeddings_with_desc = {
            KE_ID: np.array([0.6, 0.8, 0.0])
        }

        def run(index):
            embedding_service.encoded = []
            svc = _make_svc(embedding_service, ke_metadata_index=index)
            results = svc.get_pathway_suggestions(KE_ID, KE_TITLE, limit=5)
            assert not any(KE_DESC in t for t in embedding_service.encoded)
            return results

        with_lookup = run(_metadata_index())
        without_lookup = run(None)

        def sims(results):
            return [
                (r['pathwayID'], r['embedding_similarity'],
                 r['title_similarity'], r['description_similarity'],
                 r['confidence_score'])
                for r in results['embedding_based_suggestions']
            ]

        assert sims(with_lookup), "no embedding suggestions were produced"
        assert sims(with_lookup) == sims(without_lookup)


class TestWiring:

    def test_constructor_accepts_the_index(self):
        svc = PathwaySuggestionService(
            config=ConfigLoader.load_config(),
            ke_metadata_index=_metadata_index(),
        )
        assert svc.resolve_ke_description(KE_ID, '') == KE_DESC
        assert svc.resolve_ke_description(KE_ID, 'Given') == 'Given'

    def test_container_passes_the_metadata_index(self):
        from src.services.container import ServiceContainer

        container = ServiceContainer.__new__(ServiceContainer)
        container._pathway_suggestion_service = None
        container._scoring_config = ConfigLoader.load_config()
        container._cache_model = object()
        container._embedding_service = None
        container._ke_override_model = _DisabledOverrides(set())
        container._ke_metadata = list(_metadata_index().values())
        container._ke_metadata_index = None

        with patch.object(
            type(container), 'embedding_service', property(lambda self: None)
        ):
            svc = container.pathway_suggestion_service

        assert svc.resolve_ke_description(KE_ID, '') == KE_DESC
