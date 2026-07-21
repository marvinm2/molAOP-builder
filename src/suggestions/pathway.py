"""
Pathway Suggestion Service
Provides intelligent pathway suggestions based on Key Events using AOP-Wiki and WikiPathways RDF data
"""
import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Dict, List

import requests
from src import PROJECT_ROOT
from src.core.config_loader import ConfigLoader
from src.suggestions.ke_genes import get_genes_from_ke
from src.suggestions.scoring import combine_scored_items
from src.utils.description_toggle import resolve_description_usage
from src.utils.text import remove_directionality_terms

logger = logging.getLogger(__name__)


class PathwaySuggestionService:
    """Service for generating pathway suggestions based on Key Events"""

    def __init__(self, cache_model=None, config=None, embedding_service=None, ke_override_model=None):
        self.cache_model = cache_model
        self.config = config or ConfigLoader.get_default_config()
        self.embedding_service = embedding_service
        self.ke_override_model = ke_override_model
        self.aop_wiki_endpoint = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"
        self.wikipathways_endpoint = "https://sparql.wikipathways.org/sparql"

    def get_pathway_suggestions(
        self, ke_id: str, ke_title: str, bio_level: str = None, limit: int = 10
    ) -> Dict[str, any]:
        """
        Get comprehensive pathway suggestions for a Key Event

        Args:
            ke_id: Key Event ID (e.g., "Event:123")
            ke_title: Key Event title for text-based matching
            bio_level: Biological level of the KE (Molecular, Cellular, Tissue, etc.)
            limit: Maximum number of suggestions to return

        Returns:
            Dictionary containing gene-based, text-based, and embedding-based suggestions
        """
        try:
            logger.info("Getting pathway suggestions for %s", ke_id)

            # Get gene-based suggestions
            genes = self._get_genes_from_ke(ke_id)
            gene_suggestions = []
            if genes:
                gene_suggestions = self._find_pathways_by_genes(genes, limit)
                logger.info("Found %d gene-based suggestions", len(gene_suggestions))

            # Get embedding-based suggestions
            embedding_suggestions = []
            if self.embedding_service:
                ke_description = ""  # Fetch from AOP-Wiki if available in future
                embedding_suggestions = self._get_embedding_based_suggestions(
                    ke_id, ke_title, ke_description, bio_level, limit
                )
                logger.info("Found %d embedding-based suggestions", len(embedding_suggestions))

            # Get ontology tag-based suggestions
            ontology_suggestions = self._compute_ontology_tag_scores(ke_title, ke_id, limit)
            logger.info("Found %d ontology tag-based suggestions", len(ontology_suggestions))

            # Combine all signals with hybrid scoring
            combined_suggestions = self._combine_multi_signal_suggestions(
                gene_suggestions, [], embedding_suggestions, ontology_suggestions, limit
            )

            return {
                "ke_id": ke_id,
                "ke_title": ke_title,
                "genes_found": len(genes),
                "gene_list": [g["symbol"] for g in genes],
                "gene_list_full": genes,
                "gene_based_suggestions": gene_suggestions,
                "embedding_based_suggestions": embedding_suggestions,
                "ontology_based_suggestions": ontology_suggestions,
                "combined_suggestions": combined_suggestions,
                "total_suggestions": len(combined_suggestions),
            }

        except Exception as e:
            logger.error("Error getting pathway suggestions for %s: %s", ke_id, e)
            return {
                "error": "Failed to generate pathway suggestions",
                "ke_id": ke_id,
                "ke_title": ke_title,
            }

    def _get_genes_from_ke(self, ke_id: str) -> List[Dict[str, str]]:
        """Extract gene identifier triples ({ncbi, hgnc, symbol}) for a Key Event."""
        return get_genes_from_ke(ke_id, self.aop_wiki_endpoint, self.cache_model)

    def _find_pathways_by_genes(
        self, genes: List[Dict[str, str]], limit: int = 20
    ) -> List[Dict[str, any]]:
        """
        Find WikiPathways containing specific genes

        Args:
            genes: List of gene-identifier dicts {ncbi, hgnc, symbol}
            limit: Maximum number of pathways to return

        Returns:
            List of pathway dictionaries with gene overlap information
        """
        if not genes:
            return []

        try:
            # Create VALUES clause for SPARQL query with URIs (WikiPathways uses identifiers.org URIs)
            # WP indexes wp:bdbHgncSymbol against hgnc.symbol/{SYMBOL}; pull symbol from gene dict.
            gene_values = " ".join([f'<https://identifiers.org/hgnc.symbol/{g["symbol"]}>' for g in genes])

            sparql_query = f"""
            PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
            PREFIX dc: <http://purl.org/dc/elements/1.1/>
            PREFIX dcterms: <http://purl.org/dc/terms/>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

            SELECT DISTINCT ?pathway ?title ?description ?pathwayID ?geneProduct ?geneSymbol
            WHERE {{
                ?pathway a wp:Pathway ;
                         dc:title ?title ;
                         dcterms:identifier ?pathwayID ;
                         wp:organismName "Homo sapiens" .
                ?geneProduct dcterms:isPartOf ?pathway ;
                             wp:bdbHgncSymbol ?geneSymbol .
                OPTIONAL {{ ?pathway dcterms:description ?description }}
                VALUES ?geneSymbol {{ {gene_values} }}
            }}
            ORDER BY ?pathway
            """

            # Check cache first
            query_hash = hashlib.md5(sparql_query.encode()).hexdigest()
            if self.cache_model:
                cached_response = self.cache_model.get_cached_response(
                    self.wikipathways_endpoint, query_hash
                )
                if cached_response:
                    logger.info("Serving gene-based pathways from cache")
                    return json.loads(cached_response)

            response = requests.post(
                self.wikipathways_endpoint,
                data={"query": sparql_query},
                headers={
                    "Accept": "application/sparql-results+json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )

            if response.status_code == 200:
                data = response.json()
                pathway_results = self._process_gene_pathway_results(data, genes)

                # Get total gene counts for all pathways
                pathway_ids = [p["pathwayID"] for p in pathway_results]
                pathway_gene_counts = self._get_pathway_gene_counts(pathway_ids)

                # Add total gene counts and recalculate confidence scores
                for pathway in pathway_results:
                    pathway_id = pathway["pathwayID"]
                    pathway_gene_count = pathway_gene_counts.get(pathway_id, 100)  # Default fallback
                    pathway["pathway_total_genes"] = pathway_gene_count

                    # Calculate pathway specificity
                    pathway["pathway_specificity"] = round(
                        pathway["matching_gene_count"] / pathway_gene_count if pathway_gene_count > 0 else 0.0,
                        3
                    )

                    # Recalculate confidence with refined formula
                    pathway["confidence_score"] = round(
                        self._calculate_gene_confidence(
                            matching_count=pathway["matching_gene_count"],
                            ke_gene_count=len(genes),
                            pathway_gene_count=pathway_gene_count
                        ),
                        3
                    )

                # Sort by confidence score and limit results
                pathway_results.sort(
                    key=lambda x: x["confidence_score"],
                    reverse=True,
                )

                limited_results = pathway_results[:limit]

                # Cache the results
                if self.cache_model:
                    self.cache_model.cache_response(
                        self.wikipathways_endpoint,
                        query_hash,
                        json.dumps(limited_results),
                        24,
                    )

                logger.info("Found %d gene-based pathway suggestions", len(limited_results))
                return limited_results
            else:
                logger.error(
                    "WikiPathways gene query failed: %s - %s", response.status_code, response.text
                )
                return []

        except Exception as e:
            logger.error("Error finding pathways by genes: %s", e)
            return []

    def _get_pathway_gene_counts(self, pathway_ids: List[str]) -> Dict[str, int]:
        """
        Get total gene count for each pathway

        Args:
            pathway_ids: List of WikiPathways IDs

        Returns:
            Dict mapping pathway_id -> total_gene_count
        """
        if not pathway_ids:
            return {}

        try:
            # Build VALUES clause for pathway IDs
            pathway_values = " ".join([f'"{pid}"' for pid in pathway_ids])

            sparql_query = f"""
            PREFIX wp: <http://vocabularies.wikipathways.org/wp#>
            PREFIX dcterms: <http://purl.org/dc/terms/>

            SELECT ?pathwayID (COUNT(DISTINCT ?geneSymbol) as ?geneCount)
            WHERE {{
                ?pathway a wp:Pathway ;
                         dcterms:identifier ?pathwayID ;
                         wp:organismName "Homo sapiens" .
                ?geneProduct dcterms:isPartOf ?pathway ;
                             wp:bdbHgncSymbol ?geneSymbol .
                VALUES ?pathwayID {{ {pathway_values} }}
            }}
            GROUP BY ?pathwayID
            """

            # Check cache first
            query_hash = hashlib.md5(sparql_query.encode()).hexdigest()
            if self.cache_model:
                cached_response = self.cache_model.get_cached_response(
                    self.wikipathways_endpoint, query_hash
                )
                if cached_response:
                    logger.info("Serving pathway gene counts from cache")
                    return json.loads(cached_response)

            response = requests.post(
                self.wikipathways_endpoint,
                data={"query": sparql_query},
                headers={
                    "Accept": "application/sparql-results+json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )

            if response.status_code == 200:
                data = response.json()
                gene_counts = {}

                if "results" in data and "bindings" in data["results"]:
                    for binding in data["results"]["bindings"]:
                        pathway_id = binding.get("pathwayID", {}).get("value", "")
                        gene_count = binding.get("geneCount", {}).get("value", "0")

                        if pathway_id:
                            gene_counts[pathway_id] = int(gene_count)

                # Cache the results
                if self.cache_model:
                    self.cache_model.cache_response(
                        self.wikipathways_endpoint,
                        query_hash,
                        json.dumps(gene_counts),
                        24,
                    )

                logger.info("Retrieved gene counts for %d pathways", len(gene_counts))
                return gene_counts
            else:
                logger.error(
                    "WikiPathways gene count query failed: %s - %s", response.status_code, response.text
                )
                return {}

        except Exception as e:
            logger.error("Error getting pathway gene counts: %s", e)
            return {}

    def _calculate_gene_confidence(
        self,
        matching_count: int,
        ke_gene_count: int,
        pathway_gene_count: int
    ) -> float:
        """
        Calculate gene-based confidence with specificity and gene count penalties

        Args:
            matching_count: Number of matching genes
            ke_gene_count: Total KE genes
            pathway_gene_count: Total pathway genes

        Returns:
            Confidence score (0.0-1.0)
        """
        if ke_gene_count == 0 or pathway_gene_count == 0:
            return 0.0

        config = self.config.pathway_suggestion.gene_scoring

        # 1. Overlap ratio (from KE perspective)
        overlap_ratio = matching_count / ke_gene_count

        # 2. Pathway specificity (from pathway perspective)
        specificity = matching_count / pathway_gene_count

        # 3. Scale specificity for meaningful contribution
        specificity_boost = min(1.0, specificity * config.specificity_scaling_factor)

        # 4. Combine overlap and specificity
        base_confidence = (
            overlap_ratio * config.overlap_weight +
            specificity_boost * config.specificity_weight +
            config.base_boost
        )

        # 5. Apply KE gene count penalty
        ke_gene_penalty = (
            1.0 if ke_gene_count >= config.min_genes_for_high_confidence
            else config.low_gene_penalty
        )

        # 6. Final confidence with cap
        confidence = min(config.max_confidence, base_confidence * ke_gene_penalty)

        return confidence

    def _process_gene_pathway_results(
        self, sparql_data: Dict, input_genes: List[Dict[str, str]]
    ) -> List[Dict[str, any]]:
        """Process SPARQL results and calculate gene overlap statistics"""
        pathway_map = {}

        if "results" not in sparql_data or "bindings" not in sparql_data["results"]:
            return []

        for binding in sparql_data["results"]["bindings"]:
            pathway_id = binding.get("pathwayID", {}).get("value", "")
            pathway_title = binding.get("title", {}).get("value", "")
            pathway_desc = binding.get("description", {}).get("value", "")
            gene_symbol_uri = binding.get("geneSymbol", {}).get("value", "")

            # Extract gene symbol from URI (e.g., https://identifiers.org/hgnc.symbol/CYP2E1 -> CYP2E1)
            gene_symbol = gene_symbol_uri.split('/')[-1] if gene_symbol_uri else ""

            if not pathway_id or not pathway_title:
                continue

            if pathway_id not in pathway_map:
                pathway_map[pathway_id] = {
                    "pathwayID": pathway_id,
                    "pathwayTitle": pathway_title,
                    "pathwayDescription": pathway_desc,
                    "matching_genes": set(),
                    "suggestion_type": "gene_based",
                }

            if gene_symbol:
                pathway_map[pathway_id]["matching_genes"].add(gene_symbol)

        # Calculate overlap statistics
        results = []
        for pathway_data in pathway_map.values():
            matching_genes = list(pathway_data["matching_genes"])
            matching_count = len(matching_genes)
            overlap_ratio = matching_count / len(input_genes) if input_genes else 0

            results.append(
                {
                    "pathwayID": pathway_data["pathwayID"],
                    "pathwayTitle": pathway_data["pathwayTitle"],
                    "pathwayDescription": pathway_data["pathwayDescription"],
                    "pathwayLink": f"https://www.wikipathways.org/index.php/Pathway:{pathway_data['pathwayID']}",
                    "pathwaySvgUrl": f"https://www.wikipathways.org/wikipathways-assets/pathways/{pathway_data['pathwayID']}/{pathway_data['pathwayID']}.svg",
                    "matching_genes": matching_genes,
                    "matching_gene_count": matching_count,
                    "gene_overlap_ratio": round(overlap_ratio, 3),
                    "suggestion_type": "gene_based",
                    "pathway_total_genes": 0,  # Placeholder, filled in _find_pathways_by_genes
                    "pathway_specificity": 0.0,  # Placeholder, calculated after we have totals
                    "confidence_score": 0.0,  # Placeholder, calculated in _find_pathways_by_genes with refined formula
                    "match_types": ["gene"],  # For UI badge display
                    "primary_evidence": "gene_overlap"  # For UI primary evidence label
                }
            )

        return results

    def _get_all_pathways_for_search(self) -> List[Dict[str, str]]:
        """
        Get all pathways with titles and descriptions for text search
        Uses pre-computed pathway_metadata.json which includes ontology tags and publications
        """
        try:
            import os

            # Load from pre-computed metadata file
            metadata_path = os.path.join(PROJECT_ROOT, 'data', 'pathway_metadata.json')

            with open(metadata_path, 'r') as f:
                pathways = json.load(f)

            # Ensure all pathways have required fields and enrichment data
            for pathway in pathways:
                # Add SVG URL if not present
                if 'pathwaySvgUrl' not in pathway:
                    pathway['pathwaySvgUrl'] = f"https://www.wikipathways.org/wikipathways-assets/pathways/{pathway['pathwayID']}/{pathway['pathwayID']}.svg"

                # Ensure enrichment fields exist (default to empty lists if missing)
                if 'ontologyTags' not in pathway:
                    pathway['ontologyTags'] = []
                if 'publications' not in pathway:
                    pathway['publications'] = []

            logger.info("Loaded %d pathways from pre-computed metadata (with enrichment data)", len(pathways))
            return pathways

        except FileNotFoundError:
            logger.warning("pathway_metadata.json not found, falling back to empty list")
            return []
        except Exception as e:
            logger.error("Error loading pathway metadata: %s", e)
            return []

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text for comparison"""
        if not text:
            return ""

        # Remove special characters and normalize whitespace
        cleaned = re.sub(r"[^\w\s]", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip().lower()


    def _get_embedding_based_suggestions(
        self,
        ke_id: str,
        ke_title: str,
        ke_description: str,
        bio_level: str,
        limit: int = 20
    ) -> List[Dict]:
        """
        Get pathway suggestions using BioBERT semantic embeddings

        Now computes separate title and description similarities using the
        embedding service's compute_ke_pathway_similarity() method.

        Args:
            ke_id: Key Event ID
            ke_title: Key Event title (will be cleaned of directionality terms)
            ke_description: Key Event description
            bio_level: Biological level (Molecular, Cellular, etc.)
            limit: Maximum suggestions to return

        Returns:
            List of pathway suggestions with separate embedding scores
        """
        if not self.embedding_service:
            logger.warning("Embedding service not available")
            return []

        try:
            logger.info("Computing embedding-based suggestions for %s", ke_id)

            # IMPORTANT: Strip directionality terms from title before computing embeddings
            ke_title_clean = remove_directionality_terms(ke_title)
            logger.debug(f"Cleaned KE title: '{ke_title}' -> '{ke_title_clean}'")

            # Resolve description toggle: global config + per-KE overrides
            global_toggle = getattr(
                getattr(
                    self.config.pathway_suggestion, 'embedding_based_matching', None
                ),
                'use_ke_description', True
            )
            disabled_kes = self.ke_override_model.get_disabled_ke_ids() if self.ke_override_model else set()
            use_desc = resolve_description_usage(ke_id, global_toggle, disabled_kes)
            logger.debug("KE description toggle: global=%s, ke_disabled=%s, use_desc=%s",
                         global_toggle, ke_id in disabled_kes, use_desc)

            # Get all pathways
            all_pathways = self._get_all_pathways_for_search()

            # Use batch processing for efficiency — internally calls
            # get_ke_embedding_for_matching with use_description flag
            batch_results = self.embedding_service.compute_ke_pathways_batch_similarity(
                ke_id=ke_id,
                ke_title=ke_title_clean,  # Use cleaned title
                ke_description=ke_description,
                pathways=all_pathways,
                use_description=use_desc,
            )

            # Apply minimum threshold and format suggestions
            embedding_config = getattr(
                self.config.pathway_suggestion,
                'embedding_based_matching',
                None
            )
            min_threshold = getattr(embedding_config, 'min_threshold', 0.3) if embedding_config else 0.3

            suggestions = []
            for result in batch_results:
                confidence = result['combined_similarity']

                if confidence >= min_threshold:
                    suggestion = {
                        'pathwayID': result['pathwayID'],
                        'pathwayTitle': result['pathwayTitle'],
                        'pathwayDescription': result.get('pathwayDescription', ''),
                        'pathwayLink': result.get('pathwayLink', ''),
                        'pathwaySvgUrl': result.get('pathwaySvgUrl', ''),
                        'confidence_score': confidence,
                        'embedding_similarity': result['combined_similarity'],
                        'title_similarity': result['title_similarity'],
                        'description_similarity': result['description_similarity'],
                        'suggestion_type': 'embedding_based',
                        'match_types': ['embedding'],  # For UI badge display
                        'primary_evidence': 'semantic_similarity'  # For UI primary evidence label
                    }
                    suggestions.append(suggestion)

            # Sort by confidence descending
            suggestions.sort(key=lambda x: x['confidence_score'], reverse=True)

            logger.info("Found %d embedding-based suggestions", len(suggestions))

            return suggestions[:limit]

        except Exception as e:
            logger.error("Embedding-based suggestion failed: %s", e)
            return []

    def _compute_ontology_tag_scores(
        self, ke_title: str, ke_id: str = None, limit: int = 20
    ) -> List[Dict[str, any]]:
        """
        Score pathways by matching ontology tags to KE biological concepts

        Args:
            ke_title: Key Event title to extract concepts from
            ke_id: Key Event ID (optional, for logging)
            limit: Maximum number of results

        Returns:
            List of pathway dictionaries with ontology-based confidence scores
        """
        try:
            if not self.config.pathway_suggestion.ontology_tag_matching.enabled:
                logger.info("Ontology tag matching disabled")
                return []

            # Load pathways with ontology tags
            all_pathways = self._get_all_pathways_for_search()

            # Clean and extract biological keywords from KE title
            ke_title_clean = self._clean_text(remove_directionality_terms(ke_title))
            ke_keywords = self._extract_biological_keywords(ke_title_clean)

            if not ke_keywords:
                logger.info("No biological keywords extracted from KE title")
                return []

            logger.info("Extracted %d keywords from KE: %s", len(ke_keywords), ke_keywords)

            # Score each pathway based on ontology tag matches
            scored_pathways = []
            config = self.config.pathway_suggestion.ontology_tag_matching

            for pathway in all_pathways:
                tags = pathway.get('ontologyTags', [])

                if not tags:
                    continue  # Skip pathways without tags

                # Calculate match score
                exact_matches = 0
                fuzzy_matches = 0
                matched_tags = []

                for keyword in ke_keywords:
                    for tag in tags:
                        tag_clean = self._clean_text(tag)

                        # Check for exact substring match
                        if keyword in tag_clean or tag_clean in keyword:
                            exact_matches += 1
                            matched_tags.append(tag)
                            break

                        # Check for fuzzy match using SequenceMatcher
                        similarity = SequenceMatcher(None, keyword, tag_clean).ratio()
                        if similarity >= config.fuzzy_match_threshold:
                            fuzzy_matches += 1
                            matched_tags.append(tag)
                            break

                # Calculate confidence score
                confidence_score = (
                    exact_matches * config.exact_match_boost +
                    fuzzy_matches * config.fuzzy_match_boost
                )

                # Cap at max_confidence
                confidence_score = min(confidence_score, config.max_confidence)

                # Only include if above threshold
                if confidence_score >= config.min_threshold:
                    scored_pathways.append({
                        **pathway,
                        'confidence_score': round(confidence_score, 3),
                        'suggestion_type': 'ontology_tag',
                        'match_types': ['ontology'],
                        'primary_evidence': 'ontology_tags',
                        'ontology_match_details': {
                            'exact_matches': exact_matches,
                            'fuzzy_matches': fuzzy_matches,
                            'ke_keywords': ke_keywords,
                            'matched_tags': matched_tags[:3],  # Sample for debugging
                        }
                    })

            # Sort by confidence and limit
            scored_pathways.sort(key=lambda x: x['confidence_score'], reverse=True)
            limited_results = scored_pathways[:limit]

            logger.info("Found %d ontology tag-based suggestions", len(limited_results))
            return limited_results

        except Exception as e:
            logger.error("Error computing ontology tag scores: %s", e)
            return []

    def _extract_biological_keywords(self, text: str) -> List[str]:
        """
        Extract biological keywords from cleaned text

        Removes common stopwords and keeps domain-specific terms
        """
        # Common stopwords to remove
        stopwords = {
            'the', 'of', 'in', 'and', 'or', 'a', 'an', 'to', 'from', 'by',
            'with', 'for', 'on', 'at', 'is', 'are', 'was', 'were', 'be',
            'increased', 'decreased', 'leading', 'resulting'
        }

        # Split and filter
        words = text.lower().split()
        keywords = [w for w in words if w not in stopwords and len(w) > 2]

        # Remove duplicates while preserving order
        seen = set()
        unique_keywords = []
        for keyword in keywords:
            if keyword not in seen:
                seen.add(keyword)
                unique_keywords.append(keyword)

        return unique_keywords

    def _combine_multi_signal_suggestions(
        self,
        gene_suggestions: List[Dict],
        text_suggestions: List[Dict],
        embedding_suggestions: List[Dict],
        ontology_suggestions: List[Dict],
        limit: int
    ) -> List[Dict]:
        """
        Combine scoring signals with transparent hybrid scoring.

        v1.5 pure-semantic: ranking is driven by BioBERT embedding similarity only.
        Gene overlap is computed and surfaced on each item (for chip rendering) but
        does not affect rank. Ontology-tag matches are applied as a post-combine
        multiplicative boost (mirroring GO IC boost) rather than as a hybrid weight.

        Returns:
            List of suggestions with all scores visible
        """
        # Get weights from config (v1.5: embedding=1.0, gene=0.0, ontology=0.0)
        hybrid_weights = getattr(
            self.config.pathway_suggestion,
            'hybrid_weights',
            None
        )

        gene_weight = getattr(hybrid_weights, 'gene', 0.0) if hybrid_weights else 0.0
        embedding_weight = getattr(hybrid_weights, 'embedding', 1.0) if hybrid_weights else 1.0
        # Ontology weight is 0.0 in v1.5 — signal applied as post-combine boost instead.
        ontology_weight = 0.0
        multi_evidence_bonus = getattr(hybrid_weights, 'multi_evidence_bonus', 0.0) if hybrid_weights else 0.0

        final_threshold = self.config.pathway_suggestion.dynamic_thresholds.base_threshold

        # Build ontology score map once — used by both _apply_ontology_boost and scores dict
        ontology_map = {
            o['pathwayID']: o.get('confidence_score', 0.0)
            for o in (ontology_suggestions or [])
        }

        combined = combine_scored_items(
            scored_lists={
                'gene': gene_suggestions,
                'embedding': embedding_suggestions,
                'ontology': ontology_suggestions,
            },
            id_field='pathwayID',
            weights={'gene': gene_weight, 'embedding': embedding_weight, 'ontology': ontology_weight},
            score_field_map={
                'gene': 'confidence_score',
                'embedding': 'confidence_score',
                'ontology': 'confidence_score',
            },
            multi_evidence_bonus=multi_evidence_bonus,
            min_threshold=final_threshold,
            max_score=0.98,
        )

        # Apply ontology post-combine boost BEFORE per-pathway post-processing.
        # Mirrors go.py::_apply_ic_boost — adjusts hybrid_score multiplicatively
        # and re-sorts. Gene overlap does NOT influence hybrid_score.
        combined = self._apply_ontology_boost(combined, ontology_map)

        # WP-specific post-processing: build scores dict, primary_evidence, embedding_details
        for pathway in combined:
            sig = pathway.pop('signal_scores', {})
            gene_score = sig.get('gene', 0.0)
            emb_score = sig.get('embedding', 0.0)

            # Restore gene-overlap chip data from per-signal raw item (if present)
            gene_signal_item = pathway.get('_signal_data', {}).get('gene', {})
            if gene_signal_item:
                pathway.setdefault('matching_genes', gene_signal_item.get('matching_genes', []))
                pathway.setdefault('matching_gene_count', gene_signal_item.get('matching_gene_count', 0))
                pathway.setdefault('gene_overlap_ratio', gene_signal_item.get('gene_overlap_ratio', 0.0))

            # ontology_confidence is sourced from the pre-built map (display-only; not in weighted sum)
            ont_score = ontology_map.get(pathway['pathwayID'], 0.0)

            pathway['scores'] = {
                'gene_confidence': gene_score,
                'embedding_similarity': emb_score,
                'ontology_confidence': ont_score,
                'final_score': pathway['hybrid_score'],
            }

            # Primary evidence: v1.5 default is 'semantic_similarity' (embedding drives rank).
            # Override to 'ontology_tags' only when the post-combine boost actually fired.
            if pathway.get('ontology_boost_applied', False):
                pathway['primary_evidence'] = 'ontology_tags'
            elif emb_score > 0:
                pathway['primary_evidence'] = 'semantic_similarity'
            else:
                pathway['primary_evidence'] = 'semantic_similarity'

            # Add embedding_details from per-signal data
            if 'embedding' in pathway.get('match_types', []):
                emb_data = pathway.get('_signal_data', {}).get('embedding', {})
                pathway['embedding_details'] = {
                    'title_similarity': emb_data.get('title_similarity', 0),
                    'description_similarity': emb_data.get('description_similarity', 0),
                    'combined': emb_data.get('embedding_similarity', 0)
                }

            # Clean up internal per-signal data and boost flag
            pathway.pop('_signal_data', None)
            pathway.pop('ontology_boost_applied', None)

        return combined[:limit]

    def _apply_ontology_boost(self, suggestions: List[Dict], ontology_map: Dict[str, float]) -> List[Dict]:
        """Apply ontology-tag post-combine boost to WP suggestions.

        Mirrors go.py::_apply_ic_boost — adjusts hybrid_score multiplicatively
        for pathways that have an ontology-tag match, then re-sorts descending.

        Formula: hybrid_score *= (1 + boost_weight * ontology_score)

        Args:
            suggestions: Combined list from combine_scored_items (already above threshold).
            ontology_map: pathwayID -> ontology confidence_score (pre-built by caller).

        Returns:
            suggestions sorted descending by updated hybrid_score.
        """
        cfg = getattr(self.config.pathway_suggestion, 'ontology_post_combine_boost', None)
        if not cfg or not getattr(cfg, 'enabled', False):
            return suggestions

        boost_weight = getattr(cfg, 'boost_weight', 0.15)

        for s in suggestions:
            ont_score = ontology_map.get(s['pathwayID'], 0.0)
            s['hybrid_score'] = round(s['hybrid_score'] * (1 + boost_weight * ont_score), 4)
            s['ontology_boost_applied'] = ont_score > 0.0

        suggestions.sort(key=lambda x: x['hybrid_score'], reverse=True)
        return suggestions

    def search_pathways(
        self, query: str, threshold: float = 0.4, limit: int = 20
    ) -> List[Dict[str, any]]:
        """
        Search pathways using SequenceMatcher fuzzy matching

        A query that is a WikiPathways identifier (``WP554``, tolerating case
        and ``:``/``-``/``_`` separators) resolves directly to that pathway
        instead of being fuzzy-matched against titles, which never hits (#156).
        Mirrors the ID branches in ``GoSuggestionService.search_go_terms`` and
        ``ReactomeSuggestionService.search_reactome_terms``.

        A bare-numeric query (``554``) is also tried as an ID, but falls
        through to fuzzy matching when it does not resolve — unlike an
        explicit ``WP``-prefixed query, digits alone are not unambiguously
        an identifier.

        Args:
            query: Search query string
            threshold: Minimum similarity threshold (0.0-1.0)
            limit: Maximum number of results

        Returns:
            List of matching pathways with relevance scores
        """
        try:
            pathways = self._get_all_pathways_for_search()

            # Direct ID lookup branch.
            id_match = re.match(r"^(WP)?[:\-_]?(\d+)$", query.strip(), re.IGNORECASE)
            if id_match:
                normalized = f"WP{id_match.group(2)}"
                for pathway in pathways:
                    if pathway.get("pathwayID", "").upper() == normalized:
                        return [
                            {
                                **pathway,
                                "title_similarity": 1.0,
                                "description_similarity": 1.0,
                                "relevance_score": 1.0,
                                "pathwaySvgUrl": f"https://www.wikipathways.org/wikipathways-assets/pathways/{pathway['pathwayID']}/{pathway['pathwayID']}.svg",
                            }
                        ]
                # An explicit WP-prefixed query that misses is a miss, not a
                # cue to fuzzy-match; bare digits fall through.
                if id_match.group(1):
                    return []

            # Remove directionality terms from query for better matching
            query_no_direction = remove_directionality_terms(query)
            query_clean = self._clean_text(query_no_direction)

            if not query_clean:
                return []

            results = []
            for pathway in pathways:
                title_clean = self._clean_text(pathway["pathwayTitle"])
                title_similarity = SequenceMatcher(None, query_clean, title_clean).ratio()

                desc_similarity = 0
                if pathway.get("pathwayDescription"):
                    desc_clean = self._clean_text(pathway["pathwayDescription"])
                    desc_similarity = SequenceMatcher(None, query_clean, desc_clean).ratio()

                max_similarity = max(title_similarity, desc_similarity)

                if max_similarity >= threshold:
                    results.append(
                        {
                            **pathway,
                            "title_similarity": round(title_similarity, 3),
                            "description_similarity": round(desc_similarity, 3),
                            "relevance_score": round(max_similarity, 3),
                            "pathwaySvgUrl": f"https://www.wikipathways.org/wikipathways-assets/pathways/{pathway['pathwayID']}/{pathway['pathwayID']}.svg",
                        }
                    )

            # Sort by relevance and limit results
            results.sort(key=lambda x: x["relevance_score"], reverse=True)
            return results[:limit]

        except Exception as e:
            logger.error("Error in pathway search: %s", e)
            return []