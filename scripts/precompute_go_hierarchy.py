"""
Pre-compute GO hierarchy data with IC scores, depths, and ancestors

Downloads go-basic.obo, parses terms for the selected namespace, computes
Information Content from the gene annotation corpus, and writes
the hierarchy JSON for use by the GO suggestion scoring pipeline.

Usage:
    python scripts/precompute_go_hierarchy.py [--namespace bp|mf] [--force]

    --namespace bp  (default) Biological Process — reads go_bp_gene_annotations.json,
                              writes data/go_bp_hierarchy.json
    --namespace mf            Molecular Function  — reads go_mf_gene_annotations.json,
                              writes data/go_mf_hierarchy.json

Output:
    data/go_bp_hierarchy.json       - Per-term hierarchy (depth, IC, ancestors,
                                      propagated_gene_count) for BP
    data/go_mf_hierarchy.json       - Same, for MF
    data/go_bp_filtered_go_ids.json - Sorted GO IDs whose propagated gene set is
                                      in [MIN_GENES, MAX_GENES] (suggestion-corpus
                                      filter; consumed by subset_go_corpus.py)
"""

import argparse
import json
import logging
import math
import os
import re
import sys
from collections import defaultdict, deque
from urllib.request import Request, urlopen

# Setup project path (same pattern as precompute_go_embeddings.py)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

GO_BASIC_OBO_URL = "https://purl.obolibrary.org/obo/go/go-basic.obo"
GO_BASIC_OBO_LOCAL = "data/go-basic.obo"

# Namespace filter map: CLI arg -> OBO namespace value
NAMESPACE_FILTER = {
    'bp': 'biological_process',
    'mf': 'molecular_function',
}

# Root GO term for each namespace (used for BFS depth computation)
NAMESPACE_ROOTS = {
    'bp': 'GO:0008150',   # biological_process root
    'mf': 'GO:0003674',   # molecular_function root
}

# Gene-set size filter bounds for the suggestion corpus.
# Terms whose PROPAGATED gene set (own genes + all descendant genes) falls
# outside [MIN_GENES, MAX_GENES] are excluded: <10 is too specific to act as a
# Key Event signature and too small for reliable over-representation testing
# (10 is clusterProfiler's minGSSize default); >500 is too non-specific.
# Mirrors the Reactome filter in download_reactome_annotations.py.
MIN_GENES = 10
MAX_GENES = 500

# Canonical generic BP process terms that recur across many AOPs (cell death,
# inflammation, DNA damage, ROS, oxidative stress). Their PROPAGATED gene sets
# exceed MAX_GENES, so the gene-count ceiling drops them and curators can neither
# suggest nor search them for generic upstream KEs (#193). These IDs are
# force-included in the suggestion corpus regardless of gene count. Keep neutral,
# non-obsolete terms only — extend as new generic KEs surface.
GENERIC_BP_WHITELIST = {
    "GO:0006954": "inflammatory response",
    "GO:0008219": "cell death",
    "GO:0006915": "apoptotic process",
    "GO:0012501": "programmed cell death",
    "GO:0006974": "DNA damage response",
    "GO:0072593": "reactive oxygen species metabolic process",
    "GO:0006979": "response to oxidative stress",
}

# Directional / signed GO labels must never be suggested for a KE Process slot:
# direction belongs in the KE's PATO Action slot, not the GO term (see the
# amigo-ke-go-mapping skill's directionality lexicon). Terms whose label matches
# any of these operators are excluded from the suggestion corpus entirely (#193).
# NOTE: neutral "regulation of X" (no sign), bare "X activation" (e.g. "T cell
# activation"), and "... activity" MF terms are deliberately NOT matched.
DIRECTIONAL_LABEL_RE = re.compile(
    "|".join([
        r"\bpositive regulation of\b",
        r"\bnegative regulation of\b",
        r"\bactivation of\b",
        r"\binhibition of\b",
        r"\binduction of\b",
        r"\brepression of\b",
        r"\bsuppression of\b",
        r"\bstimulation of\b",
        r"\bup[- ]?regulation\b",
        r"\bdown[- ]?regulation\b",
        r"\bincreased?\b",
        r"\bdecreased?\b",
        r"\bactivated\b",
        r"\binhibited\b",
    ]),
    re.IGNORECASE,
)


def is_directional_label(name: str) -> bool:
    """True if a GO label encodes a sign/direction (excluded from the corpus)."""
    return bool(name and DIRECTIONAL_LABEL_RE.search(name))


