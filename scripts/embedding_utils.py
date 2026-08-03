"""
Shared utilities for pre-compute embedding scripts.

Provides common setup, embedding computation, and save routines
used by precompute_ke_embeddings.py, precompute_pathway_title_embeddings.py,
and precompute_go_embeddings.py.
"""
import json
import os
import sys
import logging

import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)

# Records when each corpus artifact was last built. The suggestion score stored
# on a mapping is only interpretable against the corpus that produced it — a
# rebuild moves every score, and without this there is no way to tell that from
# a scoring bug. Written beside the artifacts on the data mount.
CORPUS_MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'corpus_manifest.json'
)


def _utc_date():
    """Today in UTC as YYYY-MM-DD.

    Deliberately a date, not a timestamp: this is a corpus *identity* that gets
    stamped on curated rows, and a second-resolution value would make two
    mappings scored from the same corpus look like they came from different
    ones.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def record_corpus_build(artifact_path: str, rows: int = None, model: str = None):
    """Note in the manifest that `artifact_path` was just rebuilt.

    Best-effort: a manifest that cannot be written must never fail a rebuild
    that otherwise succeeded. The cost of failing here is a missing provenance
    stamp; the cost of raising is a corpus half-rebuilt.
    """
    key = os.path.basename(artifact_path)
    entry = {'built_on': _utc_date()}
    if rows is not None:
        entry['rows'] = rows
    if model is not None:
        entry['model'] = model

    path = os.path.abspath(CORPUS_MANIFEST_PATH)
    try:
        manifest = {}
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as fh:
                manifest = json.load(fh)
        manifest[key] = entry
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        logger.info("Recorded corpus build: %s -> %s", key, entry['built_on'])
    except Exception as e:
        logger.warning("Could not record corpus build for %s: %s", key, e)


def setup_project_path():
    """Add project root to sys.path so imports like embedding_service work."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def init_embedding_service():
    """
    Initialize BioBERT embedding service with standard config.

    Returns:
        BiologicalEmbeddingService instance
    """
    from src.services.embedding import BiologicalEmbeddingService

    logger.info("Initializing BioBERT service...")
    service = BiologicalEmbeddingService()
    return service


def compute_embeddings_batch(embedding_service, items, label="items"):
    """
    Compute embeddings for a dict of {id: text} with a progress bar.

    Args:
        embedding_service: BiologicalEmbeddingService instance
        items: Dict mapping ID -> text to embed
        label: Description for the progress bar

    Returns:
        Dict mapping ID -> numpy embedding vector
    """
    embeddings = {}
    sample_count = 0

    for item_id, text in tqdm(items.items(), desc=f"Encoding {label}"):
        # Log first 3 samples for verification
        if sample_count < 3:
            logger.info(f"Sample {item_id}: '{text[:80]}{'...' if len(text) > 80 else ''}'")
            sample_count += 1

        emb = embedding_service.encode(text)
        embeddings[item_id] = emb

    logger.info(f"Computed {len(embeddings)} embeddings for {label}")
    return embeddings


def save_embeddings(embeddings: dict, path: str):
    """
    Save embeddings dict as NPZ matrix format with pre-normalized vectors (no pickle).

    Format: two arrays in the .npz file:
      - 'ids': 1D Unicode string array of embedding keys (dtype=str, NOT dtype=object)
      - 'matrix': 2D float32 array of shape (N, embedding_dim), each row unit-normalized

    Pre-normalization means dot product == cosine similarity at query time (no per-query
    norm computation needed). Using dtype=str for ids avoids pickle requirement on load.

    Args:
        embeddings: Dict mapping ID string -> numpy embedding vector
        path: Output path (accepts .npy or no extension; always writes .npz)
    """
    if not embeddings:
        logger.warning("save_embeddings called with empty dict, skipping save")
        return

    # Always write to .npz extension regardless of input path
    npz_path = path.replace('.npy', '').replace('.npz', '').rstrip('.')

    # Build ids array with Unicode dtype (NOT dtype=object — that requires pickle on load)
    ids = np.array(list(embeddings.keys()), dtype=str)

    # Build and normalize matrix
    matrix = np.array(list(embeddings.values()), dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # Guard against zero vectors (avoid division by zero)
    norms = np.where(norms == 0.0, 1.0, norms)
    matrix = (matrix / norms).astype(np.float32)

    logger.info("Saving %d normalized embeddings to %s.npz ...", len(embeddings), npz_path)
    actual_path = npz_path + '.npz'
    # Write to a sibling and rename. A rebuild that dies partway through must
    # not leave a truncated corpus behind — that matters far more now the
    # rebuild can run unattended from a cron than it did when a human always
    # ran it and saw the traceback. np.savez appends .npz itself, so the temp
    # stem is named without it and the written file is <stem>.npz.
    tmp_stem = npz_path + '.tmp'
    np.savez(tmp_stem, ids=ids, matrix=matrix)
    os.replace(tmp_stem + '.npz', actual_path)

    file_size_mb = os.path.getsize(actual_path) / 1024 / 1024
    logger.info("Saved: %.2f MB (shape: %s)", file_size_mb, str(matrix.shape))

    record_corpus_build(actual_path, rows=len(embeddings))

    sample_id = next(iter(embeddings))
    logger.info("Sample id: %s, vector norm after normalization: %.6f",
                sample_id, float(np.linalg.norm(matrix[0])))


def subset_embeddings(npz_path: str, keep_ids):
    """
    Subset an embeddings .npz (ids/matrix arrays) in place to keep_ids.

    The retained rows are byte-identical to the original — this is an exact
    subset of a precomputed corpus, NOT a recompute. Vectors stay unit-normalized.

    Args:
        npz_path: Path to the .npz file (overwritten in place).
        keep_ids: Iterable of ID strings to retain.

    Returns:
        (original_count, kept_count)
    """
    keep = set(keep_ids)
    with np.load(npz_path) as data:
        ids = data['ids']
        matrix = data['matrix']
    mask = np.array([str(i) in keep for i in ids], dtype=bool)
    np.savez(npz_path, ids=ids[mask], matrix=matrix[mask])
    logger.info("subset_embeddings %s: %d -> %d rows", npz_path, len(ids), int(mask.sum()))
    return len(ids), int(mask.sum())


def subset_json(json_path: str, keep_ids):
    """
    Subset a top-level-dict JSON file in place, keeping only keys in keep_ids.

    Args:
        json_path: Path to the JSON file (overwritten in place).
        keep_ids: Iterable of key strings to retain.

    Returns:
        (original_count, kept_count)
    """
    keep = set(keep_ids)
    with open(json_path, encoding='utf-8') as f:
        data = json.load(f)
    subset = {k: v for k, v in data.items() if k in keep}
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(subset, f, indent=2)
    logger.info("subset_json %s: %d -> %d entries", json_path, len(data), len(subset))
    return len(data), len(subset)


def save_metadata(metadata, path):
    """
    Save metadata list/dict to JSON file with size reporting.

    Args:
        metadata: List or dict to serialize
        path: Output file path
    """
    logger.info(f"Saving metadata to {path}...")
    # Atomic, for the same reason as save_embeddings. `open(path, 'w')`
    # truncates immediately, so an interrupted write leaves a half-written
    # snapshot — and ke_metadata.json is what the Key Event dropdown is served
    # from, so a truncated one silently removes Key Events from curation.
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2)
    os.replace(tmp, path)

    file_size_mb = os.path.getsize(path) / 1024 / 1024
    count = len(metadata)
    logger.info(f"Saved {count} entries: {file_size_mb:.2f} MB")
    record_corpus_build(path, rows=count)
