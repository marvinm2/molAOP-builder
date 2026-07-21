## Target
scripts/precompute_go_embeddings.py

## Metric
python benchmarks/bench_parse_obo_file.py

## Direction
lower

## Iterations
20

## Constraints
- The function signature of `parse_obo_file(obo_path, namespace_value)` must not change
- The return value must be identical: dict of {go_id: {name, definition, is_a[], part_of[], synonyms[], direction}}
- The `detect_go_direction` call must still be applied to each term
- Do not modify any function outside of `parse_obo_file` (lines 62-150)
- Do not add external dependencies beyond what is already in requirements.txt
- The function must correctly parse the standard OBO format
- Do not change other functions in the file (download_go_obo, precompute_go_embeddings, etc.)

## Research Direction
This function parses a 36MB / 642K-line OBO file line by line. It is primarily I/O-bound with string operations.

Focus areas:

1. **Batch file reading**: Instead of iterating line-by-line with `for line in f`, read the entire file into memory at once (`f.read()`) and split on `\n`. This avoids per-line I/O overhead and Python iterator overhead.

2. **Reduce `line.strip()` overhead**: Consider reading the whole file and using `splitlines()`, which avoids the need for per-line strip.

3. **Optimize string prefix checks**: The cascade of `if line.startswith(...)` checks runs for every line. Consider:
   - Using a dispatch dict keyed on the first word/prefix
   - Checking the first character first (e.g., `line[0]` == 'i', 'n', 'd', 's', 'r') before the full startswith

4. **Compile regex patterns once**: `re.match(r'def: "(.+?)"', line)` and the synonym regex are called inside the loop but the patterns are not pre-compiled. Use `re.compile()` outside the loop.

5. **Skip non-target namespaces early**: Once a term's namespace is known (after parsing `namespace:` line), if it does not match `namespace_value`, skip all subsequent lines for that term until the next `[Term]` block. This avoids parsing definition, is_a, relationships, and synonyms for terms we will discard.

6. **Reduce dict creation overhead**: Each `[Term]` creates a full dict with 8 keys. Consider using simpler variables and only creating the final dict for matching terms.
