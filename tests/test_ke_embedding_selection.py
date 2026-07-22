"""
Regression tests for get_ke_embedding_for_matching() vector selection (#209).

The defect these pin down: data/ke_embeddings_title_only.npz was never actually
generated (scripts/precompute_ke_embeddings.py copies the with-description set),
so a `use_description=False` request fell through to get_ke_embedding(), which
returns a title+description vector. The Reactome name channel therefore spent
85% of its ranking weight comparing short pathway *names* against KE
title+description vectors, and nothing in the code or the tests noticed.

The contract asserted here: a title-only request must never return a
with-description vector. When the precomputed title-only set lacks the KE, the
service encodes the supplied text live instead — the same thing the
WikiPathways path has always done.
"""
import numpy as np
import pytest

from src.services.embedding import BiologicalEmbeddingService


TITLE_ONLY = np.array([1.0, 0.0, 0.0], dtype=np.float32)
WITH_DESC = np.array([0.0, 1.0, 0.0], dtype=np.float32)
LEGACY = np.array([0.0, 0.0, 1.0], dtype=np.float32)
ENCODED = np.array([0.5, 0.5, 0.5], dtype=np.float32)


@pytest.fixture
def service():
    """A bare service with the embedding dicts stubbed and no BioBERT loaded.

    __init__ constructs a SentenceTransformer, which we neither want nor need
    here, so build the instance without running it and populate only the
    attributes the method under test reads.
    """
    svc = BiologicalEmbeddingService.__new__(BiologicalEmbeddingService)
    svc.embeddings_degraded = []
    svc.ke_embeddings_title_only = {}
    svc.ke_embeddings_with_desc = {}
    svc.ke_embeddings = {}

    calls = []

    def fake_encode(text):
        calls.append(text)
        return ENCODED

    svc.encode = fake_encode
    svc.encode_calls = calls
    return svc


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------

def test_title_only_miss_never_returns_with_description_vector(service):
    """The #209 regression: a title-only request on a KE that is absent from
    the title-only set must not resolve to the with-description vector."""
    service.ke_embeddings_with_desc = {'KE 1115': WITH_DESC}
    service.ke_embeddings = {'KE 1115': LEGACY}
    # title-only set is empty — exactly the deployed state before the fix

    result = service.get_ke_embedding_for_matching(
        'KE 1115', 'Increase, Reactive oxygen species', use_description=False
    )

    assert not np.array_equal(result, WITH_DESC)
    assert not np.array_equal(result, LEGACY)
    assert np.array_equal(result, ENCODED)
    assert service.encode_calls == ['Increase, Reactive oxygen species']


def test_title_only_miss_flags_degradation_once(service):
    """The fallback is recorded so /health can report it, and only once."""
    service.ke_embeddings = {'KE 1': LEGACY, 'KE 2': LEGACY}

    service.get_ke_embedding_for_matching('KE 1', 'first', use_description=False)
    service.get_ke_embedding_for_matching('KE 2', 'second', use_description=False)

    assert service.embeddings_degraded == ['ke_embeddings_title_only']


# ---------------------------------------------------------------------------
# Behaviour that must not regress
# ---------------------------------------------------------------------------

def test_title_only_hit_uses_precomputed_vector(service):
    """When the artifact IS present it stays an optimisation — no encoding."""
    service.ke_embeddings_title_only = {'KE 1115': TITLE_ONLY}
    service.ke_embeddings_with_desc = {'KE 1115': WITH_DESC}

    result = service.get_ke_embedding_for_matching(
        'KE 1115', 'Increase, Reactive oxygen species', use_description=False
    )

    assert np.array_equal(result, TITLE_ONLY)
    assert service.encode_calls == []
    assert service.embeddings_degraded == []


def test_with_description_hit_uses_precomputed_vector(service):
    service.ke_embeddings_title_only = {'KE 1115': TITLE_ONLY}
    service.ke_embeddings_with_desc = {'KE 1115': WITH_DESC}

    result = service.get_ke_embedding_for_matching(
        'KE 1115', 'some text', use_description=True
    )

    assert np.array_equal(result, WITH_DESC)
    assert service.encode_calls == []


def test_with_description_miss_may_use_legacy_set(service):
    """The with-description fallback to ke_embeddings stays intact: that set is
    itself title+description, so it is a valid substitute — unlike the
    title-only direction, where it is precisely the wrong vector."""
    service.ke_embeddings = {'KE 1115': LEGACY}

    result = service.get_ke_embedding_for_matching(
        'KE 1115', 'some text', use_description=True
    )

    assert np.array_equal(result, LEGACY)
    assert service.encode_calls == []
    assert service.embeddings_degraded == []


def test_unknown_ke_encodes_in_both_directions(service):
    """A KE absent from every set encodes live either way."""
    for use_desc in (True, False):
        result = service.get_ke_embedding_for_matching(
            'KE 9999', 'novel key event', use_description=use_desc
        )
        assert np.array_equal(result, ENCODED)
