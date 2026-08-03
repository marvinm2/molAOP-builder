"""
Pre-compute BioBERT embeddings for pathway titles (title-only) WITH entity extraction

This script generates embeddings for pathway TITLES with entity extraction applied,
removing directionality terms and focusing on biological entities for more specific matching.

Usage:
    python scripts/precompute_pathway_title_embeddings.py

Optional input:
    data/wikipathways_filtered_ids.json - if present (from
    download_wikipathways_annotations.py), the corpus is restricted to those
    [10,500]-gene pathways; otherwise the full WP corpus is embedded.

Output:
    pathway_title_embeddings.npz - NPZ file with 'ids' (Unicode) and 'matrix' (float32, normalized)
"""

import json
import logging
import os

import requests

from embedding_utils import setup_project_path, init_embedding_service, compute_embeddings_batch, save_embeddings, save_metadata

setup_project_path()

from src.utils.text import remove_directionality_terms, extract_entities

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# WikiPathways SPARQL endpoint
WIKIPATHWAYS_SPARQL_ENDPOINT = "https://sparql.wikipathways.org/sparql"

# Gene-set-size filtered pathway-ID list (download_wikipathways_annotations.py)
FILTERED_IDS_PATH = "data/wikipathways_filtered_ids.json"


def load_filtered_pathway_ids(path=FILTERED_IDS_PATH):
    """
    Load the [10,500]-gene filtered pathway-ID set produced by
    download_wikipathways_annotations.py.

    Returns None if the file is absent — the corpus is then embedded unfiltered,
    preserving pre-filter behaviour (graceful degradation).
    """
    if not os.path.exists(path):
        logger.info("No filtered-ID list at %s — embedding the full WP corpus", path)
        return None
    with open(path, encoding="utf-8") as f:
        ids = set(json.load(f))
    logger.info("Loaded %d filtered pathway IDs from %s", len(ids), path)
    return ids


def fetch_all_pathways():
    """Fetch all WikiPathways from SPARQL endpoint"""
    sparql_query = """
    PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
    PREFIX dcterms: <http://purl.org/dc/terms/>
    PREFIX dc: <http://purl.org/dc/elements/1.1/>

    SELECT DISTINCT ?pathway ?pathwayTitle ?pathwayLink ?pathwayDescription
    WHERE {
        ?pathway a wp:Pathway ;
                dc:title ?pathwayTitle ;
                dc:identifier ?pathwayLink ;
                wp:organismName "Homo sapiens" .
        OPTIONAL { ?pathway dcterms:description ?pathwayDescription }
    }
    """

    try:
        response = requests.post(
            WIKIPATHWAYS_SPARQL_ENDPOINT,
            data={'query': sparql_query},
            headers={'Accept': 'application/sparql-results+json'},
            timeout=60
        )
        response.raise_for_status()

        results = response.json()['results']['bindings']

        # Extract pathway data
        pathways = []
        for result in results:
            pathway_uri = result['pathway']['value']
            # Extract pathway ID from URI and remove revision number (e.g., WP5482_r129257 -> WP5482)
            pathway_id_full = pathway_uri.split('/')[-1]
            pathway_id = pathway_id_full.split('_')[0]  # Remove revision number

            pathway_data = {
                'pathwayID': pathway_id,
                'pathwayTitle': result['pathwayTitle']['value'],
                'pathwayLink': result.get('pathwayLink', {}).get('value', ''),
                'pathwayDescription': result.get('pathwayDescription', {}).get('value', '')
            }
            pathways.append(pathway_data)

        logger.info(f"Fetched {len(pathways)} pathways from WikiPathways")
        return pathways

    except Exception as e:
        logger.error(f"Failed to fetch pathways from WikiPathways: {e}")
        raise


def fetch_pathway_ontology_tags():
    """Fetch pathway ontology tags from WikiPathways"""
    sparql_query = """
    PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
    PREFIX dcterms: <http://purl.org/dc/terms/>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

    SELECT DISTINCT ?pathwayID ?termLabel
    WHERE {
        ?pathway a wp:Pathway ;
                 dcterms:identifier ?pathwayID ;
                 wp:organismName "Homo sapiens" ;
                 wp:pathwayOntologyTag ?ontologyTerm .
        ?ontologyTerm rdfs:label ?termLabel .
    }
    """

    try:
        response = requests.post(
            WIKIPATHWAYS_SPARQL_ENDPOINT,
            data={'query': sparql_query},
            headers={'Accept': 'application/sparql-results+json'},
            timeout=90
        )
        response.raise_for_status()

        results = response.json()['results']['bindings']

        # Group tags by pathway ID
        tags_by_pathway = {}
        for result in results:
            pathway_id = result['pathwayID']['value']
            tag_label = result['termLabel']['value']

            if pathway_id not in tags_by_pathway:
                tags_by_pathway[pathway_id] = []
            tags_by_pathway[pathway_id].append(tag_label)

        logger.info(f"Fetched ontology tags for {len(tags_by_pathway)} pathways")
        return tags_by_pathway

    except Exception as e:
        logger.warning(f"Failed to fetch pathway ontology tags: {e}")
        return {}


