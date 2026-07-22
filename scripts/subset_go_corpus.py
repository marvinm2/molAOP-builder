"""
Subset the GO BP suggestion corpus to the [MIN_GENES, MAX_GENES] filtered ID list.

Reads data/go_bp_filtered_go_ids.json (produced by precompute_go_hierarchy.py) and
subsets the three embedding-corpus artifacts IN PLACE to that ID set:
  - data/go_bp_embeddings.npz
  - data/go_bp_name_embeddings.npz
  - data/go_bp_metadata.json

Does NOT touch:
  - data/go_bp_gene_annotations.json  (canonical DIRECT-annotation record. Since
    #208 the runtime reads the propagated closure written alongside it by
    precompute_go_hierarchy.py; this file stays direct because IC computation
    and this script both need the unpropagated counts. The
                                       runtime gene-overlap scorer reads it)
  - data/go_bp_hierarchy.json         (keeps the full term set — ancestor IC
                                       lookups need out-of-range terms present)

This is an exact subset of an already-precomputed corpus — the retained BioBERT
vectors are byte-identical, so there is no recompute.

Usage:
    python scripts/subset_go_corpus.py

Prerequisite:
    Run precompute_go_hierarchy.py first (it writes data/go_bp_filtered_go_ids.json),
    and precompute_go_embeddings.py (it writes the embedding/metadata artifacts).
"""
import json
import logging
import os

import numpy as np

from embedding_utils import setup_project_path, subset_embeddings, subset_json

setup_project_path()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

FILTERED_IDS_PATH = 'data/go_bp_filtered_go_ids.json'
EMBEDDINGS_PATH = 'data/go_bp_embeddings.npz'
NAME_EMBEDDINGS_PATH = 'data/go_bp_name_embeddings.npz'
METADATA_PATH = 'data/go_bp_metadata.json'


def main():
    if not os.path.exists(FILTERED_IDS_PATH):
        raise FileNotFoundError(
            f"{FILTERED_IDS_PATH} not found — run precompute_go_hierarchy.py first."
        )
    with open(FILTERED_IDS_PATH, encoding='utf-8') as f:
        keep = set(json.load(f))
    logger.info("Filtered GO ID list: %d terms", len(keep))

    for path in (EMBEDDINGS_PATH, NAME_EMBEDDINGS_PATH, METADATA_PATH):
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"{path} not found — run precompute_go_embeddings.py first."
            )

    subset_embeddings(EMBEDDINGS_PATH, keep)
    subset_embeddings(NAME_EMBEDDINGS_PATH, keep)
    subset_json(METADATA_PATH, keep)

    # Consistency gate — the three corpus artifacts must now hold the SAME ID set.
    with np.load(EMBEDDINGS_PATH) as d:
        emb_ids = set(map(str, d['ids']))
    with np.load(NAME_EMBEDDINGS_PATH) as d:
        name_ids = set(map(str, d['ids']))
    with open(METADATA_PATH, encoding='utf-8') as f:
        meta_ids = set(json.load(f).keys())

    if not (emb_ids == name_ids == meta_ids):
        raise AssertionError(
            f"GO corpus desync: embeddings={len(emb_ids)} "
            f"name_embeddings={len(name_ids)} metadata={len(meta_ids)}"
        )

    # Filtered IDs absent from the embedding corpus (the hierarchy is parsed from
    # go-basic.obo, the embeddings from go.obo — a few BP terms may differ).
    missing = keep - emb_ids
    if missing:
        logger.warning(
            "%d filtered GO IDs are absent from the embedding corpus "
            "(not embeddable, harmless): e.g. %s",
            len(missing), sorted(missing)[:5],
        )

    logger.info(
        "Consistency OK — embeddings, name embeddings, metadata all hold %d terms.",
        len(emb_ids),
    )


if __name__ == '__main__':
    main()
