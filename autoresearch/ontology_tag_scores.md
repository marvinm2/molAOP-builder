## Target
src/suggestions/pathway.py

## Metric
python benchmarks/bench_ontology_tag_scores.py

## Direction
lower

## Iterations
20

## Constraints
- The method signature of `_compute_ontology_tag_scores(self, ke_title, ke_id, limit)` must not change
- The return value format must remain identical: list of pathway dicts with confidence_score, suggestion_type, match_types, primary_evidence, and ontology_match_details
- All existing tests must pass: `python -m pytest tests/ -q`
- Do not modify any method outside of `_compute_ontology_tag_scores` (lines 563-661)
- Do not remove the fuzzy matching capability (SequenceMatcher or equivalent)
- The scoring formula (exact_match_boost, fuzzy_match_boost, max_confidence, min_threshold) must use the same config values
- Do not add external dependencies beyond what is already in requirements.txt

## Research Direction
The critical bottleneck is the O(pathways x keywords x tags) loop with `SequenceMatcher` at its core (line 621). This function processes ~1012 pathways, each with ~2 ontology tags, against ~5 extracted keywords. That is ~10,000 SequenceMatcher calls per invocation.

Focus areas:

1. **Replace SequenceMatcher with faster alternatives**: `SequenceMatcher.ratio()` creates a new object per comparison. Consider:
   - Pre-compute SequenceMatcher objects and reuse `.set_seq2()` instead of creating new ones
   - Use `SequenceMatcher.quick_ratio()` as a fast pre-filter before calling `.ratio()`

2. **Early termination in the inner loop**: The `break` after finding a match for a keyword already helps, but consider breaking out of keywords too once enough matches are found.

3. **Pre-filter pathways**: Skip pathways with zero tags earlier (already done with `if not tags: continue`). Consider also pre-lowercasing and pre-cleaning all tags once before the loop.

4. **Batch string operations**: Pre-compute `self._clean_text(tag)` for all tags across all pathways ONCE before entering the scoring loop, since `_clean_text` is called repeatedly for the same tags.

5. **Use set-based exact matching**: For the exact substring check (`keyword in tag_clean or tag_clean in keyword`), consider building an inverted index of tag tokens for O(1) lookup.

6. **Reduce object creation in results**: The `{**pathway, ...}` spread and nested dict creation in the scored_pathways list can be deferred or simplified.
