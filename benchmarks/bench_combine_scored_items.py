"""Benchmark for combine_scored_items() — prints avg seconds per call."""
import logging
logging.basicConfig(level=logging.WARNING)

import os
import sys
import time
import random

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.suggestions.scoring import combine_scored_items

# --- SETUP (deterministic, not timed) ---
random.seed(42)

pathway_ids = [f"WP{i}" for i in range(1, 301)]


def make_signal(ids_subset):
    return [
        {
            "pathwayID": pid,
            "confidence_score": random.uniform(0.1, 0.95),
            "pathwayTitle": f"Pathway {pid}",
            "pathwayLink": f"https://wikipathways.org/pathways/{pid}",
            "pathwayDescription": "A biological pathway involved in cellular signaling",
            "pathwaySvgUrl": f"https://wikipathways.org/{pid}.svg",
        }
        for pid in ids_subset
    ]


gene_items = make_signal(random.sample(pathway_ids, 120))
embedding_items = make_signal(random.sample(pathway_ids, 180))
ontology_items = make_signal(random.sample(pathway_ids, 100))

scored_lists = {"gene": gene_items, "embedding": embedding_items, "ontology": ontology_items}
weights = {"gene": 0.35, "embedding": 0.50, "ontology": 0.15}
score_field_map = {
    "gene": "confidence_score",
    "embedding": "confidence_score",
    "ontology": "confidence_score",
}

# --- BENCHMARK ---
N = 500
start = time.perf_counter()
for _ in range(N):
    combine_scored_items(
        scored_lists, "pathwayID", weights, score_field_map,
        multi_evidence_bonus=0.05, min_threshold=0.15, max_score=0.98,
    )
elapsed = time.perf_counter() - start

print(f"{elapsed / N:.6f}")
