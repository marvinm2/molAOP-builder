"""
Pre-compute BioBERT embeddings for GO terms (Biological Process or Molecular Function)

Downloads the GO OBO file, parses terms for the selected namespace,
generates BioBERT embeddings, and saves metadata.

Usage:
    python scripts/precompute_go_embeddings.py [--namespace bp|mf]

    --namespace bp  (default) Biological Process — outputs go_bp_* files
    --namespace mf            Molecular Function  — outputs go_mf_* files

Output (bp):
    go_bp_name_embeddings.npz - NPZ file with 'ids' (Unicode) and 'matrix' (float32, normalized)
    go_bp_embeddings.npz - NPZ file with 'ids' (Unicode) and 'matrix' (float32, normalized)
    go_bp_metadata.json - {go_id: {name, definition, is_a[], part_of[]}}

Output (mf):
    go_mf_name_embeddings.npz
    go_mf_embeddings.npz
    go_mf_metadata.json
"""

import argparse
import os
import re
import json
import logging
from urllib.request import urlretrieve

from embedding_utils import setup_project_path, init_embedding_service, compute_embeddings_batch, save_embeddings

setup_project_path()

from src.utils.text import remove_directionality_terms, extract_entities, detect_go_direction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GO_OBO_URL = "http://purl.obolibrary.org/obo/go.obo"
GO_OBO_LOCAL = "data/go.obo"

# Namespace filter map: CLI arg -> OBO namespace value
NAMESPACE_FILTER = {
    'bp': 'biological_process',
    'mf': 'molecular_function',
}


def download_go_obo(url=GO_OBO_URL, local_path=GO_OBO_LOCAL, force=False):
    """Download the GO OBO file if not already present, or always with force=True"""
    if os.path.exists(local_path) and not force:
        logger.info(f"Using existing OBO file: {local_path}")
        return local_path

    logger.info(f"Downloading GO OBO file from {url}...")
    urlretrieve(url, local_path + '.tmp')
    os.replace(local_path + '.tmp', local_path)
    logger.info(f"Downloaded to {local_path}")
    return local_path


