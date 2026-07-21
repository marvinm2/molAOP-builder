"""Benchmark for parse_obo_file() — prints avg seconds per call."""
import logging
logging.basicConfig(level=logging.WARNING)

import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, 'scripts'))

from precompute_go_embeddings import parse_obo_file

# --- SETUP ---
obo_path = os.path.join(project_root, 'data', 'go.obo')
if not os.path.exists(obo_path):
    print("-1")
    sys.exit(1)

# --- BENCHMARK ---
N = 5
start = time.perf_counter()
for _ in range(N):
    parse_obo_file(obo_path, 'biological_process')
elapsed = time.perf_counter() - start

print(f"{elapsed / N:.6f}")
