"""
GMT file generation for KE-WP and KE-GO mapping types.

Pure Python module — no Flask dependency. Called by routes in later plans.
"""
import io
import json
import logging
import os
import re
import unicodedata
from collections import defaultdict

logger = logging.getLogger(__name__)

WIKIPATHWAYS_SPARQL = "https://sparql.wikipathways.org/sparql"


def _make_ke_slug(ke_id: str, ke_title: str) -> str:
    """Return KE{N}_{Title_Slug} without a target suffix.

    Examples:
        _make_ke_slug('KE 55', 'Decreased BDNF Expression') -> 'KE55_Decreased_BDNF_Expression'
    """
    num = re.sub(r'\D', '', ke_id)
    # Normalise unicode -> ASCII, then keep only alphanumeric/underscore chars
    normalized = unicodedata.normalize("NFKD", ke_title).encode("ascii", "ignore").decode("ascii")
    title_slug = re.sub(r'[^a-zA-Z0-9]+', '_', normalized).strip('_')
    return f"KE{num}_{title_slug}"


def _parse_gene_bindings(data: dict) -> dict:
    """Parse SPARQL JSON result bindings into {pathway_id: [gene_symbol, ...]}."""
    result = {}
    for binding in data.get("results", {}).get("bindings", []):
        pid = binding.get("pathwayID", {}).get("value", "")
        gene = binding.get("geneSymbol", {}).get("value", "")
        if pid and gene:
            result.setdefault(pid, []).append(gene)
    return result


