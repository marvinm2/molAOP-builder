## Target
src/services/embedding.py

## Metric
python benchmarks/bench_ke_pathways_similarity.py

## Direction
lower

## Iterations
15

## Constraints
- The method signature of `compute_ke_pathways_batch_similarity(self, ke_id, ke_title, ke_description, pathways, use_description)` must not change
- The return value must be identical: list of dicts with pathwayID, pathwayTitle, pathwayDescription, pathwayLink, pathwaySvgUrl, title_similarity, description_similarity, combined_similarity
- All existing tests must pass: `python -m pytest tests/ -q`
- Do not modify any method outside of `compute_ke_pathways_batch_similarity` (lines 565-660)
- Do not change the BioBERT model or its loading mechanism
- The score transformation logic must produce numerically equivalent results
- Do not add external dependencies beyond what is already in requirements.txt
- The `encode()` method's LRU cache must not be removed or resized

## Research Direction
The main bottleneck is the per-pathway encoding loop (lines 605-624) which calls `self.encode()` for each pathway title and potentially each full text. The encode method uses BioBERT inference which is the dominant cost.

Focus areas:

1. **Batch encoding**: The `SentenceTransformer.encode()` method supports batch input (list of strings). Instead of calling `self.encode(text)` in a loop for each pathway, collect all texts and encode them in a single batch call. This is the single biggest optimization opportunity — batch GPU/CPU inference is dramatically faster than sequential calls. Note: this would bypass the LRU cache on `self.encode()`, so consider whether the tradeoff is worth it.

2. **Separate title vs full-text encoding paths**: Currently both title and full-text embeddings are computed in the same loop. Separate them into two batch operations.

3. **Pre-compute entity extraction**: `self._extract_entities(pathway_title)` is called per pathway in the loop. Batch all entity extractions first, then batch encode.

4. **Minimize list-to-numpy conversions**: `np.array(pathway_title_embeddings)` at line 627 converts a list of arrays to a 2D array. Pre-allocating a numpy matrix and filling rows would avoid the conversion overhead.

5. **Optimize result dict construction**: The result-building loop (lines 644-654) creates a new dict per pathway. Consider whether this can be vectorized or simplified.

6. **Profile the encode LRU cache hit rate**: If most pathway titles are already cached from warm-up, the loop cost is dominated by dict lookups rather than inference. Measure whether cache is helping or hurting.
