"""
Reactome Pathway Suggestion Service
Provides ranked Reactome pathway suggestions for Key Events using
pre-computed BioBERT embeddings and gene annotation overlap.
"""
import json
import logging
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List

import numpy as np
from src.core.config_loader import ConfigLoader
from src.suggestions.ke_genes import get_genes_from_ke
from src.suggestions.scoring import combine_scored_items
from src.utils.description_toggle import resolve_description_usage
from src.utils.text import remove_directionality_terms

logger = logging.getLogger(__name__)


class ReactomeSuggestionService:
    """Service for generating Reactome pathway suggestions for Key Events.

    Mirrors the structure of GoSuggestionService but is intentionally simpler:
    - Single namespace (no BP/MF split)
    - No information-content (IC) boost (Reactome has no DAG hierarchy here)
    - No direction adjustment (Reactome pathways are not directional)
    - No redundant-ancestor filtering
    - No fuzzy term search
    """

    def __init__(
        self,
        cache_model=None,
        config=None,
        embedding_service=None,
        ke_override_model=None,
        reactome_embeddings_path: str = 'data/reactome_pathway_embeddings.npz',
        reactome_name_embeddings_path: str = 'data/reactome_pathway_name_embeddings.npz',
        reactome_metadata_path: str = 'data/reactome_pathway_metadata.json',
        reactome_annotations_path: str = 'data/reactome_gene_annotations.json',
    ):
        self.cache_model = cache_model
        self.config = config or ConfigLoader.get_default_config()
        self.embedding_service = embedding_service
        self.ke_override_model = ke_override_model
        self.aop_wiki_endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

        # Pre-computed Reactome data — keyed by R-HSA-* stable IDs
        self.reactome_embeddings: Dict[str, np.ndarray] = {}
        self.reactome_name_embeddings: Dict[str, np.ndarray] = {}
        self.reactome_metadata: Dict[str, dict] = {}
        self.reactome_gene_annotations: Dict[str, list] = {}

        self._load_npz_into(
            reactome_embeddings_path, self.reactome_embeddings, 'Reactome embeddings'
        )
        self._load_npz_into(
            reactome_name_embeddings_path,
            self.reactome_name_embeddings,
            'Reactome name embeddings',
        )
        self._load_json_into(
            reactome_metadata_path, self.reactome_metadata, 'Reactome metadata'
        )
        self._load_json_into(
            reactome_annotations_path,
            self.reactome_gene_annotations,
            'Reactome annotations',
        )

    # ------------------------------------------------------------------
    # Data loading helpers (parity with GoSuggestionService)
    # ------------------------------------------------------------------

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

    def _get_genes_from_ke(self, ke_id: str) -> List[Dict[str, str]]:
        """Extract gene identifier triples ({ncbi, hgnc, symbol}) for a Key Event."""
        return get_genes_from_ke(ke_id, self.aop_wiki_endpoint, self.cache_model)

    # ------------------------------------------------------------------
    # Scoring methods
    # ------------------------------------------------------------------

    def _compute_embedding_scores(self, ke_id: str, ke_title: str) -> List[Dict]:
        """Compute embedding-based similarity between KE and Reactome pathways.

        Uses split name/definition embeddings with weighted combination.
        Falls back to combined-only if name embeddings are not available.
        """
        if not self.embedding_service or not self.reactome_embeddings:
            return []

        try:
            reactome_cfg = getattr(self.config, 'reactome_suggestion', None)

            min_threshold = (
                getattr(reactome_cfg, 'embedding_min_threshold', 0.3)
                if reactome_cfg else 0.3
            )
            name_weight = (
                getattr(reactome_cfg, 'name_weight', 0.85)
                if reactome_cfg else 0.85
            )
            def_weight = 1.0 - name_weight

            # Clean KE title (strip directional terms)
            ke_title_clean = remove_directionality_terms(ke_title)

            # Resolve description toggle: global config + per-KE overrides
            global_toggle = (
                getattr(reactome_cfg, 'use_ke_description', True)
                if reactome_cfg else True
            )
            disabled_kes = (
                self.ke_override_model.get_disabled_ke_ids()
                if self.ke_override_model else set()
            )
            use_desc = resolve_description_usage(ke_id, global_toggle, disabled_kes)
            logger.debug(
                "Reactome embedding toggle: global=%s, ke_disabled=%s, use_desc=%s",
                global_toggle, ke_id in disabled_kes, use_desc,
            )

            # Use split name + definition embeddings if available.
            # Mirror the WP path (src/services/embedding.py:565-660): pair
            # the name channel (name-only pathway embedding) with a
            # title-only KE embedding, and pair the description channel
            # (name+description joint pathway embedding) with the
            # toggle-aware full KE embedding. This avoids the asymmetry
            # of comparing a title-only pathway vector against a
            # title+description KE vector and vice-versa.
            if self.reactome_name_embeddings:
                ke_name_emb = self.embedding_service.get_ke_embedding_for_matching(
                    ke_id, ke_title_clean, use_description=False
                )
                ke_full_emb = self.embedding_service.get_ke_embedding_for_matching(
                    ke_id, ke_title_clean, use_description=use_desc
                )
                ke_name_norm = np.linalg.norm(ke_name_emb)
                ke_full_norm = np.linalg.norm(ke_full_emb)

                pathway_ids = [
                    rid for rid in self.reactome_embeddings.keys()
                    if rid in self.reactome_name_embeddings
                ]

                name_emb_array = np.array(
                    [self.reactome_name_embeddings[rid] for rid in pathway_ids]
                )
                def_emb_array = np.array(
                    [self.reactome_embeddings[rid] for rid in pathway_ids]
                )

                raw_name_sim = np.dot(name_emb_array, ke_name_emb) / (
                    np.linalg.norm(name_emb_array, axis=1) * ke_name_norm + 1e-8
                )
                raw_def_sim = np.dot(def_emb_array, ke_full_emb) / (
                    np.linalg.norm(def_emb_array, axis=1) * ke_full_norm + 1e-8
                )

                transformed_name = self.embedding_service._transform_similarity_batch(
                    raw_name_sim
                )
                transformed_def = self.embedding_service._transform_similarity_batch(
                    raw_def_sim
                )

                combined = (transformed_name * name_weight) + (
                    transformed_def * def_weight
                )
                logger.info(
                    "Reactome split embedding scoring: %.0f%% name + %.0f%% definition",
                    name_weight * 100, def_weight * 100,
                )
            else:
                # Fallback: only the joint name+description embedding is
                # available. Use a single toggle-aware KE embedding against it.
                ke_emb = self.embedding_service.get_ke_embedding_for_matching(
                    ke_id, ke_title_clean, use_description=use_desc
                )
                ke_norm = np.linalg.norm(ke_emb)

                pathway_ids = list(self.reactome_embeddings.keys())
                emb_array = np.array(
                    [self.reactome_embeddings[rid] for rid in pathway_ids]
                )
                emb_norms = np.linalg.norm(emb_array, axis=1)
                raw_similarities = np.dot(emb_array, ke_emb) / (
                    emb_norms * ke_norm + 1e-8
                )
                combined = self.embedding_service._transform_similarity_batch(
                    raw_similarities
                )
                transformed_name = None
                transformed_def = None

            results = []
            for i, reactome_id in enumerate(pathway_ids):
                score = float(combined[i])
                if score < min_threshold:
                    continue

                metadata = self.reactome_metadata.get(reactome_id, {})
                pathway_size = len(
                    self.reactome_gene_annotations.get(reactome_id, [])
                )

                result = {
                    'reactome_id': reactome_id,
                    'pathway_name': metadata.get('name', 'Unknown'),
                    'pathway_description': metadata.get('description', ''),
                    'text_similarity': score,
                    'gene_overlap': 0.0,
                    'matching_genes': [],
                    'hybrid_score': score,
                    'match_types': ['text'],
                    'reactome_pathway_gene_count': pathway_size,
                }

                if transformed_name is not None:
                    result['name_similarity'] = round(float(transformed_name[i]), 4)
                    result['definition_similarity'] = round(
                        float(transformed_def[i]), 4
                    )

                results.append(result)

            logger.info("Found %d embedding-based Reactome suggestions", len(results))
            return results

        except Exception as e:
            logger.error("Embedding-based Reactome suggestion failed: %s", e)
            return []

    def _compute_gene_overlap_scores(self, ke_genes: List[Dict[str, str]]) -> List[Dict]:
        """Compute gene overlap between KE genes and Reactome pathway annotations.

        Uses weighted KE overlap + Jaccard similarity with dampening for small
        pathways (gene_score *= pathway_size / min_term_size when pathway_size
        is below the configured minimum).
        """
        if not ke_genes or not self.reactome_gene_annotations:
            return []

        try:
            reactome_cfg = getattr(self.config, 'reactome_suggestion', None)

            min_threshold = (
                getattr(reactome_cfg, 'gene_min_threshold', 0.05)
                if reactome_cfg else 0.05
            )
            min_term_size = (
                getattr(reactome_cfg, 'gene_min_term_size', 10)
                if reactome_cfg else 10
            )

            # Reactome annotations are HGNC-symbol-keyed (Phase 23 — sourced from
            # ReactomePathways.gmt which carries symbols only). Intersect on the
            # symbol field of each gene dict.
            ke_gene_set = {g['symbol'] for g in ke_genes}
            results = []

            for reactome_id, pathway_genes in self.reactome_gene_annotations.items():
                pathway_gene_set = set(pathway_genes)
                matching = ke_gene_set.intersection(pathway_gene_set)
                if not matching:
                    continue

                union = ke_gene_set.union(pathway_gene_set)
                jaccard = len(matching) / len(union) if union else 0.0
                ke_overlap = (
                    len(matching) / len(ke_gene_set) if ke_gene_set else 0.0
                )
                gene_score = (ke_overlap * 0.7) + (jaccard * 0.3)

                pathway_size = len(pathway_gene_set)
                if pathway_size < min_term_size:
                    gene_score *= pathway_size / min_term_size

                if gene_score < min_threshold:
                    continue

                metadata = self.reactome_metadata.get(reactome_id, {})
                results.append({
                    'reactome_id': reactome_id,
                    'pathway_name': metadata.get('name', 'Unknown'),
                    'pathway_description': metadata.get('description', ''),
                    'text_similarity': 0.0,
                    'gene_overlap': round(gene_score, 4),
                    'matching_genes': sorted(list(matching)),
                    'hybrid_score': gene_score,
                    'match_types': ['gene'],
                    'reactome_pathway_gene_count': pathway_size,
                })

            logger.info("Found %d gene-based Reactome suggestions", len(results))
            return results

        except Exception as e:
            logger.error("Gene overlap Reactome suggestion failed: %s", e)
            return []

    def _combine_reactome_scores(
        self,
        embedding_scores: List[Dict],
        gene_scores: List[Dict],
    ) -> List[Dict]:
        """Combine embedding and gene scores with hybrid weighting.

        v1.5: embedding weight=1.0, gene weight=0.0, multi_evidence_bonus=0.0.
        Gene overlap data is still computed and attached to each item for the
        frontend chip (gene_overlap, matching_genes, reactome_pathway_gene_count)
        but does not influence rank order.

        Returns a merged list with hybrid_score computed from both signals via
        the shared combine_scored_items helper, then post-processes results to
        restore per-signal scores and gene overlap data.
        """
        reactome_cfg = getattr(self.config, 'reactome_suggestion', None)

        if reactome_cfg:
            weights_cfg = getattr(reactome_cfg, 'hybrid_weights', {})
            if isinstance(weights_cfg, dict):
                emb_weight = weights_cfg.get('embedding', 0.60)
                gene_weight = weights_cfg.get('gene', 0.40)
                bonus = weights_cfg.get('multi_evidence_bonus', 0.0)
            else:
                emb_weight = 0.60
                gene_weight = 0.40
                bonus = 0.0
        else:
            emb_weight = 0.60
            gene_weight = 0.40
            bonus = 0.0

        if not getattr(self, '_v15_logged', False):
            logger.info(
                "Reactome ranking: pure-semantic v1.5 (embedding=%.2f, gene=%.2f, bonus=%.2f)",
                emb_weight, gene_weight, bonus,
            )
            self._v15_logged = True

        min_threshold = (
            getattr(reactome_cfg, 'min_threshold', 0.15)
            if reactome_cfg else 0.15
        )

        results = combine_scored_items(
            scored_lists={'text': embedding_scores, 'gene': gene_scores},
            id_field='reactome_id',
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
            if gene_data.get('reactome_pathway_gene_count'):
                item['reactome_pathway_gene_count'] = gene_data[
                    'reactome_pathway_gene_count'
                ]

            emb_data = sig_data.get('text', {})
            if emb_data.get('name_similarity') is not None:
                item['name_similarity'] = emb_data['name_similarity']
                item['definition_similarity'] = emb_data.get(
                    'definition_similarity', 0.0
                )

        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_reactome_suggestions(
        self,
        ke_id: str,
        ke_title: str,
        limit: int = 20,
        method_filter: str = 'all',
    ) -> Dict:
        """Get ranked Reactome pathway suggestions for a Key Event.

        Args:
            ke_id: Key Event ID (e.g., "Event:123")
            ke_title: Key Event title for text-based matching
            limit: Maximum number of suggestions to return
            method_filter: 'all', 'text', or 'gene'

        Returns:
            Dictionary containing ranked suggestions with hybrid_score and
            per-signal evidence (text_similarity, gene_overlap, matching_genes).
        """
        try:
            logger.info(
                "Getting Reactome suggestions for %s (method: %s)",
                ke_id, method_filter,
            )

            # Get genes associated with this KE (shared across signals)
            genes = self._get_genes_from_ke(ke_id)

            reactome_cfg = getattr(self.config, 'reactome_suggestion', None)
            max_results = (
                getattr(reactome_cfg, 'max_results', 20)
                if reactome_cfg else 20
            )
            effective_limit = min(limit, max_results)

            # Compute embedding-based scores
            embedding_scores: List[Dict] = []
            if (
                method_filter in ('all', 'text')
                and self.embedding_service
                and self.reactome_embeddings
            ):
                embedding_scores = self._compute_embedding_scores(ke_id, ke_title)

            # Compute gene-overlap scores
            gene_scores: List[Dict] = []
            if (
                method_filter in ('all', 'gene')
                and genes
                and self.reactome_gene_annotations
            ):
                gene_scores = self._compute_gene_overlap_scores(genes)

            # Combine signals
            if method_filter == 'all':
                combined = self._combine_reactome_scores(
                    embedding_scores, gene_scores
                )
            elif method_filter == 'text':
                combined = embedding_scores
            elif method_filter == 'gene':
                combined = gene_scores
            else:
                combined = self._combine_reactome_scores(
                    embedding_scores, gene_scores
                )

            # Sort by hybrid_score descending and apply effective limit
            combined.sort(key=lambda x: x['hybrid_score'], reverse=True)
            limited = combined[:effective_limit]

            return {
                "ke_id": ke_id,
                "ke_title": ke_title,
                "genes_found": len(genes),
                "gene_list": [g["symbol"] for g in genes],
                "gene_list_full": genes,
                "suggestions": limited,
                "total_suggestions": len(combined),
                "method_filter": method_filter,
            }

        except Exception as e:
            logger.error("Error getting Reactome suggestions for %s: %s", ke_id, e)
            return {
                "error": "Failed to generate Reactome suggestions",
                "ke_id": ke_id,
                "ke_title": ke_title,
            }

    def _gene_count_for(self, reactome_id: str) -> int:
        """Resolved gene-set size for a pathway, for search rows (#210).

        /suggest_reactome already emits this under the same key; search did not,
        so a curator arriving via keyword search — which is how the correct
        pathways had to be found while #209 was open — saw no size at all.

        Note this can never fall below the Analyser's five-gene testability
        floor: scripts/download_reactome_annotations.py filters the corpus at
        MIN_GENES = 10. It is informational here, unlike the GO equivalent.
        """
        annotations = getattr(self, "reactome_gene_annotations", None) or {}
        return len(annotations.get(reactome_id, []))

    def search_reactome_terms(
        self, query: str, threshold: float = 0.4, limit: int = 10
    ) -> List[Dict]:
        """Search Reactome pathways using SequenceMatcher fuzzy matching.

        Mirrors GoSuggestionService.search_go_terms (src/suggestions/go.py)
        but searches the in-memory ``self.reactome_metadata`` dict loaded from
        ``data/reactome_pathway_metadata.json`` (Phase 23 output).

        Scoring formula (Phase 25-01):
            name_sim   = SequenceMatcher(query, name).ratio()
                         max-boosted to 0.85 if query is a substring of name
            desc_sim   = SequenceMatcher(query, description[:200]).ratio()
            relevance  = max(name_sim, 0.5 * desc_sim)

        Returns list of dicts (sorted descending by relevance_score, capped
        at ``limit``) with keys: reactome_id, pathway_name, species,
        description, name_similarity, relevance_score.
        """
        try:
            if not query or not query.strip():
                return []

            # Direct ID lookup branch (R-HSA-NNNN; tolerates separators).
            id_match = re.match(
                r"^R[-_]?HSA[-_]?(\d+)$", query.strip(), re.IGNORECASE
            )
            if id_match:
                normalized = f"R-HSA-{id_match.group(1)}"
                if normalized in self.reactome_metadata:
                    meta = self.reactome_metadata[normalized]
                    return [{
                        "reactome_id": normalized,
                        "pathway_name": meta.get(
                            "name", meta.get("pathway_name", "")
                        ),
                        "species": meta.get("species", "Homo sapiens"),
                        "description": meta.get("description", ""),
                        "name_similarity": 1.0,
                        "relevance_score": 1.0,
                        "reactome_pathway_gene_count": self._gene_count_for(normalized),
                    }]
                return []

            query_clean = query.strip().lower()
            results: List[Dict] = []
            for rid, meta in self.reactome_metadata.items():
                name = (meta.get("name") or meta.get("pathway_name") or "").lower()
                desc = (meta.get("description") or "").lower()
                name_sim = (
                    SequenceMatcher(None, query_clean, name).ratio() if name else 0.0
                )
                # Substring boost mirrors search_go_terms behavior.
                if name and query_clean in name:
                    name_sim = max(name_sim, 0.85)
                desc_sim = (
                    SequenceMatcher(None, query_clean, desc[:200]).ratio()
                    if desc else 0.0
                )
                relevance = max(name_sim, 0.5 * desc_sim)
                if relevance >= threshold:
                    results.append({
                        "reactome_id": rid,
                        "pathway_name": meta.get(
                            "name", meta.get("pathway_name", "")
                        ),
                        "species": meta.get("species", "Homo sapiens"),
                        "description": meta.get("description", ""),
                        "name_similarity": round(name_sim, 4),
                        "relevance_score": round(relevance, 4),
                        "reactome_pathway_gene_count": self._gene_count_for(rid),
                    })

            results.sort(key=lambda x: x["relevance_score"], reverse=True)
            return results[:limit]
        except Exception as e:
            logger.error("Error in Reactome term search: %s", e)
            return []