def _fetch_pathway_genes_batch(wp_ids: list, cache_model=None) -> dict:
    """Return {wp_id: [hgnc_symbol, ...]} for all given wp_ids.

    Issues a single SPARQL VALUES query to WikiPathways for all IDs at once.
    Silently returns an empty dict on any failure.
    """
    if not wp_ids:
        return {}
    import hashlib
    import requests

    values_clause = " ".join([f'"{wid}"' for wid in wp_ids])
    query = f"""
PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT DISTINCT ?pathwayID ?geneSymbol WHERE {{
  ?pathway a wp:Pathway ;
           dcterms:identifier ?pathwayID .
  ?geneProduct dcterms:isPartOf ?pathway ;
               wp:bdbHgncSymbol ?geneSymbolIRI .
  BIND(STRAFTER(STR(?geneSymbolIRI), "hgnc.symbol/") AS ?geneSymbol)
  VALUES ?pathwayID {{ {values_clause} }}
}}
"""
    query_hash = hashlib.md5(query.encode()).hexdigest()

    # Try cache first
    if cache_model:
        cached = cache_model.get_cached_response(WIKIPATHWAYS_SPARQL, query_hash)
        if cached:
            data = json.loads(cached)
            return _parse_gene_bindings(data)

    try:
        resp = requests.post(
            WIKIPATHWAYS_SPARQL,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if cache_model:
            cache_model.cache_response(WIKIPATHWAYS_SPARQL, query_hash, resp.text, expiry_hours=24)
        return _parse_gene_bindings(data)
    except Exception as e:
        logger.warning("WikiPathways SPARQL batch gene fetch failed: %s", e)
        return {}


def generate_ke_wp_gmt(mappings, cache_model=None, min_confidence=None) -> str:
    """Generate GMT content for KE-WP mappings.

    Parameters
    ----------
    mappings:
        List of dicts from MappingModel.get_all_mappings(). Each dict must
        contain at least: ke_id, ke_title, wp_id, wp_title, confidence_level.
    cache_model:
        Optional CacheModel instance for SPARQL result caching. Pass None to
        skip caching.
    min_confidence:
        Optional lowercase string (e.g. "high"). Rows whose confidence_level
        does not match are excluded.

    Returns
    -------
    str
        GMT-formatted string (tab-separated, one row per KE-pathway pair).
        Empty string if no rows survive filtering or no genes are found.
    """
    # Apply confidence filter
    if min_confidence:
        mappings = [
            r for r in mappings
            if r.get("confidence_level", "").lower() == min_confidence
        ]

    if not mappings:
        return ""

    # Collect unique WP IDs for batch SPARQL
    wp_ids = list(dict.fromkeys(r["wp_id"] for r in mappings))
    genes_by_wp = _fetch_pathway_genes_batch(wp_ids, cache_model=cache_model)

    buf = io.StringIO()
    for row in mappings:
        wp_id = row["wp_id"]
        genes = genes_by_wp.get(wp_id, [])
        if not genes:
            # GMT convention: skip rows with no genes
            continue
        # Deduplicate while preserving order
        genes = list(dict.fromkeys(genes))
        ke_slug = _make_ke_slug(row["ke_id"], row["ke_title"])
        term_name = f"{ke_slug}_{wp_id}"
        description = row["wp_title"]
        line = "\t".join([term_name, description] + genes)
        buf.write(line + "\n")

    return buf.getvalue()


def _load_go_annotations_merged(bp_path=None, mf_path=None) -> dict:
    """Load and merge BP and MF gene annotation dicts.

    BP is ontology-propagated (#208) — a gene annotated to a term counts toward
    all of its ancestors, per the GO true-path rule. Without that, exported KE
    gene sets were smaller than the mapping claimed and generic terms such as
    GO:0008219 "cell death" resolved to 7 genes instead of 891.

    Explicit paths keep the direct-annotation behaviour, since existing fixtures
    pass toy files with no accompanying hierarchy.
    """
    if bp_path is None and mf_path is None:
        from src.services.go_annotation_index import get_go_annotations_merged
        return get_go_annotations_merged()

    data_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data')

    if bp_path is None:
        bp_path = os.path.join(data_dir, 'go_bp_gene_annotations.json')
    if mf_path is None:
        mf_path = os.path.join(data_dir, 'go_mf_gene_annotations.json')

    merged = {}
    for path, label in [(bp_path, 'BP'), (mf_path, 'MF')]:
        try:
            with open(path) as f:
                merged.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            logger.info("Could not load GO %s annotations from %s: %s", label, path, e)

    return merged


def generate_ke_go_gmt(mappings, go_annotations_path=None, min_confidence=None) -> str:
    """Generate GMT content for KE-GO mappings.

    Parameters
    ----------
    mappings:
        List of dicts from GoMappingModel.get_all_mappings(). Each dict must
        contain at least: ke_id, ke_title, go_id, go_name, confidence_level.
        MF mappings are identified by go_namespace='molecular_function'.
    go_annotations_path:
        Path to go_bp_gene_annotations.json. Defaults to
        data/go_bp_gene_annotations.json relative to the project root.
        MF annotations are loaded from the sibling go_mf_gene_annotations.json.
    min_confidence:
        Optional lowercase string for confidence filtering.

    Returns
    -------
    str
        GMT-formatted string. Empty string if no rows survive.
    """
    # Load both BP and MF annotations; MF terms need genes from MF file
    go_annotations = _load_go_annotations_merged(bp_path=go_annotations_path)

    # Apply confidence filter
    if min_confidence:
        mappings = [
            r for r in mappings
            if r.get("confidence_level", "").lower() == min_confidence
        ]

    if not mappings:
        return ""

    buf = io.StringIO()
    for row in mappings:
        go_id = row["go_id"]
        genes = go_annotations.get(go_id, [])
        if not genes:
            # Skip rows with no annotation entry
            continue
        # Deduplicate while preserving order
        genes = list(dict.fromkeys(genes))
        ke_slug = _make_ke_slug(row["ke_id"], row["ke_title"])
        term_name = f"{ke_slug}_{go_id}"
        description = row["go_name"]
        go_dir = row.get("go_direction")
        if go_dir:
            description += f" | direction:{go_dir}"
        line = "\t".join([term_name, description] + genes)
        buf.write(line + "\n")

    return buf.getvalue()


def generate_ke_centric_wp_gmt(mappings, cache_model=None, min_confidence=None) -> str:
    """Generate KE-centric GMT content for KE-WP mappings.

    Each row represents one Key Event. Gene symbols are unioned across all
    approved WikiPathways mappings for that KE. Field 1 is just ``KE{N}``
    (e.g. ``KE55``), not the full slug — suitable for KE-level enrichment
    testing with fgsea or clusterProfiler.

    Parameters
    ----------
    mappings:
        List of dicts from MappingModel.get_all_mappings(). Each dict must
        contain at least: ke_id, ke_title, wp_id, confidence_level.
    cache_model:
        Optional CacheModel instance for SPARQL result caching.
    min_confidence:
        Optional lowercase string (e.g. "high"). Rows whose confidence_level
        does not match are excluded.

    Returns
    -------
    str
        GMT-formatted string (tab-separated, one row per KE).
        Empty string if no rows survive filtering or no genes are found.
    """
    if min_confidence:
        mappings = [r for r in mappings if r.get("confidence_level", "").lower() == min_confidence]

    if not mappings:
        return ""

    # Group WP IDs by KE, preserving KE metadata
    ke_to_wps = defaultdict(list)
    ke_meta = {}
    for row in mappings:
        ke_to_wps[row["ke_id"]].append(row["wp_id"])
        ke_meta[row["ke_id"]] = (row["ke_id"], row["ke_title"])

    # Collect all unique WP IDs for a single batch SPARQL call
    all_wp_ids = list(dict.fromkeys(wp for wps in ke_to_wps.values() for wp in wps))
    genes_by_wp = _fetch_pathway_genes_batch(all_wp_ids, cache_model=cache_model)

    buf = io.StringIO()
    for ke_id in sorted(ke_to_wps.keys(), key=lambda k: int(re.sub(r'\D', '', k) or '0')):
        all_genes = []
        for wp_id in ke_to_wps[ke_id]:
            all_genes.extend(genes_by_wp.get(wp_id, []))
        genes = list(dict.fromkeys(all_genes))  # deduplicate, preserve order
        if not genes:
            continue
        ke_id_raw, ke_title = ke_meta[ke_id]
        num = re.sub(r'\D', '', ke_id_raw)
        term_name = f"KE{num}"  # Field 1: JUST "KE55" — locked decision
        description = ke_title  # Field 2: KE title
        line = "\t".join([term_name, description] + genes)
        buf.write(line + "\n")

    return buf.getvalue()


def generate_ke_centric_go_gmt(mappings, go_annotations_path=None, min_confidence=None) -> str:
    """Generate KE-centric GMT content for KE-GO mappings.

    Each row represents one Key Event. Gene symbols are unioned across all
    approved GO (BP and MF) mappings for that KE. Field 1 is just
    ``KE{N}`` (e.g. ``KE55``).

    Parameters
    ----------
    mappings:
        List of dicts from GoMappingModel.get_all_mappings(). Each dict must
        contain at least: ke_id, ke_title, go_id, confidence_level.
        MF mappings are identified by go_namespace='molecular_function'.
    go_annotations_path:
        Path to go_bp_gene_annotations.json. Defaults to
        data/go_bp_gene_annotations.json relative to the project root.
        MF annotations are loaded from the sibling go_mf_gene_annotations.json.
    min_confidence:
        Optional lowercase string for confidence filtering.

    Returns
    -------
    str
        GMT-formatted string (tab-separated, one row per KE).
        Empty string if no rows survive filtering or no genes are found.
    """
    # Load both BP and MF annotations
    go_annotations = _load_go_annotations_merged(bp_path=go_annotations_path)

    if min_confidence:
        mappings = [r for r in mappings if r.get("confidence_level", "").lower() == min_confidence]

    if not mappings:
        return ""

    # Group GO IDs by KE, preserving KE metadata
    ke_to_gos = defaultdict(list)
    ke_meta = {}
    for row in mappings:
        ke_to_gos[row["ke_id"]].append(row["go_id"])
        ke_meta[row["ke_id"]] = (row["ke_id"], row["ke_title"])

    buf = io.StringIO()
    for ke_id in sorted(ke_to_gos.keys(), key=lambda k: int(re.sub(r'\D', '', k) or '0')):
        all_genes = []
        for go_id in ke_to_gos[ke_id]:
            all_genes.extend(go_annotations.get(go_id, []))
        genes = list(dict.fromkeys(all_genes))  # deduplicate, preserve order
        if not genes:
            continue
        ke_id_raw, ke_title = ke_meta[ke_id]
        num = re.sub(r'\D', '', ke_id_raw)
        term_name = f"KE{num}"  # Field 1: JUST "KE55" — locked decision
        description = ke_title  # Field 2: KE title
        line = "\t".join([term_name, description] + genes)
        buf.write(line + "\n")

    return buf.getvalue()


def _load_reactome_annotations(path=None) -> dict:
    """Load {reactome_id: [hgnc, ...]} from data/reactome_gene_annotations.json.

    Single-file analog of _load_go_annotations_merged. Returns {} on
    OSError / JSONDecodeError so callers can degrade gracefully.
    """
    if path is None:
        path = os.path.join(
            os.path.dirname(__file__), '..', '..',
            'data', 'reactome_gene_annotations.json',
        )
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.info("Could not load Reactome annotations from %s: %s", path, e)
        return {}


def generate_ke_reactome_gmt(mappings, gene_annotations_path=None, min_confidence=None) -> str:
    """Generate per-mapping GMT content for KE-Reactome mappings.

    Each row: ``KE{N}_{Slug}_R-HSA-NNNN \\t pathway_name \\t gene1 \\t gene2 ...``

    Genes are loaded from ``data/reactome_gene_annotations.json`` (overridable
    via ``gene_annotations_path``). Rows whose ``reactome_id`` has no entry in
    that file are silently skipped to avoid emitting malformed lines.

    Parameters
    ----------
    mappings:
        List of dicts (e.g. from ``ReactomeMappingModel.get_all_mappings``).
        Each dict must contain at least ``ke_id``, ``ke_title``, ``reactome_id``,
        ``pathway_name``, ``confidence_level``.
    gene_annotations_path:
        Optional override for the Reactome gene-annotations JSON path.
    min_confidence:
        Optional lowercase string (e.g. ``"high"``). Rows whose
        ``confidence_level`` does not match are excluded.

    Returns
    -------
    str
        GMT-formatted string (tab-separated). Empty string if no rows survive.
    """
    reactome_annotations = _load_reactome_annotations(path=gene_annotations_path)

    # Apply confidence filter
    if min_confidence:
        mappings = [
            r for r in mappings
            if r.get("confidence_level", "").lower() == min_confidence
        ]

    if not mappings:
        return ""

    buf = io.StringIO()
    for row in mappings:
        reactome_id = row["reactome_id"]
        genes = reactome_annotations.get(reactome_id, [])
        if not genes:
            # Skip rows with no annotation entry
            continue
        # Deduplicate while preserving order
        genes = list(dict.fromkeys(genes))
        ke_slug = _make_ke_slug(row["ke_id"], row["ke_title"])
        term_name = f"{ke_slug}_{reactome_id}"
        description = row["pathway_name"]
        # No direction suffix — Reactome has no direction concept (per D-05).
        line = "\t".join([term_name, description] + genes)
        buf.write(line + "\n")

    return buf.getvalue()


def generate_ke_centric_reactome_gmt(mappings, gene_annotations_path=None, min_confidence=None) -> str:
    """Generate KE-centric GMT content for KE-Reactome mappings.

    One row per Key Event. Gene symbols are unioned (order-preserving dedup)
    across all approved Reactome mappings for that KE. Field 1 is just
    ``KE{N}`` (e.g. ``KE55``), suitable for KE-level enrichment with fgsea or
    clusterProfiler.

    Parameters
    ----------
    mappings:
        List of dicts. Each dict must contain at least ``ke_id``, ``ke_title``,
        ``reactome_id``, ``confidence_level``.
    gene_annotations_path:
        Optional override for the Reactome gene-annotations JSON path.
    min_confidence:
        Optional lowercase string for confidence filtering.

    Returns
    -------
    str
        GMT-formatted string (tab-separated, one row per KE).
        Empty string if no rows survive filtering or no genes are found.
    """
    reactome_annotations = _load_reactome_annotations(path=gene_annotations_path)

    if min_confidence:
        mappings = [
            r for r in mappings
            if r.get("confidence_level", "").lower() == min_confidence
        ]

    if not mappings:
        return ""

    # Group Reactome IDs by KE, preserving KE metadata
    ke_to_reactome = defaultdict(list)
    ke_meta = {}
    for row in mappings:
        ke_to_reactome[row["ke_id"]].append(row["reactome_id"])
        ke_meta[row["ke_id"]] = (row["ke_id"], row["ke_title"])

    buf = io.StringIO()
    for ke_id in sorted(ke_to_reactome.keys(), key=lambda k: int(re.sub(r'\D', '', k) or '0')):
        all_genes = []
        for rid in ke_to_reactome[ke_id]:
            all_genes.extend(reactome_annotations.get(rid, []))
        genes = list(dict.fromkeys(all_genes))  # deduplicate, preserve order
        if not genes:
            continue
        ke_id_raw, ke_title = ke_meta[ke_id]
        num = re.sub(r'\D', '', ke_id_raw)
        term_name = f"KE{num}"  # Field 1: JUST "KE55" — locked decision
        description = ke_title  # Field 2: KE title
        line = "\t".join([term_name, description] + genes)
        buf.write(line + "\n")

    return buf.getvalue()
