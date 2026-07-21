## Target
src/suggestions/scoring.py

## Metric
python benchmarks/bench_combine_scored_items.py

## Direction
lower

## Iterations
20

## Constraints
- The function signature of `combine_scored_items()` must not change (same parameters, same defaults)
- The return value must be identical: list of dicts sorted by hybrid_score descending, each containing signal_scores, hybrid_score, match_types, and _signal_data keys
- All existing tests must pass: `python -m pytest tests/ -q`
- Do not remove any functionality or fields from the output dicts
- Do not add external dependencies (no new pip packages)
- The function must remain a standalone module-level function (not a class method)
- Do not modify any other file besides src/suggestions/scoring.py

## Research Direction
This is a pure Python function doing dictionary merges and arithmetic. Focus areas:

1. **Reduce dictionary allocations**: The inner loop creates a new dict with `{**item, 'signal_scores': ...}` for each first-seen item. Consider pre-allocating or reusing structures.

2. **Avoid repeated dict lookups**: `entry['signal_scores'][signal_name]` and `entry['match_types']` are accessed inside the hot loop. Local variable binding could help.

3. **Replace list-based match_types tracking**: Using a list + `not in` check is O(n). A set would be O(1) for the membership test (convert to list at the end).

4. **Optimize the hybrid score calculation**: The generator expression `sum(scores[signal_name] * weights[signal_name] for signal_name in weights)` could be replaced with a pre-computed dot product or avoided if scores are stored in array form.

5. **Consider using `operator.itemgetter`** for the final sort instead of a lambda.

6. **Profile whether the `{**item, ...}` spread operator** is the bottleneck vs the loop iteration itself.