def fetch_pathway_publications():
    """Fetch pathway publications from WikiPathways"""
    sparql_query = """
    PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
    PREFIX dcterms: <http://purl.org/dc/terms/>

    SELECT DISTINCT ?pathwayID ?pubmed
    WHERE {
        ?pathway a wp:Pathway ;
                 dcterms:identifier ?pathwayID ;
                 wp:organismName "Homo sapiens" .
        ?pathway ?p ?pubref .
        ?pubref ?pp ?pubmed .
        FILTER(CONTAINS(STR(?pubmed), "pubmed"))
    }
    """

    try:
        response = requests.post(
            WIKIPATHWAYS_SPARQL_ENDPOINT,
            data={'query': sparql_query},
            headers={'Accept': 'application/sparql-results+json'},
            timeout=90
        )
        response.raise_for_status()

        results = response.json()['results']['bindings']

        # Group publications by pathway ID
        pubs_by_pathway = {}
        for result in results:
            pathway_id = result['pathwayID']['value']
            pubmed_url = result['pubmed']['value']

            # Extract PMID from URL (e.g., http://www.ncbi.nlm.nih.gov/pubmed/34519429 -> 34519429)
            if 'pubmed/' in pubmed_url:
                pmid = pubmed_url.split('pubmed/')[-1]

                if pathway_id not in pubs_by_pathway:
                    pubs_by_pathway[pathway_id] = []

                # Store as dict with PMID and URL
                pubs_by_pathway[pathway_id].append({
                    'pmid': pmid,
                    'url': f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                })

        logger.info(f"Fetched publications for {len(pubs_by_pathway)} pathways")
        return pubs_by_pathway

    except Exception as e:
        logger.warning(f"Failed to fetch pathway publications: {e}")
        return {}


def precompute_pathway_title_embeddings(output_path='data/pathway_title_embeddings.npz',
                                        metadata_path='data/pathway_metadata.json'):
    """
    Fetch all WikiPathways and pre-compute their title-only BioBERT embeddings.
    Also saves pathway_metadata.json for serving dropdown options without live SPARQL.
    """
    embedding_service = init_embedding_service()

    # Fetch all pathways
    logger.info("Fetching all pathways from WikiPathways...")
    pathways = fetch_all_pathways()

    # Remove duplicates (keep first occurrence of each pathway ID)
    seen_ids = set()
    unique_pathways = []
    for pathway in pathways:
        if pathway['pathwayID'] not in seen_ids:
            unique_pathways.append(pathway)
            seen_ids.add(pathway['pathwayID'])

    logger.info(f"After removing duplicates: {len(unique_pathways)} unique pathways")

    # Mark which pathways the [10,500]-gene filter admits, rather than dropping
    # the rest (#238). The filter's rationale is about what the *ranker* should
    # propose — a gene set outside those bounds is a poor Key Event signature —
    # but writing it into the corpus artifact made it govern the manual dropdown
    # and the search box too, so a curator could not select a small, exactly
    # correct pathway even knowing its ID. WP699 (Aflatoxin B1 metabolism, 7
    # genes) was unselectable for the Key Event it is named after.
    #
    # Metadata now carries every pathway with an `inSuggestionCorpus` flag; the
    # ranker filters on that flag at query time (src/suggestions/pathway.py).
    # Embeddings stay restricted to the admitted set, since only those are ever
    # ranked — so this does not grow the .npz.
    filtered_ids = load_filtered_pathway_ids()
    for pathway in unique_pathways:
        pathway['inSuggestionCorpus'] = (
            True if filtered_ids is None else pathway['pathwayID'] in filtered_ids
        )
    if filtered_ids is not None:
        admitted = sum(1 for p in unique_pathways if p['inSuggestionCorpus'])
        logger.info(
            "Gene-set-size filter: %d of %d pathways are rankable; "
            "all %d remain selectable",
            admitted, len(unique_pathways), len(unique_pathways),
        )

    # Fetch enrichment data (ontology tags and publications)
    logger.info("Fetching pathway ontology tags...")
    ontology_tags = fetch_pathway_ontology_tags()

    logger.info("Fetching pathway publications...")
    publications = fetch_pathway_publications()

    # Merge enrichment data into pathway metadata
    for pathway in unique_pathways:
        pathway_id = pathway['pathwayID']

        # Add ontology tags (default to empty list if none found)
        pathway['ontologyTags'] = ontology_tags.get(pathway_id, [])

        # Add publications (default to empty list if none found)
        pathway['publications'] = publications.get(pathway_id, [])

    logger.info(f"Enriched {len(unique_pathways)} pathways with ontology tags and publications")

    # Save metadata in the format expected by /get_pathway_options
    save_metadata(unique_pathways, metadata_path)

    # Build {id: text} dict with directionality removal + entity extraction.
    # Only the rankable pathways need embedding — the rest are reachable through
    # the dropdown and search, which are text-matched, not embedded (#238).
    items = {}
    for pathway in unique_pathways:
        if not pathway.get('inSuggestionCorpus', True):
            continue
        items[pathway['pathwayID']] = extract_entities(
            remove_directionality_terms(pathway['pathwayTitle'])
        )

    embeddings = compute_embeddings_batch(embedding_service, items, label="pathway titles")
    save_embeddings(embeddings, output_path)


if __name__ == '__main__':
    precompute_pathway_title_embeddings()