def download_go_obo(url=GO_BASIC_OBO_URL, local_path=GO_BASIC_OBO_LOCAL, force=False):
    """Download go-basic.obo if not already present or if force=True."""
    if os.path.exists(local_path) and not force:
        logger.info(f"Using existing OBO file: {local_path}")
        return local_path

    logger.info(f"Downloading go-basic.obo from {url}...")
    req = Request(url, headers={'User-Agent': 'KE-WP-Mapping/1.0'})
    with urlopen(req) as response, open(local_path, 'wb') as out_file:
        out_file.write(response.read())
    size_mb = os.path.getsize(local_path) / 1024 / 1024
    logger.info(f"Downloaded to {local_path} ({size_mb:.1f} MB)")
    return local_path


def parse_obo_file(obo_path, namespace_value='biological_process'):
    """
    Parse go-basic.obo and extract all terms for the specified namespace.

    Args:
        obo_path: Path to the OBO file
        namespace_value: OBO namespace string to filter by

    Returns:
        tuple: (terms_dict, obsolete_remap_dict)
            terms_dict: {go_id: {name, namespace, is_a[], part_of[]}}
            obsolete_remap: {obsolete_id: replacement_id}
    """
    logger.info(f"Parsing OBO file: {obo_path} (namespace: {namespace_value})")

    terms = {}
    obsolete_terms = []
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
                    'is_a': [],
                    'part_of': [],
                    'is_obsolete': False,
                    'replaced_by': None,
                    'consider': [],
                }
                continue

            if line == '' or line.startswith('['):
                if in_term and current_term and current_term['id']:
                    if current_term['namespace'] == namespace_value:
                        if current_term['is_obsolete']:
                            obsolete_terms.append(current_term)
                        else:
                            go_id = current_term['id']
                            terms[go_id] = {
                                'name': current_term['name'],
                                'namespace': current_term['namespace'],
                                'is_a': current_term['is_a'],
                                'part_of': current_term['part_of'],
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
            elif line.startswith('is_a: '):
                # Strip comment after !
                parent_id = line[6:].split(' ! ')[0].strip()
                current_term['is_a'].append(parent_id)
            elif line.startswith('relationship: part_of '):
                part_id = line[22:].split(' ! ')[0].strip()
                current_term['part_of'].append(part_id)
            elif line == 'is_obsolete: true':
                current_term['is_obsolete'] = True
            elif line.startswith('replaced_by: '):
                current_term['replaced_by'] = line[13:].strip()
            elif line.startswith('consider: '):
                current_term['consider'].append(line[10:].strip())

    # Build obsolete remap dict
    remap = {}
    skipped = 0
    for term in obsolete_terms:
        obs_id = term['id']
        if term['replaced_by'] and term['replaced_by'] in terms:
            remap[obs_id] = term['replaced_by']
        elif term['consider']:
            # Use first consider term that exists in our active terms
            found = False
            for candidate in term['consider']:
                if candidate in terms:
                    remap[obs_id] = candidate
                    found = True
                    break
            if not found:
                logger.warning(f"Obsolete term {obs_id} ({term['name']}): no valid replacement found")
                skipped += 1
        else:
            logger.warning(f"Obsolete term {obs_id} ({term['name']}): no replaced_by or consider fields")
            skipped += 1

    logger.info(f"Parsed {len(terms)} active {namespace_value} terms")
    logger.info(f"Found {len(obsolete_terms)} obsolete {namespace_value} terms")
    logger.info(f"Remapped {len(remap)} obsolete terms, skipped {skipped} with no replacement")

    return terms, remap


def build_parents_map(terms):
    """Build parents map from is_a + part_of relationships."""
    parents = defaultdict(set)
    for go_id, data in terms.items():
        for parent_id in data['is_a']:
            if parent_id in terms:
                parents[go_id].add(parent_id)
        for parent_id in data['part_of']:
            if parent_id in terms:
                parents[go_id].add(parent_id)
    return parents


def compute_ancestors(terms, parents_map):
    """
    Compute transitive closure of ancestors for each term.

    Uses memoized recursive traversal with cycle guard.
    """
    ancestors_cache = {}
    in_progress = set()  # cycle guard

    def _get_ancestors(go_id):
        if go_id in ancestors_cache:
            return ancestors_cache[go_id]

        if go_id in in_progress:
            logger.warning(f"Cycle detected at {go_id}, breaking")
            return set()

        in_progress.add(go_id)
        result = set()
        for parent in parents_map.get(go_id, set()):
            result.add(parent)
            result.update(_get_ancestors(parent))
        in_progress.discard(go_id)

        ancestors_cache[go_id] = result
        return result

    logger.info("Computing transitive ancestors...")
    for go_id in terms:
        _get_ancestors(go_id)

    return ancestors_cache


def compute_depths(terms, parents_map, root):
    """
    Compute minimum depth from the namespace root via BFS.

    Args:
        terms: dict of GO terms
        parents_map: dict of {go_id: set(parent_ids)}
        root: root GO term ID (e.g. GO:0008150 for BP, GO:0003674 for MF)

    Returns dict: {go_id: depth}
    """
    logger.info(f"Computing depths via BFS from root {root}...")

    # Build children map for BFS
    children = defaultdict(set)
    for go_id, parent_ids in parents_map.items():
        for pid in parent_ids:
            children[pid].add(go_id)

    depths = {}
    queue = deque([(root, 0)])
    depths[root] = 0

    while queue:
        current, depth = queue.popleft()
        for child in children.get(current, set()):
            if child not in depths or depth + 1 < depths[child]:
                depths[child] = depth + 1
                queue.append((child, depth + 1))

    # Terms not reachable from root get depth -1 (should be rare in go-basic.obo)
    unreachable = 0
    for go_id in terms:
        if go_id not in depths:
            depths[go_id] = -1
            unreachable += 1

    if unreachable > 0:
        logger.warning(f"{unreachable} terms not reachable from root {root}")

    max_depth = max(d for d in depths.values() if d >= 0)
    logger.info(f"Depth range: 0 to {max_depth}")

    return depths


def compute_ic_scores(terms, ancestors_cache, annotations_path, remap, root):
    """
    Compute Information Content scores from gene annotation corpus.

    IC(t) = -log2(freq(t) / freq(root))
    Normalized to [0, 1] by dividing by max IC.
    Root forced to IC = 0.0.

    Args:
        terms: dict of active GO terms
        ancestors_cache: transitive ancestor map
        annotations_path: path to gene annotations JSON
        remap: obsolete term remapping dict
        root: namespace root GO term ID
    """
    logger.info(f"Loading annotation corpus from {annotations_path}...")
    with open(annotations_path, 'r') as f:
        raw_annotations = json.load(f)

    # Apply obsolete term remapping to annotations
    annotations = defaultdict(set)
    remapped_count = 0
    for go_id, genes in raw_annotations.items():
        effective_id = remap.get(go_id, go_id)
        if effective_id != go_id:
            remapped_count += 1
        if effective_id in terms:
            annotations[effective_id].update(genes)

    logger.info(f"Remapped {remapped_count} annotation entries via obsolete term remap")
    logger.info(f"Annotations cover {len(annotations)} active terms")

    # Propagate annotations upward: each gene annotated to t is also
    # implicitly annotated to all ancestors of t
    logger.info("Propagating annotations upward through hierarchy...")
    propagated = defaultdict(set)
    for go_id, genes in annotations.items():
        propagated[go_id].update(genes)
        for ancestor in ancestors_cache.get(go_id, set()):
            propagated[ancestor].update(genes)

    # Propagated gene-set size per term — used downstream for the [MIN,MAX]
    # corpus filter. Terms absent from `propagated` have no annotated genes
    # anywhere in their subtree (count 0).
    propagated_counts = {go_id: len(propagated.get(go_id, set())) for go_id in terms}

    # Compute IC
    root_freq = len(propagated.get(root, set()))
    if root_freq == 0:
        logger.error("Root term has no annotations after propagation!")
        root_freq = 1  # avoid division by zero

    logger.info(f"Root term {root} has {root_freq} unique genes after propagation")

    raw_ic = {}
    for go_id in terms:
        freq = len(propagated.get(go_id, set()))
        if freq == 0:
            # Terms with no annotations get maximum IC (most specific/rare)
            raw_ic[go_id] = None  # placeholder, set after max computation
        else:
            raw_ic[go_id] = -math.log2(freq / root_freq)

    # Force root IC = 0.0 (log2(1) = 0)
    raw_ic[root] = 0.0

    # Find max IC among computed values for normalization
    computed_ics = [v for v in raw_ic.values() if v is not None]
    max_ic = max(computed_ics) if computed_ics else 1.0

    # Normalize to [0, 1]
    ic_scores = {}
    for go_id, ic_val in raw_ic.items():
        if ic_val is None:
            ic_scores[go_id] = 1.0  # no annotations = max specificity
        else:
            ic_scores[go_id] = ic_val / max_ic if max_ic > 0 else 0.0

    # Force root to exactly 0.0
    ic_scores[root] = 0.0

    # Stats
    non_zero = sum(1 for v in ic_scores.values() if v > 0)
    logger.info(f"IC scores computed: {len(ic_scores)} terms, {non_zero} with IC > 0")
    logger.info(f"IC range: 0.0 to {max(ic_scores.values()):.4f} (normalized)")

    return ic_scores, propagated_counts


def build_hierarchy_json(terms, ancestors_cache, depths, ic_scores, propagated_counts):
    """Build the final hierarchy JSON structure."""
    hierarchy = {}
    for go_id, data in terms.items():
        hierarchy[go_id] = {
            'name': data['name'],
            'namespace': data['namespace'],
            'depth': depths.get(go_id, -1),
            'ic_score': round(ic_scores.get(go_id, 0.0), 6),
            'propagated_gene_count': propagated_counts.get(go_id, 0),
            'ancestors': sorted(list(ancestors_cache.get(go_id, set()))),
            'is_obsolete': False,
        }
    return hierarchy


def main():
    parser = argparse.ArgumentParser(
        description='Pre-compute GO hierarchy data with IC scores'
    )
    parser.add_argument(
        '--namespace', choices=['bp', 'mf'], default='bp',
        help='GO namespace to process: bp (Biological Process, default) or mf (Molecular Function)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Force re-download of go-basic.obo'
    )
    args = parser.parse_args()

    namespace = args.namespace
    namespace_value = NAMESPACE_FILTER[namespace]
    root = NAMESPACE_ROOTS[namespace]
    annotations_path = f'data/go_{namespace}_gene_annotations.json'
    output_path = f'data/go_{namespace}_hierarchy.json'
    filtered_ids_path = f'data/go_{namespace}_filtered_go_ids.json'

    logger.info(f"Processing GO hierarchy for namespace: {namespace_value} (root: {root})")

    # Download OBO
    obo_path = download_go_obo(force=args.force)

    # Parse terms for the selected namespace
    terms, remap = parse_obo_file(obo_path, namespace_value=namespace_value)

    # Build hierarchy structures
    parents_map = build_parents_map(terms)
    ancestors_cache = compute_ancestors(terms, parents_map)
    depths = compute_depths(terms, parents_map, root=root)

    # Compute IC scores + propagated gene-set sizes
    ic_scores, propagated_counts = compute_ic_scores(
        terms, ancestors_cache, annotations_path, remap, root=root
    )

    # Build output — hierarchy keeps ALL terms (ancestor IC lookups need them)
    hierarchy = build_hierarchy_json(terms, ancestors_cache, depths, ic_scores, propagated_counts)

    # Write output
    logger.info(f"Writing {len(hierarchy)} terms to {output_path}...")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(hierarchy, f, indent=2)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info(f"Output: {output_path} ({size_mb:.1f} MB)")

    # Write the filtered-ID list: terms whose propagated gene set is in
    # [MIN_GENES, MAX_GENES]. Drives the embedding-corpus subset (subset_go_corpus.py).
    in_range_ids = {
        go_id for go_id, c in propagated_counts.items()
        if MIN_GENES <= c <= MAX_GENES
    }
    dropped_zero = sum(1 for c in propagated_counts.values() if c == 0)
    dropped_low = sum(1 for c in propagated_counts.values() if 0 < c < MIN_GENES)
    dropped_high = sum(1 for c in propagated_counts.values() if c > MAX_GENES)

    # Force-include the curated generic BP terms dropped by the gene-count ceiling
    # so they are suggestable/searchable for generic upstream KEs (#193, BP only).
    whitelist = set(GENERIC_BP_WHITELIST) if namespace == 'bp' else set()
    forced = {gid for gid in whitelist if gid in terms}
    missing_whitelist = sorted(whitelist - forced)
    n_whitelisted = len(forced - in_range_ids)

    # Exclude directional/signed terms entirely — direction lives in the KE's PATO
    # Action slot, not the GO term, so signed variants must never be suggested (#193).
    candidate_ids = in_range_ids | forced
    kept, n_directional_excluded = set(), 0
    for gid in candidate_ids:
        if is_directional_label((terms.get(gid) or {}).get('name') or ''):
            n_directional_excluded += 1
            continue
        kept.add(gid)
    in_range = sorted(kept)

    with open(filtered_ids_path, 'w', encoding='utf-8') as f:
        json.dump(in_range, f, indent=2)
    logger.info(
        "Filter [%d,%d] genes: %d kept (of %d) | dropped: %d zero-gene, %d <%d, %d >%d | "
        "+%d generic-whitelisted, -%d directional-excluded -> %s",
        MIN_GENES, MAX_GENES, len(in_range), len(propagated_counts),
        dropped_zero, dropped_low, MIN_GENES, dropped_high, MAX_GENES,
        n_whitelisted, n_directional_excluded, filtered_ids_path,
    )
    if missing_whitelist:
        logger.warning(
            "Whitelisted generic terms not found in parsed %s namespace (obsolete/renamed?): %s",
            namespace, ", ".join(missing_whitelist),
        )
    logger.info("Done.")


if __name__ == '__main__':
    main()