def parse_obo_file(obo_path, namespace_value='biological_process'):
    """
    Parse GO OBO file and extract terms for the specified namespace.

    Args:
        obo_path: Path to the OBO file
        namespace_value: OBO namespace string to filter by (e.g. 'biological_process',
                         'molecular_function')

    Returns:
        dict: {go_id: {name, definition, is_a[], part_of[]}}
    """
    logger.info(f"Parsing OBO file: {obo_path} (namespace: {namespace_value})")

    terms = {}
    current_term = None
    in_term = False

    with open(obo_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()

            if line == '[Term]':
                in_term = True
                current_term = {
                    'id': None,
                    'name': None,
                    'namespace': None,
                    'definition': '',
                    'is_a': [],
                    'part_of': [],
                    'synonyms': [],
                    'is_obsolete': False
                }
                continue

            if line == '' or line.startswith('['):
                if in_term and current_term and current_term['id']:
                    # Save term if it matches the target namespace and not obsolete
                    if (current_term['namespace'] == namespace_value
                            and not current_term['is_obsolete']):
                        go_id = current_term['id']
                        go_name = current_term['name']
                        terms[go_id] = {
                            'name': go_name,
                            'definition': current_term['definition'],
                            'is_a': current_term['is_a'],
                            'part_of': current_term['part_of'],
                            'synonyms': current_term['synonyms'],
                            'direction': detect_go_direction(go_name)
                        }
                in_term = False
                current_term = None
                continue

            if not in_term or current_term is None:
                continue

            if line.startswith('id: '):
                current_term['id'] = line[4:]
            elif line.startswith('name: '):
                current_term['name'] = line[6:]
            elif line.startswith('namespace: '):
                current_term['namespace'] = line[11:]
            elif line.startswith('def: '):
                # Extract definition text from quotes
                match = re.match(r'def: "(.+?)"', line)
                if match:
                    current_term['definition'] = match.group(1)
            elif line.startswith('is_a: '):
                # Extract parent GO ID
                parent_id = line[6:].split(' ! ')[0].strip()
                current_term['is_a'].append(parent_id)
            elif line.startswith('relationship: part_of '):
                part_id = line[22:].split(' ! ')[0].strip()
                current_term['part_of'].append(part_id)
            elif line.startswith('synonym: '):
                # synonym: "alt name" EXACT|BROAD|NARROW|RELATED [...]
                m = re.match(r'synonym: "(.+?)"\s+(EXACT|BROAD|NARROW|RELATED)', line)
                if m:
                    current_term['synonyms'].append({
                        'text': m.group(1),
                        'type': m.group(2)
                    })
            elif line == 'is_obsolete: true':
                current_term['is_obsolete'] = True

    logger.info(f"Parsed {len(terms)} {namespace_value} GO terms")
    return terms


def precompute_go_embeddings(
    namespace='bp',
    embeddings_path=None,
    name_embeddings_path=None,
    metadata_path=None,
    force=False
):
    """
    Parse GO OBO file and pre-compute BioBERT embeddings for GO terms.

    Args:
        namespace: 'bp' (Biological Process, default) or 'mf' (Molecular Function)
        embeddings_path: Override output path for combined embeddings
        name_embeddings_path: Override output path for name-only embeddings
        metadata_path: Override output path for metadata JSON

    Produces two embedding files:
    - name_embeddings_path: name-only embeddings (like pathway titles)
    - embeddings_path: combined name+definition embeddings (like pathway descriptions)
    """
    namespace_value = NAMESPACE_FILTER[namespace]
    ns_label = namespace.upper()

    # Default output paths are namespace-aware
    if embeddings_path is None:
        embeddings_path = f'data/go_{namespace}_embeddings.npz'
    if name_embeddings_path is None:
        name_embeddings_path = f'data/go_{namespace}_name_embeddings.npz'
    if metadata_path is None:
        metadata_path = f'data/go_{namespace}_metadata.json'

    logger.info(f"Precomputing GO embeddings for namespace: {namespace_value}")

    # Download OBO file
    obo_path = download_go_obo(force=force)

    # Parse GO terms for the selected namespace
    go_terms = parse_obo_file(obo_path, namespace_value=namespace_value)

    # Save metadata
    logger.info(f"Saving {ns_label} metadata to {metadata_path}...")
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(go_terms, f, indent=2)
    logger.info(f"Metadata saved: {os.path.getsize(metadata_path) / 1024 / 1024:.2f} MB")

    # Initialize embedding service
    embedding_service = init_embedding_service()

    # Build name + EXACT synonyms items {id: cleaned_text}
    name_items = {}
    for go_id, term_data in go_terms.items():
        name = term_data['name']
        exact_syns = [s['text'] for s in term_data.get('synonyms', []) if s['type'] == 'EXACT']
        # Append EXACT synonyms to enrich the name embedding
        text = '. '.join([name] + exact_syns) if exact_syns else name
        name_items[go_id] = extract_entities(remove_directionality_terms(text))

    logger.info(f"Computing {ns_label} name-only embeddings...")
    name_embeddings = compute_embeddings_batch(embedding_service, name_items, label=f"GO {ns_label} names")
    save_embeddings(name_embeddings, name_embeddings_path)

    # Build combined name+definition items {id: cleaned_text}
    combined_items = {}
    for go_id, term_data in go_terms.items():
        name = term_data['name']
        definition = term_data.get('definition', '')
        text = f"{name}. {definition}" if definition else name
        combined_items[go_id] = extract_entities(remove_directionality_terms(text))

    logger.info(f"Computing {ns_label} combined (name+definition) embeddings...")
    combined_embeddings = compute_embeddings_batch(embedding_service, combined_items, label=f"GO {ns_label} terms")
    save_embeddings(combined_embeddings, embeddings_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Pre-compute BioBERT embeddings for GO terms'
    )
    parser.add_argument(
        '--namespace', choices=['bp', 'mf'], default='bp',
        help='GO namespace to process: bp (Biological Process, default) or mf (Molecular Function)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Re-download go.obo even if a copy is cached in data/'
    )
    args = parser.parse_args()
    precompute_go_embeddings(namespace=args.namespace, force=args.force)
