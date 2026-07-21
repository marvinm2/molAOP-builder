"""Benchmark for compute_ke_pathways_batch_similarity() — prints avg seconds per call."""
import logging
logging.basicConfig(level=logging.WARNING)

import os
import sys
import json
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.services.embedding import BiologicalEmbeddingService
from src.core.config_loader import ConfigLoader

# --- SETUP (not timed — BioBERT model load ~15s) ---
config = ConfigLoader.load_config()
emb_cfg = config.pathway_suggestion.embedding_based_matching

service = BiologicalEmbeddingService(
    model_name=emb_cfg.model,
    use_gpu=emb_cfg.use_gpu,
    precomputed_embeddings_path=emb_cfg.precomputed_embeddings,
    precomputed_ke_embeddings_path=emb_cfg.precomputed_ke_embeddings,
    score_transform_config={
        'method': emb_cfg.score_transformation.method,
        'power_exponent': emb_cfg.score_transformation.power_exponent,
        'scale_factor': emb_cfg.score_transformation.scale_factor,
        'output_min': emb_cfg.score_transformation.output_min,
        'output_max': emb_cfg.score_transformation.output_max,
        'skip_precomputed_for_titles': emb_cfg.skip_precomputed_for_titles,
    },
    title_weight=emb_cfg.title_weight,
    entity_extract_config={
        'enabled': emb_cfg.entity_extraction.enabled,
        'min_entity_length': emb_cfg.entity_extraction.min_entity_length,
        'include_numbers': emb_cfg.entity_extraction.include_numbers,
        'biological_terms_only': emb_cfg.entity_extraction.biological_terms_only,
    },
)

# Load pathway data — use 50 pathways for realistic batch size
metadata_path = os.path.join(project_root, 'data', 'pathway_metadata.json')
with open(metadata_path, 'r') as f:
    all_pathways = json.load(f)
test_pathways = all_pathways[:50]

ke_id = "KE 55"
ke_title = "Increase, Reactive Oxygen Species production"
ke_description = (
    "Reactive oxygen species (ROS) are chemically reactive molecules "
    "containing oxygen. They include superoxide, hydrogen peroxide, "
    "and hydroxyl radicals."
)

# Warm-up: populate caches
service.compute_ke_pathways_batch_similarity(
    ke_id, ke_title, ke_description, test_pathways[:5], use_description=True
)

# --- BENCHMARK ---
N = 10
start = time.perf_counter()
for _ in range(N):
    service.compute_ke_pathways_batch_similarity(
        ke_id, ke_title, ke_description, test_pathways, use_description=True
    )
elapsed = time.perf_counter() - start

print(f"{elapsed / N:.6f}")
