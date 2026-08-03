"""Which embedding corpus produced a suggestion score.

`suggestion_score` is stored on every mapping and is treated as review evidence
— #236 argues that a reviewer who cannot reproduce a curator's ranking cannot
use the score to judge the mapping. But a score is only meaningful against the
corpus that produced it: regenerating the embeddings moves every score, and
until now nothing recorded which corpus a stored score came from. A reviewer
comparing a May score against an August ranking had no way to tell a genuine
disagreement from a corpus rebuild.

This module reads `data/corpus_manifest.json`, written by
`scripts/embedding_utils.record_corpus_build()` whenever an artifact is
rebuilt, and turns it into a short identifier to stamp on the row:

    wp:2026-08-03        WikiPathways title embeddings, built that day
    go:2026-07-11
    reactome:2026-06-02

The stamp is a *date*, not a timestamp, on purpose: it identifies a corpus, and
two mappings scored against the same corpus must carry the same value.
"""

import json
import logging
import os
from typing import Dict, Optional

from src import PROJECT_ROOT

logger = logging.getLogger(__name__)

MANIFEST_PATH = os.path.join(PROJECT_ROOT, 'data', 'corpus_manifest.json')

# Which artifact identifies each resource's suggestion corpus. These are the
# files whose contents decide a suggestion score, so a rebuild of one of them
# is what invalidates a stored score for that resource.
RESOURCE_ARTIFACTS = {
    'wp': 'pathway_title_embeddings.npz',
    'go': 'go_bp_embeddings.npz',
    'reactome': 'reactome_pathway_embeddings.npz',
}


def _load_manifest() -> Dict[str, dict]:
    if not os.path.exists(MANIFEST_PATH):
        return {}
    try:
        with open(MANIFEST_PATH, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception as e:
        logger.warning("Could not read corpus manifest: %s", e)
        return {}


def corpus_version(resource: str) -> Optional[str]:
    """Return e.g. ``"wp:2026-08-03"``, or None when it cannot be determined.

    None rather than a placeholder: a wrong provenance stamp is worse than an
    absent one, because an absent one is visibly absent. A deployment whose
    artifacts predate the manifest simply records nothing, exactly as it did
    before, and starts recording once the corpus is next rebuilt.
    """
    artifact = RESOURCE_ARTIFACTS.get(resource)
    if not artifact:
        return None
    entry = _load_manifest().get(artifact)
    if not entry or not entry.get('built_on'):
        return None
    return f"{resource}:{entry['built_on']}"


def all_corpus_versions() -> Dict[str, Optional[str]]:
    """Every resource's corpus version, for /health and the admin view."""
    return {resource: corpus_version(resource) for resource in RESOURCE_ARTIFACTS}
