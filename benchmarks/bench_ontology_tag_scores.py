"""Benchmark for _compute_ontology_tag_scores() — prints avg seconds per call."""
import logging
logging.basicConfig(level=logging.WARNING)

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.suggestions.pathway import PathwaySuggestionService
from src.core.config_loader import ConfigLoader

# --- SETUP (not timed) ---
config = ConfigLoader.load_config()
service = PathwaySuggestionService(config=config)

# Pre-warm: load pathway JSON into memory
_ = service._get_all_pathways_for_search()

# Realistic KE titles with varying biological complexity
test_cases = [
    ("Increase, Reactive Oxygen Species production", "KE 55"),
    ("Activation of NF-kB signaling pathway", "KE 100"),
    ("Disruption of mitochondrial membrane potential", "KE 200"),
    ("Decreased glutathione synthesis", "KE 300"),
    ("CYP2E1 enzyme induction and oxidative stress", "KE 400"),
]

# --- BENCHMARK ---
N = 20
start = time.perf_counter()
for _ in range(N):
    for ke_title, ke_id in test_cases:
        service._compute_ontology_tag_scores(ke_title, ke_id, limit=20)
elapsed = time.perf_counter() - start

calls = N * len(test_cases)
print(f"{elapsed / calls:.6f}")
