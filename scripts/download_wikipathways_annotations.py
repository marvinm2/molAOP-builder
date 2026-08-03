"""
Download and process WikiPathways gene annotations for Homo sapiens.

Queries the WikiPathways SPARQL endpoint for every Homo sapiens pathway's gene
membership, filters by gene count (10-500), and saves:

    data/wikipathways_gene_annotations.json  - {pathwayID: [gene_symbols]} (filtered)
    data/wikipathways_filtered_ids.json      - [pathwayID, ...] sorted list

The filtered-ID list is consumed by precompute_pathway_title_embeddings.py to
restrict the suggestion corpus to pathways with a usable gene-set size. Mirrors
scripts/download_reactome_annotations.py.

Usage:
    python scripts/download_wikipathways_annotations.py [--output PATH] [--chunk-size N]

Gene predicate (wp:bdbHgncSymbol via dcterms:isPartOf) matches
src/suggestions/pathway.py::_get_pathway_gene_counts so corpus counts agree with
the runtime.
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

sys.path.insert(0, os.path.abspath('.'))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# WikiPathways SPARQL endpoint
WIKIPATHWAYS_SPARQL_ENDPOINT = "https://sparql.wikipathways.org/sparql"

# Gene count filter bounds.
# <10 genes is too specific to act as a Key Event signature and too small for
# reliable over-representation testing (10 is clusterProfiler's minGSSize
# default); >500 is too non-specific. Mirrors download_reactome_annotations.py.
MIN_GENES = 10
MAX_GENES = 500

# Pathways per chunked gene query. The WikiPathways endpoint is community-hosted
# and times out on large aggregations, so the gene fetch is chunked.
CHUNK_SIZE = 200

# Output file paths
OUTPUT_PATH = "data/wikipathways_gene_annotations.json"
FILTERED_IDS_PATH = "data/wikipathways_filtered_ids.json"
# Gene count for EVERY pathway, including the ones the filter drops (#238).
# The annotations file above holds genes only for pathways inside the bounds,
# so without this there is no way to tell a curator *why* a pathway is not
# suggested — the app can only omit it silently, which is the defect.
GENE_COUNTS_PATH = "data/wikipathways_gene_counts.json"


def _sparql_post(query, timeout=90, retries=2):
    """
    POST a SPARQL query to the WikiPathways endpoint, retrying on timeout / 5xx.

    Returns the parsed `results.bindings` list.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                WIKIPATHWAYS_SPARQL_ENDPOINT,
                data={"query": query},
                headers={
                    "Accept": "application/sparql-results+json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()["results"]["bindings"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < retries:
                logger.warning("SPARQL request failed (%s) — retrying in 30s ...", e)
                time.sleep(30)
            else:
                raise
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if attempt < retries and status in (429, 500, 502, 503, 504):
                logger.warning("SPARQL HTTP %d — retrying in 30s ...", status)
                time.sleep(30)
            else:
                raise


def fetch_all_pathway_ids():
    """Return the sorted list of all Homo sapiens WikiPathways IDs."""
    query = """
    PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
    PREFIX dcterms: <http://purl.org/dc/terms/>

    SELECT DISTINCT ?pathwayID
    WHERE {
        ?pathway a wp:Pathway ;
                 dcterms:identifier ?pathwayID ;
                 wp:organismName "Homo sapiens" .
    }
    """
    bindings = _sparql_post(query, timeout=60)
    ids = sorted({b["pathwayID"]["value"] for b in bindings if b.get("pathwayID")})
    logger.info("Found %d Homo sapiens pathways", len(ids))
    return ids


def fetch_gene_annotations(pathway_ids, chunk_size=CHUNK_SIZE):
    """
    Fetch gene memberships for the given pathways, chunked.

    Returns {pathwayID: sorted_list_of_gene_symbols}. Pathways with no
    wp:bdbHgncSymbol genes simply do not appear in the result.
    """
    annotations = {}
    chunks = [pathway_ids[i: i + chunk_size] for i in range(0, len(pathway_ids), chunk_size)]
    logger.info(
        "Fetching gene annotations in %d chunk(s) of up to %d pathways ...",
        len(chunks), chunk_size,
    )

    for idx, chunk in enumerate(chunks, 1):
        values = " ".join(f'"{pid}"' for pid in chunk)
        query = f"""
        PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
        PREFIX dcterms: <http://purl.org/dc/terms/>

        SELECT DISTINCT ?pathwayID ?geneSymbol
        WHERE {{
            ?pathway a wp:Pathway ;
                     dcterms:identifier ?pathwayID ;
                     wp:organismName "Homo sapiens" .
            ?geneProduct dcterms:isPartOf ?pathway ;
                         wp:bdbHgncSymbol ?geneSymbol .
            VALUES ?pathwayID {{ {values} }}
        }}
        """
        bindings = _sparql_post(query, timeout=90)
        for b in bindings:
            pid = b.get("pathwayID", {}).get("value", "")
            gene = b.get("geneSymbol", {}).get("value", "")
            # wp:bdbHgncSymbol values are identifiers.org URIs
            # (https://identifiers.org/hgnc.symbol/ANPEP) — store the bare symbol,
            # consistent with reactome_gene_annotations.json. Harmless if already bare.
            gene = gene.rstrip("/").rsplit("/", 1)[-1]
            if pid and gene:
                annotations.setdefault(pid, set()).add(gene)
        logger.info("  chunk %d/%d: %d pathways with genes so far", idx, len(chunks), len(annotations))
        time.sleep(0.5)  # polite pacing

    return {pid: sorted(genes) for pid, genes in annotations.items()}


def filter_annotations(raw_annotations, min_genes=MIN_GENES, max_genes=MAX_GENES):
    """Keep only pathways whose gene count is within [min_genes, max_genes]."""
    filtered = {}
    n_low = n_high = 0
    for pid, genes in raw_annotations.items():
        n = len(genes)
        if n < min_genes:
            n_low += 1
        elif n > max_genes:
            n_high += 1
        else:
            filtered[pid] = genes
    logger.info(
        "Filter [%d,%d]: %d kept (from %d with genes) | dropped %d <%d, %d >%d",
        min_genes, max_genes, len(filtered), len(raw_annotations),
        n_low, min_genes, n_high, max_genes,
    )
    return filtered


def _write_json_atomic(obj, path):
    """Write JSON to a .tmp sibling then atomically rename — never corrupt a good file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def download_wikipathways_annotations(output_path=OUTPUT_PATH, chunk_size=CHUNK_SIZE):
    """Main pipeline: fetch IDs -> fetch gene annotations -> filter -> save."""
    pathway_ids = fetch_all_pathway_ids()
    raw = fetch_gene_annotations(pathway_ids, chunk_size=chunk_size)
    logger.info(
        "%d of %d pathways have wp:bdbHgncSymbol genes", len(raw), len(pathway_ids)
    )

    filtered = filter_annotations(raw)

    _write_json_atomic(filtered, output_path)
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info("Saved %d pathways: %.2f MB -> %s", len(filtered), size_mb, output_path)

    filtered_ids = sorted(filtered.keys())
    _write_json_atomic(filtered_ids, FILTERED_IDS_PATH)
    logger.info("Saved %d filtered pathway IDs -> %s", len(filtered_ids), FILTERED_IDS_PATH)

    # Gene counts for every pathway that has any, filtered or not (#238), so
    # the picker can say "7 genes — below the suggestion threshold" instead of
    # dropping the pathway without explanation.
    gene_counts = {pid: len(genes) for pid, genes in raw.items()}
    _write_json_atomic(gene_counts, GENE_COUNTS_PATH)
    logger.info(
        "Saved gene counts for %d pathways (%d outside [%d,%d]) -> %s",
        len(gene_counts), len(gene_counts) - len(filtered),
        MIN_GENES, MAX_GENES, GENE_COUNTS_PATH,
    )

    for pid in filtered_ids[:3]:
        logger.info("Sample %s: %d genes - %s...", pid, len(filtered[pid]), filtered[pid][:5])

    return filtered


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download and filter WikiPathways gene annotations for Homo sapiens"
    )
    parser.add_argument("--output", default=OUTPUT_PATH, help=f"Output JSON path (default: {OUTPUT_PATH})")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE,
                        help=f"Pathways per SPARQL chunk (default: {CHUNK_SIZE})")
    args = parser.parse_args()

    download_wikipathways_annotations(output_path=args.output, chunk_size=args.chunk_size)
