"""
BioBERT embedding service for semantic similarity computation

Features:
- LRU cache for recent encodings (1000 items)
- Pre-computed pathway embeddings (loaded from disk)
- Power transformation to address inflated BioBERT scores
- Fallback to text-based matching on errors
- GPU support (auto-detected)
"""

from __future__ import annotations
from typing import List, Dict, Optional, TYPE_CHECKING
from functools import lru_cache
import logging
import os

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# Default score transformation config
DEFAULT_SCORE_TRANSFORM = {
    'method': 'power',
    'power_exponent': 4.0,
    'scale_factor': 0.75,
    'output_min': 0.0,
    'output_max': 0.95,
    'skip_precomputed_for_titles': False
}

# Default entity extraction config
DEFAULT_ENTITY_EXTRACT = {
    'enabled': True,
    'min_entity_length': 3,
    'include_numbers': True,
    'biological_terms_only': False
}

# Optional imports - only required when embeddings are enabled
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    import torch
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    logger.warning("BioBERT dependencies not installed. Embedding service will be unavailable.")
    EMBEDDINGS_AVAILABLE = False
    SentenceTransformer = None
    np = None
    torch = None


class BiologicalEmbeddingService:
    """
    Service for computing semantic similarity using BioBERT embeddings

    Features:
    - LRU cache for recent encodings (1000 items)
    - Pre-computed pathway embeddings (loaded from disk)
    - Fallback to text-based matching on errors
    - GPU support (auto-detected)
    """

    def __init__(
        self,
        model_name: str = "dmis-lab/biobert-base-cased-v1.2",
        use_gpu: bool = True,
        precomputed_embeddings_path: Optional[str] = None,
        precomputed_ke_embeddings_path: Optional[str] = None,
        score_transform_config: Optional[Dict] = None,
        title_weight: float = 0.85,
        entity_extract_config: Optional[Dict] = None
    ):
        """
        Initialize BioBERT model

        Args:
            model_name: HuggingFace model identifier
            use_gpu: Use GPU if available
            precomputed_embeddings_path: Path to .npy file with pathway embeddings
            precomputed_ke_embeddings_path: Path to .npy file with KE embeddings
            score_transform_config: Configuration for score transformation (optional)
            title_weight: Weight for title similarity (0.0-1.0), description = 1 - title_weight
            entity_extract_config: Configuration for entity extraction (optional)
        """
        if not EMBEDDINGS_AVAILABLE:
            raise RuntimeError(
                "BioBERT dependencies not installed. "
                "Install with: pip install transformers sentence-transformers torch"
            )

        try:
            device = 'cuda' if use_gpu and torch.cuda.is_available() else 'cpu'
            logger.info(f"Initializing BioBERT model on {device}")

            self.model = SentenceTransformer(model_name, device=device)
            self.model_name = model_name
            self.device = device

            # Precomputed artifacts found missing at load or lookup time.
            # Reported by /health so a degraded deployment is visible (#209).
            self.embeddings_degraded: List[str] = []

            # Score transformation configuration
            self.score_transform_config = score_transform_config or DEFAULT_SCORE_TRANSFORM
            logger.info(f"Score transformation: {self.score_transform_config['method']} "
                       f"(exponent={self.score_transform_config.get('power_exponent', 'N/A')})")

            # Title vs description weighting
            self.title_weight = max(0.0, min(1.0, title_weight))  # Clamp to [0, 1]
            self.desc_weight = 1.0 - self.title_weight
            logger.info(f"Similarity weighting: title={self.title_weight:.0%}, description={self.desc_weight:.0%}")

            # Entity extraction configuration
            self.entity_extract_config = entity_extract_config or DEFAULT_ENTITY_EXTRACT
            if self.entity_extract_config.get('enabled', False):
                logger.info(f"Entity extraction enabled")

            # Load pre-computed pathway embeddings if available
            self.pathway_embeddings = {}
            if precomputed_embeddings_path and os.path.exists(precomputed_embeddings_path):
                self._load_precomputed_embeddings(precomputed_embeddings_path)

            # Load pre-computed KE embeddings if available
            self.ke_embeddings = {}
            if precomputed_ke_embeddings_path and os.path.exists(precomputed_ke_embeddings_path):
                self._load_precomputed_ke_embeddings(precomputed_ke_embeddings_path)

            # Load dual KE embedding sets (title-only and with-description)
            self.ke_embeddings_title_only = {}
            self.ke_embeddings_with_desc = {}
            self._load_precomputed_ke_embeddings_dual()

            # Load pre-computed pathway TITLE embeddings if available
            self.pathway_title_embeddings = {}
            if os.path.exists('data/pathway_title_embeddings.npz'):
                self._load_precomputed_pathway_title_embeddings('data/pathway_title_embeddings.npz')

            logger.info(f"BioBERT service initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize BioBERT: {e}")
            raise

    def _load_precomputed_embeddings(self, path: str):
        """Load pre-computed pathway embeddings from NPZ format (no pickle)."""
        npz_path = path.replace('.npy', '.npz')
        if not os.path.exists(npz_path):
            logger.warning("Pathway embeddings file not found: %s", npz_path)
            self.pathway_embeddings = {}
            return
        try:
            with np.load(npz_path) as data:  # allow_pickle=False by default
                ids = data['ids']
                matrix = data['matrix']
            self.pathway_embeddings = dict(zip(ids, matrix))
            logger.info("Loaded %d pre-computed pathway embeddings (normalized)",
                        len(self.pathway_embeddings))
        except Exception as e:
            logger.warning("Could not load pre-computed embeddings: %s", e)
            self.pathway_embeddings = {}

    def _load_precomputed_ke_embeddings(self, path: str):
        """Load pre-computed KE embeddings from NPZ format (no pickle)."""
        npz_path = path.replace('.npy', '.npz')
        if not os.path.exists(npz_path):
            logger.warning("KE embeddings file not found: %s", npz_path)
            self.ke_embeddings = {}
            return
        try:
            with np.load(npz_path) as data:
                ids = data['ids']
                matrix = data['matrix']
            self.ke_embeddings = dict(zip(ids, matrix))
            logger.info("Loaded %d pre-computed KE embeddings (normalized)",
                        len(self.ke_embeddings))
        except Exception as e:
            logger.warning("Could not load pre-computed KE embeddings: %s", e)
            self.ke_embeddings = {}

    def _load_precomputed_pathway_title_embeddings(self, path: str):
        """Load pre-computed pathway title embeddings from NPZ format (no pickle)."""
        npz_path = path.replace('.npy', '.npz')
        if not os.path.exists(npz_path):
            logger.warning("Pathway title embeddings file not found: %s", npz_path)
            self.pathway_title_embeddings = {}
            return
        try:
            with np.load(npz_path) as data:
                ids = data['ids']
                matrix = data['matrix']
            self.pathway_title_embeddings = dict(zip(ids, matrix))
            logger.info("Loaded %d pre-computed pathway title embeddings (normalized)",
                        len(self.pathway_title_embeddings))
        except Exception as e:
            logger.warning("Could not load pre-computed pathway title embeddings: %s", e)
            self.pathway_title_embeddings = {}

    def _load_precomputed_ke_embeddings_dual(self):
        """Load both title-only and title+description KE embedding sets.

        Falls back gracefully if files don't exist:
        - title-only missing: empty dict (log warning)
        - with-desc missing: falls back to self.ke_embeddings
        """
        title_only_path = 'data/ke_embeddings_title_only.npz'
        with_desc_path = 'data/ke_embeddings_with_desc.npz'

        # Load title-only embeddings
        if os.path.exists(title_only_path):
            try:
                with np.load(title_only_path) as data:
                    ids = data['ids']
                    matrix = data['matrix']
                self.ke_embeddings_title_only = dict(zip(ids, matrix))
                logger.info("Loaded %d title-only KE embeddings",
                            len(self.ke_embeddings_title_only))
            except Exception as e:
                logger.warning("Could not load title-only KE embeddings: %s", e)
                self.ke_embeddings_title_only = {}
                self._note_degraded_embeddings('ke_embeddings_title_only')
        else:
            logger.warning("Title-only KE embeddings not found: %s", title_only_path)
            self.ke_embeddings_title_only = {}
            self._note_degraded_embeddings('ke_embeddings_title_only')

        # Load with-description embeddings
        if os.path.exists(with_desc_path):
            try:
                with np.load(with_desc_path) as data:
                    ids = data['ids']
                    matrix = data['matrix']
                self.ke_embeddings_with_desc = dict(zip(ids, matrix))
                logger.info("Loaded %d title+description KE embeddings",
                            len(self.ke_embeddings_with_desc))
            except Exception as e:
                logger.warning("Could not load title+description KE embeddings: %s", e)
                self.ke_embeddings_with_desc = dict(self.ke_embeddings)
        else:
            logger.warning("Title+description KE embeddings not found: %s — "
                           "falling back to default ke_embeddings", with_desc_path)
            self.ke_embeddings_with_desc = dict(self.ke_embeddings)

    def get_ke_embedding_for_matching(self, ke_id: str, ke_text: str,
                                       use_description: bool = True) -> 'np.ndarray':
        """Select title-only or title+description embedding based on toggle state.

        Args:
            ke_id: Key Event ID (e.g., "KE 55")
            ke_text: Fallback text if no precomputed embedding exists
            use_description: Whether to use title+description embedding

        Returns:
            Embedding vector (768-dim)
        """
        if use_description:
            if ke_id in self.ke_embeddings_with_desc:
                return self.ke_embeddings_with_desc[ke_id]
            # Fallback to original ke_embeddings (backward compat) or encode.
            # Safe: ke_embeddings is itself a title+description set.
            return self.get_ke_embedding(ke_id, ke_text)

        if ke_id in self.ke_embeddings_title_only:
            return self.ke_embeddings_title_only[ke_id]

        # A title-only request must NEVER fall through to get_ke_embedding():
        # self.ke_embeddings holds title+description vectors, so doing so would
        # silently return the very thing the caller asked to exclude. That is
        # what broke Reactome name-channel ranking (#209) — the title-only NPZ
        # was never generated, so every caller got a with-description vector
        # while believing it had a title-only one. Encode the text instead;
        # encode() is lru_cached, and this is what the WP path already does.
        self._note_degraded_embeddings('ke_embeddings_title_only')
        return self.encode(ke_text)

    def _note_degraded_embeddings(self, artifact: str) -> None:
        """Record a missing precomputed artifact, logging once per artifact.

        Surfaced by ServiceContainer.get_health_status() so a deployment
        running on live-encoded fallbacks is visible rather than silent.
        """
        if artifact in self.embeddings_degraded:
            return
        self.embeddings_degraded.append(artifact)
        logger.warning(
            "Precomputed artifact '%s' unavailable — falling back to live "
            "encoding. Suggestion quality is unaffected but each uncached "
            "Key Event costs one forward pass; regenerate via "
            "scripts/precompute_ke_embeddings.py to restore.",
            artifact,
        )

    def _extract_entities(self, text: str) -> str:
        """
        Extract biological entities from text for more specific embedding.

        Delegates to text_utils.extract_entities() with config-driven parameters.
        """
        from src.utils.text import extract_entities

        config = self.entity_extract_config
        if not config.get('enabled', False):
            return text

        return extract_entities(
            text,
            min_length=config.get('min_entity_length', 3),
            include_numbers=config.get('include_numbers', True),
            bio_only=config.get('biological_terms_only', False),
        )

    def _transform_similarity_score(self, raw_cosine: float) -> float:
        """
        Transform raw cosine similarity using configured method.

        BioBERT cosine similarities tend to cluster at 0.85-0.95 for all biomedical
        text because biological texts rarely have negative similarity. This
        transformation spreads scores for better differentiation.

        Args:
            raw_cosine: Raw cosine similarity in range [-1, 1]

        Returns:
            Transformed score in range [output_min, output_max]
        """
        config = self.score_transform_config

        # Normalize to [0, 1] range first
        base_score = (raw_cosine + 1.0) / 2.0

        # Apply transformation based on method
        method = config.get('method', 'power')

        if method == 'power':
            # Power transformation: score^exponent
            # Higher exponent = more aggressive compression of high scores
            exponent = config.get('power_exponent', 1.5)
            transformed = base_score ** exponent

        elif method == 'linear':
            # Linear scaling: score × factor
            scale_factor = config.get('scale_factor', 0.75)
            transformed = base_score * scale_factor

        elif method == 'none':
            # No transformation, use raw normalized score
            transformed = base_score

        else:
            logger.warning(f"Unknown transformation method '{method}', using raw score")
            transformed = base_score

        # Apply bounds
        output_min = config.get('output_min', 0.0)
        output_max = config.get('output_max', 0.85)

        return max(output_min, min(output_max, transformed))

    def _transform_similarity_batch(self, raw_cosines: 'np.ndarray') -> 'np.ndarray':
        """
        Transform an array of raw cosine similarities using configured method.

        Vectorized version of _transform_similarity_score for batch processing.

        Args:
            raw_cosines: Array of raw cosine similarities in range [-1, 1]

        Returns:
            Array of transformed scores
        """
        config = self.score_transform_config

        # Normalize to [0, 1] range
        base_scores = (raw_cosines + 1.0) / 2.0

        # Apply transformation based on method
        method = config.get('method', 'power')

        if method == 'power':
            exponent = config.get('power_exponent', 1.5)
            transformed = base_scores ** exponent

        elif method == 'linear':
            scale_factor = config.get('scale_factor', 0.75)
            transformed = base_scores * scale_factor

        elif method == 'none':
            transformed = base_scores

        else:
            logger.warning(f"Unknown transformation method '{method}', using raw scores")
            transformed = base_scores

        # Apply bounds
        output_min = config.get('output_min', 0.0)
        output_max = config.get('output_max', 0.85)

        return np.clip(transformed, output_min, output_max)

    @lru_cache(maxsize=1000)
    def encode(self, text: str) -> 'np.ndarray':
        """
        Encode text to embedding vector with caching

        Args:
            text: Input text (KE or pathway description)

        Returns:
            768-dimensional embedding vector
        """
        try:
            emb = self.model.encode(text, convert_to_numpy=True, show_progress_bar=False)
            norm = np.linalg.norm(emb)
            return (emb / norm).astype(np.float32) if norm > 0.0 else emb
        except Exception as e:
            logger.error(f"Encoding failed: {e}")
            # Return zero vector as fallback
            return np.zeros(768)

    def get_pathway_embedding(self, pathway_id: str, pathway_text: str) -> 'np.ndarray':
        """
        Get embedding for pathway (uses pre-computed if available)

        Args:
            pathway_id: WikiPathways ID (e.g., "WP4269")
            pathway_text: Combined title + description

        Returns:
            Embedding vector
        """
        # Check pre-computed first
        if pathway_id in self.pathway_embeddings:
            return self.pathway_embeddings[pathway_id]

        # Otherwise, compute and cache
        return self.encode(pathway_text)

    def get_ke_embedding(self, ke_id: str, ke_text: str) -> 'np.ndarray':
        """
        Get embedding for Key Event (uses pre-computed if available)

        Args:
            ke_id: Key Event ID (e.g., "KE 55", "KE 1508")
            ke_text: Combined title + description

        Returns:
            Embedding vector
        """
        # Check pre-computed first
        if ke_id in self.ke_embeddings:
            return self.ke_embeddings[ke_id]

        # Otherwise, compute and cache via encode()
        return self.encode(ke_text)

    def compute_similarity(self, text1: str, text2: str, pathway_id: str = None) -> float:
        """
        Compute cosine similarity between two texts with score transformation.

        Args:
            text1: First text (e.g., KE title)
            text2: Second text (e.g., pathway title)
            pathway_id: Optional pathway ID for pre-computed lookup

        Returns:
            Transformed similarity score (0.0-output_max, typically 0.0-0.80)
        """
        try:
            # Extract entities for more specific matching
            text1_processed = self._extract_entities(text1)
            text2_processed = self._extract_entities(text2)

            # Encode processed texts
            emb1 = self.encode(text1_processed)

            # For pathway titles, use pre-computed if available (but note: pre-computed
            # embeddings use full titles, not entity-extracted versions)
            if pathway_id and pathway_id in self.pathway_title_embeddings:
                emb2 = self.pathway_title_embeddings[pathway_id]
            else:
                emb2 = self.encode(text2_processed)

            # Raw similarity — vectors are pre-normalized so dot product == cosine similarity
            raw_similarity = np.dot(emb1, emb2)

            # Apply power transformation to spread scores
            transformed = self._transform_similarity_score(float(raw_similarity))

            return transformed

        except Exception as e:
            logger.error(f"Similarity computation failed: {e}")
            return 0.0

    def compute_ke_pathway_similarity(
        self,
        ke_title: str,
        ke_description: str,
        pathway_id: str,
        pathway_title: str,
        pathway_description: str
    ) -> Dict[str, float]:
        """
        Compute multi-level semantic similarity between KE and pathway
        with score transformation and configurable title weighting.

        Args:
            ke_title: Key Event title
            ke_description: Key Event description
            pathway_id: WikiPathways ID
            pathway_title: Pathway title
            pathway_description: Pathway description

        Returns:
            {
                'title_similarity': float,
                'description_similarity': float,
                'combined_similarity': float
            }
        """
        try:
            # Title-to-title similarity (uses entity extraction and transformation)
            title_sim = self.compute_similarity(ke_title, pathway_title, pathway_id=pathway_id)

            # Full text similarity (title + description)
            ke_text = f"{ke_title}. {ke_description}" if ke_description else ke_title
            pathway_text = f"{pathway_title}. {pathway_description}" if pathway_description else pathway_title

            # Extract entities for description matching too
            ke_text_processed = self._extract_entities(ke_text)
            self._extract_entities(pathway_text)

            # Use pre-computed pathway embedding if available
            ke_emb = self.encode(ke_text_processed)
            pathway_emb = self.get_pathway_embedding(pathway_id, pathway_text)

            # Raw description-level similarity — pre-normalized vectors, dot product == cosine
            raw_desc_sim = np.dot(ke_emb, pathway_emb)

            # Apply transformation to description similarity
            desc_sim = self._transform_similarity_score(float(raw_desc_sim))

            # Combined using configurable title weight (default 85% title, 15% description)
            combined = (title_sim * self.title_weight) + (desc_sim * self.desc_weight)

            return {
                'title_similarity': title_sim,
                'description_similarity': desc_sim,
                'combined_similarity': combined
            }

        except Exception as e:
            logger.error(f"KE-pathway similarity failed: {e}")
            return {
                'title_similarity': 0.0,
                'description_similarity': 0.0,
                'combined_similarity': 0.0
            }

    def compute_batch_similarity(
        self,
        query: str,
        candidates: List[str]
    ) -> List[float]:
        """
        Compute similarity between query and multiple candidates efficiently
        with score transformation applied.

        Args:
            query: Query text (e.g., KE description)
            candidates: List of candidate texts (pathway descriptions)

        Returns:
            List of transformed similarity scores
        """
        try:
            query_emb = self.encode(query)
            candidate_embs = self.model.encode(
                candidates,
                convert_to_numpy=True,
                show_progress_bar=False,
                batch_size=32
            )

            # Batch similarity — pre-normalized vectors, dot product == cosine similarity
            raw_similarities = np.dot(candidate_embs, query_emb)

            # Apply vectorized transformation
            transformed = self._transform_similarity_batch(raw_similarities)

            return [float(s) for s in transformed]

        except Exception as e:
            logger.error(f"Batch similarity failed: {e}")
            return [0.0] * len(candidates)

    def compute_ke_pathways_batch_similarity(
        self,
        ke_id: str,
        ke_title: str,
        ke_description: str,
        pathways: List[Dict],
        use_description: bool = True,
    ) -> List[Dict]:
        """
        Compute similarity between KE and multiple pathways efficiently using pre-computed embeddings
        with score transformation and configurable title weighting.

        Args:
            ke_id: Key Event ID (e.g., "KE 55")
            ke_title: Key Event title
            ke_description: Key Event description
            pathways: List of pathway dicts with pathwayID, pathwayTitle, pathwayDescription

        Returns:
            List of dicts with pathway info and transformed similarity scores
        """
        try:
            # Extract entities from KE title for more specific matching
            ke_title_processed = self._extract_entities(ke_title)

            # Encode KE texts ONCE (use cached if available)
            ke_title_emb = self.encode(ke_title_processed)
            ke_text = f"{ke_title}. {ke_description}" if ke_description else ke_title
            ke_text_processed = self._extract_entities(ke_text)
            ke_full_emb = self.get_ke_embedding_for_matching(ke_id, ke_text_processed, use_description=use_description)

            results = []

            # Pre-load all pathway embeddings (from pre-computed cache)
            pathway_title_embeddings = []
            pathway_full_embeddings = []

            # Check if we should skip pre-computed embeddings for titles
            skip_precomputed = self.score_transform_config.get('skip_precomputed_for_titles', True)

            for pathway in pathways:
                pathway_id = pathway['pathwayID']
                pathway_title = pathway['pathwayTitle']
                pathway_desc = pathway.get('pathwayDescription', '')
                pathway_text = f"{pathway_title}. {pathway_desc}" if pathway_desc else pathway_title

                # For title: compute fresh with entity extraction (more specific) or use pre-computed
                if not skip_precomputed and pathway_id in self.pathway_title_embeddings:
                    pathway_title_embeddings.append(self.pathway_title_embeddings[pathway_id])
                else:
                    # Always extract entities for title matching (this is the key change)
                    pathway_title_processed = self._extract_entities(pathway_title)
                    pathway_title_embeddings.append(self.encode(pathway_title_processed))

                # For full text: use pre-computed or compute with entity extraction
                if pathway_id in self.pathway_embeddings:
                    pathway_full_embeddings.append(self.pathway_embeddings[pathway_id])
                else:
                    pathway_text_processed = self._extract_entities(pathway_text)
                    pathway_full_embeddings.append(self.encode(pathway_text_processed))

            # Convert to numpy arrays
            pathway_title_embeddings = np.array(pathway_title_embeddings)
            pathway_full_embeddings = np.array(pathway_full_embeddings)

            # Vectorized title similarity — pre-normalized vectors, dot product == cosine
            raw_title_similarities = np.dot(pathway_title_embeddings, ke_title_emb)

            # Vectorized full-text similarity — pre-normalized vectors, dot product == cosine
            raw_desc_similarities = np.dot(pathway_full_embeddings, ke_full_emb)

            # Apply vectorized transformation to both
            title_similarities = self._transform_similarity_batch(raw_title_similarities)
            desc_similarities = self._transform_similarity_batch(raw_desc_similarities)

            # Combine scores using configurable weights (default 85% title, 15% description)
            combined_similarities = (title_similarities * self.title_weight) + (desc_similarities * self.desc_weight)

            # Build results
            for i, pathway in enumerate(pathways):
                results.append({
                    'pathwayID': pathway['pathwayID'],
                    'pathwayTitle': pathway['pathwayTitle'],
                    'pathwayDescription': pathway.get('pathwayDescription', ''),
                    'pathwayLink': pathway.get('pathwayLink', ''),
                    'pathwaySvgUrl': pathway.get('pathwaySvgUrl', ''),
                    'title_similarity': float(title_similarities[i]),
                    'description_similarity': float(desc_similarities[i]),
                    'combined_similarity': float(combined_similarities[i])
                })

            return results

        except Exception as e:
            logger.error(f"Batch KE-pathway similarity failed: {e}")
            return []
