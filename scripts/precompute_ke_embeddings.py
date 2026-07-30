"""
Pre-compute BioBERT embeddings for all Key Events from AOP-Wiki

Usage:
    python scripts/precompute_ke_embeddings.py

Output:
    ke_embeddings.npz - NPZ file with 'ids' (Unicode) and 'matrix' (float32, normalized)
"""

import argparse
import json
import os
import re
import logging
import requests

from embedding_utils import setup_project_path, init_embedding_service, compute_embeddings_batch, save_embeddings, save_metadata

setup_project_path()

from src.utils.text import remove_directionality_terms

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AOP-Wiki SPARQL endpoint
AOPWIKI_SPARQL_ENDPOINT = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"


def fetch_all_kes():
    """Fetch all Key Events from AOP-Wiki SPARQL endpoint"""
    sparql_query = """
    PREFIX aopo: <http://aopkb.org/aop_ontology#>
    PREFIX dc: <http://purl.org/dc/elements/1.1/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX foaf: <http://xmlns.com/foaf/0.1/>
    PREFIX nci: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>

    SELECT DISTINCT ?KElabel ?KEtitle ?KEdescription ?biolevel ?KEpage
    WHERE {
      ?KE a aopo:KeyEvent ;
          dc:title ?KEtitle ;
          rdfs:label ?KElabel ;
          foaf:page ?KEpage .
      OPTIONAL { ?KE dc:description ?KEdescription }
      OPTIONAL { ?KE nci:C25664 ?biolevel }
    }
    """

    try:
        response = requests.post(
            AOPWIKI_SPARQL_ENDPOINT,
            data={'query': sparql_query},
            headers={'Accept': 'application/sparql-results+json'},
            timeout=60
        )
        response.raise_for_status()

        results = response.json()['results']['bindings']

        # Extract KE data
        kes = []
        for result in results:
            # Extract KE ID from label
            ke_label = result['KElabel']['value']
            match = re.search(r'(\d+)$', ke_label)
            ke_id = f"KE {match.group(1)}" if match else ke_label

            ke_data = {
                'ke_id': ke_id,
                'ke_title': result['KEtitle']['value'],
                'ke_description': result.get('KEdescription', {}).get('value', ''),
                'biolevel': result.get('biolevel', {}).get('value', ''),
                'ke_page': result.get('KEpage', {}).get('value', '')
            }
            kes.append(ke_data)

        logger.info(f"Fetched {len(kes)} Key Events from AOP-Wiki")
        return kes

    except Exception as e:
        logger.error(f"Failed to fetch KEs from AOP-Wiki: {e}")
        raise


def load_kes_from_metadata(metadata_path):
    """Rebuild the KE list from an existing ke_metadata.json.

    Lets the embeddings be regenerated without re-fetching AOP-Wiki, so a
    corpus rebuild does not silently move the Key Event snapshot underneath a
    curation session (#225). The metadata file carries every field the
    embedding text is built from.
    """
    with open(metadata_path, 'r', encoding='utf-8') as fh:
        metadata = json.load(fh)

    return [
        {
            'ke_id': entry.get('KElabel'),
            'ke_title': entry.get('KEtitle', ''),
            'ke_description': entry.get('KEdescription', '') or '',
            'biolevel': entry.get('biolevel', ''),
            'ke_page': entry.get('KEpage', ''),
        }
        for entry in metadata
        if entry.get('KElabel')
    ]


def precompute_all_ke_embeddings(output_path='data/ke_embeddings.npz',
                                  metadata_path='data/ke_metadata.json',
                                  refresh_metadata=False):
    """
    Pre-compute BioBERT embeddings for every Key Event.

    Generates three NPZ files:
    - data/ke_embeddings_title_only.npz  (title-only embeddings)
    - data/ke_embeddings_with_desc.npz   (title+description embeddings)
    - data/ke_embeddings.npz             (backward-compat copy of with_desc)

    By default the Key Events are read from the existing ``metadata_path``, so
    regenerating embeddings is a pure recompute. Pass ``refresh_metadata=True``
    (CLI: ``--refresh-metadata``) to re-fetch from AOP-Wiki SPARQL and rewrite
    ``ke_metadata.json`` as well.

    That default is the point of the flag. This script used to always re-fetch
    and rewrite the metadata, so "regenerate the embeddings" also advanced the
    Key Event snapshot the curation UI serves — a side effect nobody asked for
    and which #225 flagged. Moving the snapshot is a deliberate act now.
    """
    import shutil

    embedding_service = init_embedding_service()

    if refresh_metadata or not os.path.exists(metadata_path):
        if refresh_metadata:
            logger.info("Refreshing KE metadata from AOP-Wiki (--refresh-metadata)...")
        else:
            logger.info("No %s found — fetching from AOP-Wiki to create it.", metadata_path)
        kes = fetch_all_kes()
        metadata = [
            {
                'KElabel': ke['ke_id'],
                'KEtitle': ke['ke_title'],
                'KEdescription': ke['ke_description'],
                'biolevel': ke['biolevel'],
                'KEpage': ke['ke_page'],
            }
            for ke in kes
        ]
        save_metadata(metadata, metadata_path)
    else:
        kes = load_kes_from_metadata(metadata_path)
        logger.info(
            "Reusing the existing KE snapshot in %s (%d Key Events) — pass "
            "--refresh-metadata to re-fetch from AOP-Wiki.", metadata_path, len(kes)
        )

    # Build two dicts: title-only and title+description
    title_only_items = {}
    with_desc_items = {}
    desc_count = 0

    for ke in kes:
        ke_title_clean = remove_directionality_terms(ke['ke_title'])

        # Title-only: always just the cleaned title
        title_only_items[ke['ke_id']] = ke_title_clean

        # With-description: concatenate description if available
        if ke['ke_description']:
            with_desc_items[ke['ke_id']] = f"{ke_title_clean}. {ke['ke_description']}"
            desc_count += 1
        else:
            with_desc_items[ke['ke_id']] = ke_title_clean

    logger.info(f"KE description stats: {desc_count}/{len(kes)} KEs have descriptions, "
                f"{len(kes) - desc_count} are title-only")

    # Compute and save title-only embeddings
    title_only_path = 'data/ke_embeddings_title_only.npz'
    logger.info("Computing title-only KE embeddings...")
    title_only_embeddings = compute_embeddings_batch(
        embedding_service, title_only_items, label="Key Events (title-only)"
    )
    save_embeddings(title_only_embeddings, title_only_path)

    # Compute and save title+description embeddings
    with_desc_path = 'data/ke_embeddings_with_desc.npz'
    logger.info("Computing title+description KE embeddings...")
    with_desc_embeddings = compute_embeddings_batch(
        embedding_service, with_desc_items, label="Key Events (with description)"
    )
    save_embeddings(with_desc_embeddings, with_desc_path)

    # Backward-compatible copy: ke_embeddings.npz = with_desc
    shutil.copy2(with_desc_path, output_path)
    logger.info(f"Copied {with_desc_path} -> {output_path} (backward compatibility)")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Pre-compute BioBERT embeddings for AOP-Wiki Key Events."
    )
    parser.add_argument(
        '--refresh-metadata',
        action='store_true',
        help=(
            "Re-fetch Key Events from AOP-Wiki and rewrite ke_metadata.json. "
            "Off by default: regenerating embeddings should not move the Key "
            "Event snapshot the curation UI serves."
        ),
    )
    args = parser.parse_args()
    precompute_all_ke_embeddings(refresh_metadata=args.refresh_metadata)
