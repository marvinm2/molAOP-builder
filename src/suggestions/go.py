"""
GO Term Suggestion Service
Provides intelligent GO term suggestions (Biological Process and Molecular Function)
for Key Events using pre-computed embeddings and gene annotation overlap.
"""
import json
import logging
import os
import re
from collections import namedtuple
from difflib import SequenceMatcher
from typing import Dict, List

import numpy as np
from src.core.config_loader import ConfigLoader
from src.suggestions.ke_genes import get_genes_from_ke
from src.suggestions.scoring import combine_scored_items
from src.utils.description_toggle import resolve_description_usage
from src.utils.text import (
    remove_directionality_terms,
    detect_ke_direction,
    detect_go_direction,
    is_directional_go_label,
)

logger = logging.getLogger(__name__)

# Lightweight container for per-namespace data + config used by scoring methods
_NamespaceData = namedtuple(
    '_NamespaceData',
    ['embeddings', 'name_embeddings', 'metadata', 'annotations', 'hierarchy', 'config']
)


class GoSuggestionService:
    """Service for generating GO BP and MF term suggestions based on Key Events"""

    def __init__(
        self,
        cache_model=None,
        config=None,
        embedding_service=None,
        ke_override_model=None,
        go_embeddings_path='data/go_bp_embeddings.npz',
        go_name_embeddings_path='data/go_bp_name_embeddings.npz',
        go_metadata_path='data/go_bp_metadata.json',
        go_annotations_path='data/go_bp_gene_annotations.json',
        go_mf_embeddings_path='data/go_mf_embeddings.npz',
        go_mf_name_embeddings_path='data/go_mf_name_embeddings.npz',
        go_mf_metadata_path='data/go_mf_metadata.json',
        go_mf_annotations_path='data/go_mf_gene_annotations.json',
        go_mf_hierarchy_path='data/go_mf_hierarchy.json',
    ):
        self.cache_model = cache_model
        self.config = config or ConfigLoader.get_default_config()
        self.embedding_service = embedding_service
        self.ke_override_model = ke_override_model
        self.aop_wiki_endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

        # Load pre-computed GO BP data
        self.go_embeddings = {}
        self.go_name_embeddings = {}
        self.go_metadata = {}
        self.go_gene_annotations = {}

        self._load_go_embeddings(go_embeddings_path)
        self._load_go_name_embeddings(go_name_embeddings_path)
        self._load_go_metadata(go_metadata_path)
        self._load_go_annotations(go_annotations_path)

        # Load GO BP hierarchy for IC-based scoring and redundancy filtering.
        # Filename matches precompute_go_hierarchy.py's actual output (go_{ns}_hierarchy.json).
        self.go_hierarchy = {}
        self._load_go_hierarchy('data/go_bp_hierarchy.json')

        # Load pre-computed GO MF data (graceful degradation — BP-only mode if files absent)
        self.go_mf_embeddings = {}
        self.go_mf_name_embeddings = {}
        self.go_mf_metadata = {}
        self.go_mf_gene_annotations = {}
        self.go_mf_hierarchy = {}

        # v1.5 pure-semantic: log once per instance when first combine call occurs
        self._v15_logged = False

        self._load_mf_data(
            go_mf_embeddings_path,
            go_mf_name_embeddings_path,
            go_mf_metadata_path,
            go_mf_annotations_path,
            go_mf_hierarchy_path,
        )

    def _load_mf_data(
        self,
        embeddings_path,
        name_embeddings_path,
        metadata_path,
        annotations_path,
        hierarchy_path,
    ):
        """Load all MF data files with graceful degradation.

        If any MF file is missing the service starts in BP-only mode.
        Individual file load failures are logged but do not abort startup.
        """
        try:
            self._load_npz_into(embeddings_path, self.go_mf_embeddings, 'GO MF embeddings')
            self._load_npz_into(name_embeddings_path, self.go_mf_name_embeddings, 'GO MF name embeddings')
            self._load_json_into(metadata_path, self.go_mf_metadata, 'GO MF metadata')
            self._load_json_into(annotations_path, self.go_mf_gene_annotations, 'GO MF annotations')
            self._load_mf_hierarchy(hierarchy_path)

            if self.go_mf_metadata:
                logger.info(
                    "GO MF data loaded: %d terms, %d embeddings",
                    len(self.go_mf_metadata),
                    len(self.go_mf_embeddings),
                )
            else:
                logger.info("GO MF data files absent — running in BP-only mode")
        except Exception as e:
            logger.warning("Could not load GO MF data, running in BP-only mode: %s", e)
            self.go_mf_embeddings = {}
            self.go_mf_name_embeddings = {}
            self.go_mf_metadata = {}
            self.go_mf_gene_annotations = {}
            self.go_mf_hierarchy = {}

    def _load_npz_into(self, path: str, target: dict, label: str):
        """Load an NPZ embedding file into an existing dict (in-place)."""
        npz_path = path.replace('.npy', '.npz')
        if os.path.exists(npz_path):
            try:
                with np.load(npz_path) as data:
                    ids = data['ids']
                    matrix = data['matrix']
                target.update(dict(zip(ids, matrix)))
                logger.info("Loaded %d %s (normalized)", len(target), label)
            except Exception as e:
                logger.warning("Could not load %s: %s", label, e)
        else:
            logger.info("%s file not found: %s", label, npz_path)

    def _load_json_into(self, path: str, target: dict, label: str):
        """Load a JSON file into an existing dict (in-place)."""
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    target.update(json.load(f))
                logger.info("Loaded %d %s entries", len(target), label)
            except Exception as e:
                logger.warning("Could not load %s: %s", label, e)
        else:
            logger.info("%s file not found: %s", label, path)

    def _load_mf_hierarchy(self, path: str):
        """Load MF hierarchy file into self.go_mf_hierarchy."""
        if not os.path.exists(path):
            logger.info("GO MF hierarchy file not found at %s", path)
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for go_id, data in raw.items():
                ancestors = data.get('ancestors', [])
                data['ancestors'] = set(ancestors) if isinstance(ancestors, list) else ancestors
            self.go_mf_hierarchy = raw
            logger.info("Loaded GO MF hierarchy for %d terms", len(self.go_mf_hierarchy))
        except Exception as e:
            logger.warning("Could not load GO MF hierarchy from %s: %s", path, e)

    def _load_go_embeddings(self, path):
        """Load pre-computed GO BP embeddings from NPZ format (no pickle)."""
        self._load_npz_into(path, self.go_embeddings, 'GO BP embeddings')

    def _load_go_name_embeddings(self, path):
        """Load pre-computed GO BP name-only embeddings from NPZ format (no pickle)."""
        self._load_npz_into(path, self.go_name_embeddings, 'GO BP name embeddings')

    def _load_go_metadata(self, path):
        """Load GO BP metadata (names, definitions, relationships)"""
        self._load_json_into(path, self.go_metadata, 'GO BP metadata')

    def _load_go_annotations(self, path):
        """Load GO BP gene annotations, ontology-propagated (#208).

        The on-disk annotations file holds direct GAF annotations only, which
        violates the GO true-path rule and made generic terms resolve to
        near-empty gene sets — GO:0008219 "cell death" measured 7 genes against
        891 once its descendants are counted. The index prefers the precomputed
        propagated file and degrades to the direct file, so a missing artifact
        costs accuracy rather than raising.

        A caller-supplied non-default path still loads verbatim, for tests.
        """
        if path != 'data/go_bp_gene_annotations.json':
            self._load_json_into(path, self.go_gene_annotations, 'GO BP annotations')
            return

        from src.services.go_annotation_index import get_go_annotations
        self.go_gene_annotations.update(get_go_annotations('bp'))
        logger.info("Loaded %d GO BP annotations (propagated)", len(self.go_gene_annotations))

    def _load_go_hierarchy(self, path):
        """Load GO hierarchy data (depth, IC scores, ancestors) for scoring adjustments.

        Provides graceful degradation: if file is missing or invalid, hierarchy
        features are simply disabled and suggestions work as before.
        """
        if not os.path.exists(path):
            logger.info("GO hierarchy file not found at %s, running without hierarchy features", path)
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            # Convert ancestor lists to sets for O(1) lookup
            for go_id, data in raw.items():
                ancestors = data.get('ancestors', [])
                data['ancestors'] = set(ancestors) if isinstance(ancestors, list) else ancestors
            self.go_hierarchy = raw
            logger.info("Loaded GO hierarchy for %d terms", len(self.go_hierarchy))
        except Exception as e:
            logger.warning("Could not load GO hierarchy from %s: %s", path, e)

    def _apply_ic_boost(self, suggestions, ns_data: _NamespaceData):
        """Apply information content boost to favour more specific GO terms.

        Formula: hybrid_score *= (1 + ic_weight * ic_score)

        **The boost is disabled by default (ic_weight = 0.0), which makes the
        multiplier identically 1.0.** This is deliberate, not an oversight: the
        v1.5 pure-semantic move (CHANGELOG 2.7.0) made BioBERT similarity the
        sole ranking signal, and re-ranking by ontology depth on top of it
        pulled overly specific terms above the term a curator actually meant.
        The IC pipeline (`scripts/precompute_go_hierarchy.py`) is still run and
        shipped because this method also attaches the `depth` field that the UI
        displays, and because re-enabling the boost is a one-value config
        change. See #192 and `docs/SCORING_CONFIG.md`.

        Also attaches depth field to each suggestion for UI display.
        """
        go_cfg = ns_data.config
        hierarchy_cfg = getattr(go_cfg, 'hierarchy', {}) if go_cfg else {}
        ic_weight = hierarchy_cfg.get('ic_weight', 0.0) if isinstance(hierarchy_cfg, dict) else 0.0

        for s in suggestions:
            go_id = s.get('go_id', '')
            h = ns_data.hierarchy.get(go_id, {})
            ic_score = h.get('ic_score', 0.0)
            depth = h.get('depth', 0)

            if not h:
                logger.debug("GO term %s not found in hierarchy data", go_id)

            # Apply IC boost
            s['hybrid_score'] *= (1 + ic_weight * ic_score)
            s['hybrid_score'] = round(min(s['hybrid_score'], 0.98), 4)

            # Attach depth for UI display
            s['depth'] = depth

        # Re-sort after boosting
        suggestions.sort(key=lambda x: x['hybrid_score'], reverse=True)
        return suggestions

    def _filter_redundant_ancestors(self, suggestions, ns_data: _NamespaceData, ke_title: str = ""):
        """Remove ancestor GO terms when a more specific descendant is present.

        An ancestor is removed unless its hybrid_score exceeds the child's score
        by more than redundancy_threshold (default 10%). The 10% figure is the
        deployed value in `scoring_config.yaml`; it prunes more aggressively
        than the 20% this defaulted to before #192, which is intended — under
        pure-semantic ranking an ancestor rarely outscores its descendant by a
        wide margin, so the looser threshold left near-duplicate umbrella terms
        in the list.

        Exception: an ancestor whose label exactly matches the (direction-stripped)
        KE title is never pruned. For a generic KE ("Cell death"), the umbrella term
        IS the intended annotation, so a fractionally-higher-scoring descendant
        ("programmed cell death") must not evict it (#193).
        """
        go_cfg = ns_data.config
        hierarchy_cfg = getattr(go_cfg, 'hierarchy', {}) if go_cfg else {}
        threshold = hierarchy_cfg.get('redundancy_threshold', 0.10) if isinstance(hierarchy_cfg, dict) else 0.10

        # Build lookup of suggestion go_ids to their scores + names
        suggestion_ids = {s['go_id'] for s in suggestions}
        score_map = {s['go_id']: s['hybrid_score'] for s in suggestions}
        name_map = {s['go_id']: s.get('go_name', '') for s in suggestions}

        # Terms whose label matches the KE title (exact or whole-word phrase) are
        # protected from pruning — for a generic KE the umbrella term is the answer,
        # so a more-specific descendant must not evict it.
        ke_clean = self._clean_text(remove_directionality_terms(ke_title)) if ke_title else ""
        protected = (
            {gid for gid, nm in name_map.items()
             if self._title_match_kind(ke_clean, self._clean_text(nm))}
            if ke_clean else set()
        )

        # Collect ancestors to remove
        ancestors_to_remove = set()
        for s in suggestions:
            go_id = s['go_id']
            h = ns_data.hierarchy.get(go_id, {})
            ancestors = h.get('ancestors', set())

            for anc_id in ancestors:
                if anc_id not in suggestion_ids:
                    continue
                if anc_id in protected:
                    # exact KE-title match — never redundant
                    continue
                # anc_id is an ancestor of go_id and both are in suggestions
                child_score = score_map[go_id]
                ancestor_score = score_map[anc_id]

                # Keep ancestor only if it scores threshold+ higher than child
                if ancestor_score < child_score * (1 + threshold):
                    ancestors_to_remove.add(anc_id)

        if ancestors_to_remove:
            logger.info("Removing %d redundant ancestor GO terms", len(ancestors_to_remove))

        return [s for s in suggestions if s['go_id'] not in ancestors_to_remove]

    # Title-match / proxy weighting constants (multiplicative, applied to hybrid_score).
    # Consistent with the existing directionality match_boost/mismatch_penalty style.
    _TITLE_EXACT_BOOST = 1.25    # GO label == KE title (direction-stripped)
    _TITLE_NEAR_BOOST = 1.10     # KE title is a whole-word phrase of the label (or vice-versa)
    _REGULATION_PROXY_PENALTY = 0.90  # "regulation of X" — the control layer, not the event
    # Neutral "regulation of X" only. Signed variants are already excluded from the
    # corpus/suggestions. "response to X" is deliberately NOT penalised — it is the
    # canonical process term for stress/stimulus KEs (e.g. response to oxidative stress).
    _REGULATION_PROXY_RE = re.compile(r"^regulation of\b", re.IGNORECASE)

    @staticmethod
    def _title_match_kind(ke_clean: str, name_clean: str):
        """Return 'exact', 'near', or None for a cleaned label vs a cleaned KE title.

        'near' = the KE title is a whole-word phrase of the label, or vice-versa
        (e.g. KE "DNA damage" ~ "DNA damage response").
        """
        if not ke_clean or not name_clean:
            return None
        if name_clean == ke_clean:
            return 'exact'
        if f" {ke_clean} " in f" {name_clean} " or f" {name_clean} " in f" {ke_clean} ":
            return 'near'
        return None

    def _apply_title_and_proxy_weighting(self, suggestions, ke_title: str):
        """Boost KE-title-matching terms; down-rank 'regulation of X' proxies.

        For a generic KE ("Cell death", "Apoptotic process"), the generic process
        term is the intended annotation, but BioBERT similarity tends to float
        specific children and "regulation of X" proxies above it. This stage:
          - multiplies a term's hybrid_score by _TITLE_EXACT_BOOST when its label
            exactly matches the direction-stripped KE title, _TITLE_NEAR_BOOST when
            the title is a whole-word phrase of the label (or vice-versa);
          - multiplies "regulation of X" labels by _REGULATION_PROXY_PENALTY.
        Boost and penalty compose (a "regulation of <title>" term gets both).
        """
        if not ke_title:
            return suggestions
        ke_clean = self._clean_text(remove_directionality_terms(ke_title))
        if not ke_clean:
            return suggestions

        for s in suggestions:
            name = s.get('go_name', '')
            name_clean = self._clean_text(name)
            factor = 1.0
            kind = self._title_match_kind(ke_clean, name_clean)
            if kind == 'exact':
                factor *= self._TITLE_EXACT_BOOST
                s['title_match'] = 'exact'
            elif kind == 'near':
                factor *= self._TITLE_NEAR_BOOST
                s['title_match'] = 'near'
            if self._REGULATION_PROXY_RE.match(name):
                factor *= self._REGULATION_PROXY_PENALTY
                s['regulation_proxy'] = True
            if factor != 1.0:
                s['hybrid_score'] = s.get('hybrid_score', 0.0) * factor

        suggestions.sort(key=lambda x: x['hybrid_score'], reverse=True)
        return suggestions

    def _apply_direction_adjustment(self, suggestions, ke_title, ns_data: _NamespaceData):
        """Apply direction-based score boost or penalty to GO suggestions.

        Detects the KE direction from its title and compares to each GO term's
        direction (from precomputed metadata or runtime fallback). Matching
        directions receive a boost; mismatched directions receive a penalty.
        Unspecified direction on either side → no adjustment.

        Config keys (under go_bp/go_mf .directionality):
            match_boost (default 1.10)
            mismatch_penalty (default 0.85)

        Attaches to each suggestion:
            go_direction: "positive" | "negative" | "unspecified"
            ke_direction: "positive" | "negative" | "unspecified"
            direction_alignment: "match" | "mismatch" | null
        """
        # Read config with safe fallback defaults
        go_cfg = ns_data.config
        dir_cfg = getattr(go_cfg, 'directionality', {}) if go_cfg else {}
        if isinstance(dir_cfg, dict):
            match_boost = dir_cfg.get('match_boost', 1.10)
            mismatch_penalty = dir_cfg.get('mismatch_penalty', 0.85)
        else:
            match_boost = 1.10
            mismatch_penalty = 0.85

        ke_direction = detect_ke_direction(ke_title)

        for s in suggestions:
            go_id = s.get('go_id', '')
            go_name = s.get('go_name', '')

            # Get GO direction from precomputed metadata; fall back to runtime detection
            meta = ns_data.metadata.get(go_id, {})
            if 'direction' in meta:
                go_direction = meta['direction']
            else:
                go_direction = detect_go_direction(go_name)

            s['go_direction'] = go_direction
            s['ke_direction'] = ke_direction

            # Compute alignment and apply score adjustment
            if ke_direction == "unspecified" or go_direction == "unspecified":
                s['direction_alignment'] = None
            elif ke_direction == go_direction:
                s['direction_alignment'] = "match"
                s['hybrid_score'] *= match_boost
                s['hybrid_score'] = round(min(s['hybrid_score'], 0.98), 4)
            else:
                s['direction_alignment'] = "mismatch"
                s['hybrid_score'] *= mismatch_penalty
                s['hybrid_score'] = round(min(s['hybrid_score'], 0.98), 4)

        # Re-sort by hybrid_score descending after adjustments
        suggestions.sort(key=lambda x: x['hybrid_score'], reverse=True)
        return suggestions

    def _make_bp_ns_data(self) -> _NamespaceData:
        """Bundle BP data and config into a _NamespaceData for scoring methods."""
        return _NamespaceData(
            embeddings=self.go_embeddings,
            name_embeddings=self.go_name_embeddings,
            metadata=self.go_metadata,
            annotations=self.go_gene_annotations,
            hierarchy=self.go_hierarchy,
            config=getattr(self.config, 'go_bp', None),
        )

    def _make_mf_ns_data(self) -> _NamespaceData:
        """Bundle MF data and config into a _NamespaceData for scoring methods."""
        return _NamespaceData(
            embeddings=self.go_mf_embeddings,
            name_embeddings=self.go_mf_name_embeddings,
            metadata=self.go_mf_metadata,
            annotations=self.go_mf_gene_annotations,
            hierarchy=self.go_mf_hierarchy,
            config=getattr(self.config, 'go_mf', None),
        )

    def _get_namespace_suggestions(
        self,
        ke_id: str,
        ke_title: str,
        method_filter: str,
        genes: List[str],
        ns_data: _NamespaceData,
    ) -> List[Dict]:
        """Run the full suggestion pipeline for one namespace (BP or MF).

        IC boost and redundancy filtering happen BEFORE the result is returned,
        so cross-namespace contamination is impossible.
        """
        # Compute embedding-based scores
        embedding_scores = []
        if method_filter in ('all', 'text') and self.embedding_service and ns_data.embeddings:
            embedding_scores = self._compute_embedding_scores_for(ke_id, ke_title, ns_data)

        # Compute gene-based scores
        gene_scores = []
        if method_filter in ('all', 'gene') and genes and ns_data.annotations:
            gene_scores = self._compute_gene_overlap_scores_for(genes, ns_data)

        # Combine scores
        if method_filter == 'all':
            combined = self._combine_go_scores_for(embedding_scores, gene_scores, ns_data)
        elif method_filter == 'text':
            combined = embedding_scores
        elif method_filter == 'gene':
            combined = gene_scores
        else:
            combined = self._combine_go_scores_for(embedding_scores, gene_scores, ns_data)

        # Apply hierarchy-based adjustments if available (per-namespace)
        if ns_data.hierarchy:
            go_cfg = ns_data.config
            hierarchy_cfg = getattr(go_cfg, 'hierarchy', {}) if go_cfg else {}
            if isinstance(hierarchy_cfg, dict) and hierarchy_cfg.get('enabled', True):
                combined = self._apply_ic_boost(combined, ns_data)
                combined = self._filter_redundant_ancestors(combined, ns_data, ke_title)

        # Apply direction-based score adjustments (boost/penalty)
        combined = self._apply_direction_adjustment(combined, ke_title, ns_data)

        # Surface the generic process term for generic KEs: boost terms whose label
        # matches the KE title and down-rank "regulation of X" control-layer proxies
        # so the process itself outranks its regulation (#193, KE->GO skill Rule 5).
        combined = self._apply_title_and_proxy_weighting(combined, ke_title)

        # Never suggest signed/directional GO terms — direction belongs in the KE's
        # PATO Action slot, not the GO Process term (#193). This runtime guard holds
        # even before the corpus is rebuilt to exclude them at the source.
        combined = [
            s for s in combined
            if not is_directional_go_label(s.get('go_name', ''))
        ]

        return combined

    def get_go_suggestions(
        self,
        ke_id: str,
        ke_title: str,
        limit: int = 20,
        method_filter: str = 'all',
        aspect_filter: str = 'all',
    ) -> Dict:
        """
        Get GO term suggestions (BP and/or MF) for a Key Event.

        Args:
            ke_id: Key Event ID (e.g., "KE 55")
            ke_title: Key Event title for text-based matching
            limit: Maximum number of suggestions to return
            method_filter: 'all', 'text', or 'gene'
            aspect_filter: 'all', 'bp', or 'mf'

        Returns:
            Dictionary containing suggestions with scores and go_namespace field on each.
        """
        try:
            logger.info(
                "Getting GO suggestions for %s (method: %s, aspect: %s)",
                ke_id, method_filter, aspect_filter,
            )

            # Get genes associated with this KE (shared across namespaces)
            genes = self._get_genes_from_ke(ke_id)

            bp_ns = self._make_bp_ns_data()
            mf_ns = self._make_mf_ns_data()

            # --- BP namespace ---
            bp_results = self._get_namespace_suggestions(ke_id, ke_title, method_filter, genes, bp_ns)
            for s in bp_results:
                s['go_namespace'] = 'BP'

            # --- MF namespace (only when data is loaded) ---
            mf_results = []
            if self.go_mf_metadata:
                mf_results = self._get_namespace_suggestions(ke_id, ke_title, method_filter, genes, mf_ns)
                for s in mf_results:
                    s['go_namespace'] = 'MF'

            # Merge and apply aspect filter
            combined = bp_results + mf_results
            if aspect_filter == 'bp':
                combined = [s for s in combined if s['go_namespace'] == 'BP']
            elif aspect_filter == 'mf':
                combined = [s for s in combined if s['go_namespace'] == 'MF']

            # Sort merged list by hybrid_score, then apply limit
            combined.sort(key=lambda x: x['hybrid_score'], reverse=True)
            limited = combined[:limit]

            return {
                "ke_id": ke_id,
                "ke_title": ke_title,
                "genes_found": len(genes),
                "gene_list": [g["symbol"] for g in genes],
                "gene_list_full": genes,
                "suggestions": limited,
                "total_suggestions": len(combined),
                "method_filter": method_filter,
                "aspect_filter": aspect_filter,
            }

        except Exception as e:
            logger.error("Error getting GO suggestions for %s: %s", ke_id, e)
            return {
                "error": "Failed to generate GO suggestions",
                "ke_id": ke_id,
                "ke_title": ke_title,
            }

    def _search_metadata(self, metadata_dict: dict, query_clean: str, threshold: float, namespace: str) -> List[Dict]:
        """Run fuzzy search over a metadata dict and tag results with namespace."""
        results = []
        for go_id, metadata in metadata_dict.items():
            go_name = metadata.get('name', '')
            go_definition = metadata.get('definition', '')

            # Signed/directional terms are never valid KE Process terms (#193).
            if is_directional_go_label(go_name):
                continue

            name_clean = self._clean_text(go_name)
            name_similarity = SequenceMatcher(None, query_clean, name_clean).ratio()

            # Exact/whole-word/substring boost so an exact label match wins over a
            # fuzzy near-miss (e.g. "cell death" must beat "cell growth"), with the
            # fuzzy ratio kept only as a tiebreaker. Mirrors the Reactome search
            # substring boost, extended with exact + whole-word tiers (#193).
            if name_clean:
                if query_clean == name_clean:
                    name_similarity = 1.0
                elif f" {query_clean} " in f" {name_clean} ":
                    name_similarity = max(name_similarity, 0.92)
                elif query_clean in name_clean:
                    name_similarity = max(name_similarity, 0.85)

            def_similarity = 0.0
            if go_definition:
                def_clean = self._clean_text(go_definition)
                def_similarity = SequenceMatcher(None, query_clean, def_clean).ratio()

            max_similarity = max(name_similarity, def_similarity)

            if max_similarity >= threshold:
                results.append({
                    'go_id': go_id,
                    'go_name': go_name,
                    'go_definition': go_definition,
                    'go_namespace': namespace,
                    'name_similarity': round(name_similarity, 3),
                    'definition_similarity': round(def_similarity, 3),
                    'relevance_score': round(max_similarity, 3),
                    'quickgo_link': f"https://www.ebi.ac.uk/QuickGO/term/{go_id}",
                    **self._gene_counts_for(go_id, namespace),
                })
        return results

    def _gene_counts_for(self, go_id: str, namespace: str) -> Dict:
        """Gene-set size fields for a GO term, for search and suggestion rows.

        Curators need this at the moment of choosing (#210): a mapping can be
        semantically perfect and still leave its Key Event untestable, because
        the Analyser refuses to test a KE resolving to fewer than five genes.
        KE 1097 -> GO:0097300 is the worked example — the correct term, five
        genes, three of them measured, and the Key Event silently excluded from
        every analysis since. Nothing in the UI warned about it.

        Both numbers are reported. `go_gene_count` is the propagated count, the
        one that governs testability; `go_gene_count_direct` is what is
        annotated to the term itself. A term with 891 propagated and 7 direct is
        well-populated but only indirectly evidenced, and hiding either number
        misleads — showing the direct count alone is what made correct general
        terms look untestable before #208.
        """
        attr = 'go_gene_annotations' if namespace == 'BP' else 'go_mf_gene_annotations'
        # getattr, not attribute access: this runs inside a search serializer and
        # must never turn a missing corpus into a failed search. Some tests also
        # build the service without the annotation dicts.
        annotations = getattr(self, attr, None) or {}
        counts = {'go_gene_count': len(annotations.get(go_id, []))}
        try:
            from src.services.go_annotation_index import get_go_direct_counts
            direct = get_go_direct_counts(namespace.lower())
            counts['go_gene_count_direct'] = direct.get(go_id, 0)
        except Exception as e:
            # A missing direct file costs the parenthetical, not the feature.
            logger.debug("Direct GO %s counts unavailable: %s", namespace, e)
        return counts

    def search_go_terms(
        self, query: str, threshold: float = 0.4, limit: int = 10
    ) -> List[Dict]:
        """
        Search GO terms (BP and MF) using SequenceMatcher fuzzy matching.

        Args:
            query: Search query string
            threshold: Minimum similarity threshold (0.0-1.0)
            limit: Maximum number of results

        Returns:
            List of matching GO terms with relevance scores and go_namespace field.
        """
        try:
            # Check for GO ID pattern (e.g. GO:0006915, go:0006915, GO_0006915)
            go_id_match = re.match(r'^(GO)[:\-_]?(\d{4,7})$', query.strip(), re.IGNORECASE)
            if go_id_match:
                normalized = f"GO:{go_id_match.group(2).zfill(7)}"
                for meta_dict, ns in [
                    (self.go_metadata, 'BP'),
                    (self.go_mf_metadata, 'MF'),
                ]:
                    if normalized in meta_dict:
                        meta = meta_dict[normalized]
                        return [{
                            'go_id': normalized,
                            'go_name': meta.get('name', ''),
                            'go_definition': meta.get('definition', ''),
                            'go_namespace': ns,
                            'name_similarity': 1.0,
                            'definition_similarity': 1.0,
                            'relevance_score': 1.0,
                            'quickgo_link': f"https://www.ebi.ac.uk/QuickGO/term/{normalized}",
                            **self._gene_counts_for(normalized, ns),
                        }]
                return []

            query_clean = self._clean_text(query)
            if not query_clean:
                return []

            # Search BP metadata
            results = self._search_metadata(self.go_metadata, query_clean, threshold, 'BP')

            # Search MF metadata (if loaded)
            if self.go_mf_metadata:
                results.extend(self._search_metadata(self.go_mf_metadata, query_clean, threshold, 'MF'))

            results.sort(key=lambda x: x['relevance_score'], reverse=True)
            return results[:limit]

        except Exception as e:
            logger.error("Error in GO term search: %s", e)
            return []

    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean and normalize text for comparison."""
        if not text:
            return ""
        cleaned = re.sub(r"[^\w\s]", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip().lower()

    def _get_genes_from_ke(self, ke_id: str) -> List[Dict[str, str]]:
        """Extract gene identifier triples ({ncbi, hgnc, symbol}) for a Key Event."""
        return get_genes_from_ke(ke_id, self.aop_wiki_endpoint, self.cache_model)

    def _compute_embedding_scores_for(self, ke_id: str, ke_title: str, ns_data: _NamespaceData) -> List[Dict]:
        """
        Compute embedding-based similarity between KE and all GO terms in a namespace.

        Uses split name/definition embeddings with weighted combination.
        Falls back to combined-only if name embeddings are not available.
        """
        if not self.embedding_service or not ns_data.embeddings:
            return []

        try:
            # Clean KE title
            ke_title_clean = remove_directionality_terms(ke_title)

            # Resolve description toggle: global config + per-KE overrides
            go_config = ns_data.config
            global_toggle = getattr(go_config, 'use_ke_description', True) if go_config else True
            disabled_kes = self.ke_override_model.get_disabled_ke_ids() if self.ke_override_model else set()
            use_desc = resolve_description_usage(ke_id, global_toggle, disabled_kes)
            logger.debug("GO embedding toggle: global=%s, ke_disabled=%s, use_desc=%s",
                         global_toggle, ke_id in disabled_kes, use_desc)

            # Get KE embedding (toggle-aware: selects title-only or title+description)
            ke_emb = self.embedding_service.get_ke_embedding_for_matching(ke_id, ke_title_clean, use_description=use_desc)

            min_threshold = getattr(go_config, 'embedding_min_threshold', 0.3) if go_config else 0.3
            name_weight = getattr(go_config, 'name_weight', 0.60) if go_config else 0.60
            def_weight = 1.0 - name_weight

            ke_norm = np.linalg.norm(ke_emb)

            # Use split name + definition embeddings if available
            if ns_data.name_embeddings:
                go_ids = [gid for gid in ns_data.embeddings.keys()
                          if gid in ns_data.name_embeddings]

                name_emb_array = np.array([ns_data.name_embeddings[gid] for gid in go_ids])
                def_emb_array = np.array([ns_data.embeddings[gid] for gid in go_ids])

                raw_name_sim = np.dot(name_emb_array, ke_emb) / (
                    np.linalg.norm(name_emb_array, axis=1) * ke_norm + 1e-8)
                raw_def_sim = np.dot(def_emb_array, ke_emb) / (
                    np.linalg.norm(def_emb_array, axis=1) * ke_norm + 1e-8)

                transformed_name = self.embedding_service._transform_similarity_batch(raw_name_sim)
                transformed_def = self.embedding_service._transform_similarity_batch(raw_def_sim)

                combined = (transformed_name * name_weight) + (transformed_def * def_weight)
                logger.info("Split embedding scoring: %.0f%% name + %.0f%% definition", name_weight * 100, def_weight * 100)
            else:
                go_ids = list(ns_data.embeddings.keys())
                go_emb_array = np.array([ns_data.embeddings[gid] for gid in go_ids])
                go_norms = np.linalg.norm(go_emb_array, axis=1)
                raw_similarities = np.dot(go_emb_array, ke_emb) / (go_norms * ke_norm + 1e-8)
                combined = self.embedding_service._transform_similarity_batch(raw_similarities)
                transformed_name = None
                transformed_def = None

            results = []
            for i, go_id in enumerate(go_ids):
                score = float(combined[i])
                if score < min_threshold:
                    continue

                metadata = ns_data.metadata.get(go_id, {})
                result = {
                    'go_id': go_id,
                    'go_name': metadata.get('name', 'Unknown'),
                    'go_definition': metadata.get('definition', ''),
                    'synonyms': metadata.get('synonyms', []),
                    'text_similarity': score,
                    'gene_overlap': 0.0,
                    'matching_genes': [],
                    'hybrid_score': score,
                    'match_types': ['text'],
                    'quickgo_link': f"https://www.ebi.ac.uk/QuickGO/term/{go_id}",
                    'go_gene_count': len(ns_data.annotations.get(go_id, []))
                }

                if transformed_name is not None:
                    result['name_similarity'] = round(float(transformed_name[i]), 4)
                    result['definition_similarity'] = round(float(transformed_def[i]), 4)

                results.append(result)

            logger.info("Found %d embedding-based GO suggestions", len(results))
            return results

        except Exception as e:
            logger.error("Embedding-based GO suggestion failed: %s", e)
            return []

    def _compute_gene_overlap_scores_for(self, ke_genes: List[Dict[str, str]], ns_data: _NamespaceData) -> List[Dict]:
        """
        Compute gene overlap between KE genes and GO term gene annotations for a namespace.

        Uses weighted KE overlap + Jaccard similarity with dampening for small terms.
        """
        if not ke_genes or not ns_data.annotations:
            return []

        try:
            go_config = ns_data.config
            min_threshold = getattr(go_config, 'gene_min_threshold', 0.05) if go_config else 0.05
            min_term_size = getattr(go_config, 'gene_min_term_size', 10) if go_config else 10

            # GO annotations (GAF-derived JSON) are HGNC-symbol-keyed. Intersect on
            # the symbol field of each gene dict.
            ke_gene_set = {g['symbol'] for g in ke_genes}
            results = []

            # Restrict to the suggestion corpus. The annotations dict is a
            # superset of it — 7628 of the 11207 annotated BP terms are outside
            # go_bp_metadata.json and would render with go_name "Unknown" — and
            # propagation (#208) both widens every gene set and adds terms, so
            # scanning the whole dict got materially more expensive for results
            # that were never surfaceable.
            for go_id in ns_data.metadata:
                go_genes = ns_data.annotations.get(go_id)
                if not go_genes:
                    continue
                go_gene_set = set(go_genes)
                matching = ke_gene_set.intersection(go_gene_set)
                if not matching:
                    continue

                union = ke_gene_set.union(go_gene_set)
                jaccard = len(matching) / len(union) if union else 0.0
                ke_overlap = len(matching) / len(ke_gene_set) if ke_gene_set else 0.0
                gene_score = (ke_overlap * 0.7) + (jaccard * 0.3)

                go_size = len(go_gene_set)
                if go_size < min_term_size:
                    gene_score *= go_size / min_term_size

                if gene_score < min_threshold:
                    continue

                metadata = ns_data.metadata.get(go_id, {})
                results.append({
                    'go_id': go_id,
                    'go_name': metadata.get('name', 'Unknown'),
                    'go_definition': metadata.get('definition', ''),
                    'synonyms': metadata.get('synonyms', []),
                    'text_similarity': 0.0,
                    'gene_overlap': round(gene_score, 4),
                    'matching_genes': sorted(list(matching)),
                    'hybrid_score': gene_score,
                    'match_types': ['gene'],
                    'quickgo_link': f"https://www.ebi.ac.uk/QuickGO/term/{go_id}",
                    'go_gene_count': go_size
                })

            logger.info("Found %d gene-based GO suggestions", len(results))
            return results

        except Exception as e:
            logger.error("Gene overlap GO suggestion failed: %s", e)
            return []

    def _combine_go_scores_for(
        self,
        embedding_scores: List[Dict],
        gene_scores: List[Dict],
        ns_data: _NamespaceData,
    ) -> List[Dict]:
        """
        Combine embedding and gene scores with hybrid weighting for a namespace.

        Returns merged list with hybrid_score computed from both signals.
        """
        go_config = ns_data.config
        if go_config:
            weights_cfg = getattr(go_config, 'hybrid_weights', {})
            if isinstance(weights_cfg, dict):
                emb_weight = weights_cfg.get('embedding', 0.55)
                gene_weight = weights_cfg.get('gene', 0.0)
                # Default is 0.0 (v1.5 pure-semantic); v1.4 YAML had 0.05
                bonus = weights_cfg.get('multi_evidence_bonus', 0.0)
            else:
                emb_weight = 0.55
                gene_weight = 0.0
                bonus = 0.0
        else:
            emb_weight = 0.55
            gene_weight = 0.0
            bonus = 0.0

        min_threshold = getattr(go_config, 'min_threshold', 0.15) if go_config else 0.15

        if not self._v15_logged:
            logger.info(
                "GO BP/MF ranking: pure-semantic v1.5 (embedding=%.2f, gene=%.2f, bonus=%.2f)",
                emb_weight, gene_weight, bonus,
            )
            self._v15_logged = True

        results = combine_scored_items(
            scored_lists={'text': embedding_scores, 'gene': gene_scores},
            id_field='go_id',
            weights={'text': emb_weight, 'gene': gene_weight},
            score_field_map={'text': 'text_similarity', 'gene': 'gene_overlap'},
            multi_evidence_bonus=bonus,
            min_threshold=min_threshold,
        )

        # Restore per-signal scores and gene data from signal_scores / _signal_data
        for item in results:
            sig = item.pop('signal_scores', {})
            sig_data = item.pop('_signal_data', {})
            item['text_similarity'] = round(sig.get('text', 0.0), 4)
            item['gene_overlap'] = round(sig.get('gene', 0.0), 4)

            gene_data = sig_data.get('gene', {})
            if gene_data.get('matching_genes'):
                item['matching_genes'] = gene_data['matching_genes']
            if gene_data.get('go_gene_count'):
                item['go_gene_count'] = gene_data['go_gene_count']

            emb_data = sig_data.get('text', {})
            if emb_data.get('name_similarity') is not None:
                item['name_similarity'] = emb_data['name_similarity']
                item['definition_similarity'] = emb_data.get('definition_similarity', 0.0)

        return results
