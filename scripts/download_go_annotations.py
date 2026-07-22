"""
Download and process GO gene annotations for human

Downloads the UniProt-GOA human annotations file, parses GAF format,
filters annotations by namespace (Biological Process or Molecular Function),
and maps gene symbols.

Usage:
    python scripts/download_go_annotations.py [--namespace bp|mf]

    --namespace bp  (default) Biological Process — filters GAF aspect 'P'
    --namespace mf            Molecular Function  — filters GAF aspect 'F'

Output (bp):
    go_bp_gene_annotations.json - {go_id: [gene_symbols]}, DIRECT annotations
        only. Ontology propagation (the GO true-path rule) is applied by
        scripts/precompute_go_hierarchy.py, which writes
        go_bp_gene_annotations_propagated.json — that is what the app reads (#208).

Output (mf):
    go_mf_gene_annotations.json - {go_id: [gene_symbols]}
"""

import argparse
import sys
import os
import json
import gzip
import logging
from urllib.request import urlretrieve
from collections import defaultdict

sys.path.insert(0, os.path.abspath('.'))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# UniProt-GOA human annotations
GOA_URL = "https://ftp.ebi.ac.uk/pub/databases/GO/goa/HUMAN/goa_human.gaf.gz"
GOA_LOCAL = "data/goa_human.gaf.gz"

# GAF aspect code map: CLI namespace arg -> GAF Aspect column value
# P = Biological Process, F = Molecular Function, C = Cellular Component
ASPECT_CODE = {
    'bp': 'P',
    'mf': 'F',
}


def download_goa_file(url=GOA_URL, local_path=GOA_LOCAL):
    """Download GOA file if not already present"""
    if os.path.exists(local_path):
        logger.info(f"Using existing GOA file: {local_path}")
        return local_path

    logger.info(f"Downloading GOA file from {url}...")
    urlretrieve(url, local_path)
    logger.info(f"Downloaded to {local_path}")
    return local_path


def parse_gaf_file(gaf_path, aspect_code='P'):
    """
    Parse GAF (Gene Association Format) file

    GAF format columns:
    0: DB
    1: DB_Object_ID (UniProt ID)
    2: DB_Object_Symbol (Gene symbol)
    3: Qualifier
    4: GO_ID
    5: DB:Reference
    6: Evidence Code
    7: With/From
    8: Aspect (P=Biological Process, F=Molecular Function, C=Cellular Component)
    9: DB_Object_Name
    10: DB_Object_Synonym
    11: DB_Object_Type
    12: Taxon
    13: Date
    14: Assigned_By

    Args:
        gaf_path: Path to the GAF (or .gz) annotation file
        aspect_code: GAF Aspect column filter ('P' for BP, 'F' for MF)

    Returns:
        dict: {go_id: set(gene_symbols)} for the specified aspect only
    """
    logger.info(f"Parsing GAF file: {gaf_path} (aspect: {aspect_code})")

    go_gene_map = defaultdict(set)
    total_lines = 0
    aspect_lines = 0

    opener = gzip.open if gaf_path.endswith('.gz') else open

    with opener(gaf_path, 'rt', encoding='utf-8') as f:
        for line in f:
            # Skip comment lines
            if line.startswith('!'):
                continue

            total_lines += 1
            fields = line.strip().split('\t')

            if len(fields) < 15:
                continue

            aspect = fields[8]
            # Only keep annotations matching the requested aspect
            if aspect != aspect_code:
                continue

            aspect_lines += 1

            go_id = fields[4]
            gene_symbol = fields[2]
            qualifier = fields[3]

            # Skip negative annotations (NOT)
            if 'NOT' in qualifier:
                continue

            if gene_symbol and go_id:
                go_gene_map[go_id].add(gene_symbol)

    logger.info(f"Parsed {total_lines} total lines, {aspect_lines} aspect={aspect_code} annotations")
    logger.info(f"Found {len(go_gene_map)} unique GO terms with gene annotations")

    return go_gene_map


def download_go_annotations(namespace='bp', output_path=None):
    """
    Download and process GO gene annotations for human.

    Args:
        namespace: 'bp' (Biological Process, default) or 'mf' (Molecular Function)
        output_path: Override output file path
    """
    aspect_code = ASPECT_CODE[namespace]
    if output_path is None:
        output_path = f'data/go_{namespace}_gene_annotations.json'

    logger.info(f"Downloading GO {namespace.upper()} gene annotations (aspect={aspect_code})")

    # Download GOA file
    gaf_path = download_goa_file()

    # Parse GAF file
    go_gene_map = parse_gaf_file(gaf_path, aspect_code=aspect_code)

    # Convert sets to sorted lists for JSON serialization
    go_gene_annotations = {}
    for go_id, genes in go_gene_map.items():
        go_gene_annotations[go_id] = sorted(list(genes))

    # Save to JSON
    logger.info(f"Saving gene annotations to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(go_gene_annotations, f, indent=2)

    file_size = os.path.getsize(output_path) / 1024 / 1024
    logger.info(f"Gene annotations saved: {file_size:.2f} MB")

    # Print statistics
    total_genes = set()
    for genes in go_gene_annotations.values():
        total_genes.update(genes)

    logger.info(f"GO {namespace.upper()} terms with annotations: {len(go_gene_annotations)}")
    logger.info(f"Unique gene symbols: {len(total_genes)}")

    # Print sample
    sample_ids = list(go_gene_annotations.keys())[:3]
    for go_id in sample_ids:
        genes = go_gene_annotations[go_id]
        logger.info(f"Sample {go_id}: {len(genes)} genes - {genes[:5]}...")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Download and process GO gene annotations for human'
    )
    parser.add_argument(
        '--namespace', choices=['bp', 'mf'], default='bp',
        help='GO namespace to download: bp (Biological Process, default) or mf (Molecular Function)'
    )
    args = parser.parse_args()
    download_go_annotations(namespace=args.namespace)
