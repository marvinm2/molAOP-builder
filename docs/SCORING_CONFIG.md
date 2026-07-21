# Scoring Configuration Reference

This document is the parameter reference for `scoring_config.yaml`.

**Last Updated**: 2026-07-21
**Configuration Version**: 1.5.0 (pure-semantic ranking)

> **v1.5 ranking shift (released 2026-05-10).** The pathway and GO
> suggestion ranker now uses **pure BioBERT semantic similarity**.
> The legacy hybrid weights (`gene`, `text`, `embedding`,
> `multi_evidence_bonus`) are still defined in the YAML schema for
> back-compat, but their default values are **0.0** and the suggestion
> services skip the corresponding code paths. The **KE-Pathway
> Assessment** rubric (the 4-question curator workflow) is unchanged.
>
> The "Gene-Based Scoring", "Text Similarity Scoring" and
> "Hybrid Scoring Weights" sections below are kept as a reference for
> the YAML keys that still exist; **none of them affects suggestion
> ordering in v1.5**. The v1.5 ranker is documented under
> `embedding_based_matching` (sections in `scoring_config.yaml`) and the
> small AOP-aligned tie-breaker under
> `pathway_suggestion.ontology_post_combine_boost`.

---

## Table of Contents

1. [Overview](#overview)
2. [v1.5 Ranking — Pure-Semantic + Ontology Boost](#v15-ranking--pure-semantic--ontology-boost)
3. [GO Hierarchy: IC Boost and Ancestor Redundancy](#go-hierarchy-ic-boost-and-ancestor-redundancy)
4. [Pathway Suggestion Scoring (legacy YAML keys)](#pathway-suggestion-scoring)
5. [KE-Pathway Assessment Scoring](#ke-pathway-assessment-scoring)
6. [Hybrid Scoring Weights (zeroed in v1.5)](#hybrid-scoring-weights)
7. [Parameter Interactions](#parameter-interactions)
8. [Use Cases and Examples](#use-cases-and-examples)
9. [Troubleshooting](#troubleshooting)

---

## Overview

The system has two independent scoring stages:

1. **Suggestion ranking** (backend) — orders candidate pathways / GO
   terms by BioBERT semantic similarity to the Key Event description.
   Configured under `embedding_based_matching` and
   `pathway_suggestion.ontology_post_combine_boost`.
2. **KE-Pathway / KE-GO assessment** (frontend) — a 4-question rubric
   answered by the curator after picking a target. Produces the
   High / Medium / Low confidence label. Configured under
   `ke_pathway_assessment` and `ke_go_assessment`.

Both stages are configurable via `scoring_config.yaml`.

## v1.5 Ranking — Pure-Semantic + Ontology Boost

```yaml
embedding_based_matching:
  score_transformation:
    power_exponent: 4.0      # spreads BioBERT cosine scores 0.8-0.95
                             # raise to 5.0 for sharper differentiation
  thresholds:
    base_threshold: 0.15     # minimum score to surface a candidate;
                             # lower for more candidates

pathway_suggestion:
  ontology_post_combine_boost:
    enabled: true
    weight: 0.05             # additive tie-breaker for AOP-aligned
                             # ontology terms after BioBERT scoring

  # Legacy hybrid weights — all zeroed in v1.5
  weights:
    gene: 0.0
    text: 0.0
    embedding: 0.0
    multi_evidence_bonus: 0.0
```

The runtime never multiplies by the legacy `weights` block in v1.5;
candidates are sorted purely by the transformed BioBERT score plus the
optional `ontology_post_combine_boost` additive tie-breaker.

---

## GO Hierarchy: IC Boost and Ancestor Redundancy

Both GO namespaces (`go_bp`, `go_mf`) carry a `hierarchy` block. Two values
in it are easy to misread, so they are stated explicitly here.

```yaml
go_bp:                  # identical block under go_mf
  hierarchy:
    enabled: true
    ic_weight: 0.0              # IC boost DISABLED — see below
    redundancy_threshold: 0.10  # ancestor kept only if it beats its child by >10%
```

### `ic_weight: 0.0` — the information-content boost is deliberately off

The IC boost multiplies each candidate by `(1 + ic_weight × IC_norm)` to
favour more specific GO terms. **With `ic_weight: 0.0` the multiplier is
identically 1.0, so IC never re-ranks anything.** This is the intended
deployed behaviour, not a misconfiguration.

The reasoning follows from the v1.5 pure-semantic move: once BioBERT
similarity is the sole ranking signal, re-ranking by ontology depth on top
of it promotes over-specific descendants above the term a curator actually
meant. A Key Event phrased at umbrella level ("Cell death") should return
the umbrella term, and an IC boost actively fights that.

The IC pipeline (`scripts/precompute_go_hierarchy.py`, `make go-hierarchy`)
is still run and its output still shipped, for two reasons: the same
hierarchy file supplies the `depth` value the UI displays on each suggestion
card, and it supplies the ancestor sets the redundancy filter needs.
Re-enabling the boost is a one-value config change requiring no code edit.

> Note: CHANGELOG 2.7.0 described the IC boost as "preserved" under v1.5.
> That wording is about the *machinery* being preserved, not about the boost
> being active. It read as the latter and caused #192; this section is the
> authoritative statement.

### `redundancy_threshold: 0.10`

An ancestor term is pruned from the suggestion list when a descendant of it
is also present, **unless** the ancestor's score exceeds the descendant's by
more than this fraction. At 0.10 the filter prunes fairly aggressively,
which is what pure-semantic ranking calls for — sibling terms in the same
subtree score within a few points of each other, and a looser 0.20 threshold
left near-duplicate umbrella terms cluttering the list.

One exception overrides the filter entirely: an ancestor whose label matches
the direction-stripped KE title is never pruned (#193). For a generic KE the
umbrella term *is* the intended annotation.

`tests/test_scoring_config_documented_defaults.py` pins both values against
the YAML, the code fallback defaults, and their behavioural consequence, so
these three surfaces cannot drift apart again silently.

---

## Pathway Suggestion Scoring

### Gene-Based Scoring (Refined with Pathway Specificity)

> **No longer affects ranking in v1.5.** The gene-overlap formula is still
> evaluated and the result is shown to curators as the `Genes: N/M` chip
> on each suggestion card, but the gene weight is 0.0 in the production
> hybrid combiner. The keys below are documented for parameter
> completeness.

Gene-based scoring calculates confidence when genes associated with a Key Event are found in a WikiPathways pathway. The refined formula incorporates **pathway specificity** to penalize matches in large, generic pathways and reward matches in smaller, specific pathways.

**Parameters** (`pathway_suggestion.gene_scoring`):

```yaml
gene_scoring:
  overlap_weight: 0.4                    # Weight for KE gene overlap ratio
  specificity_weight: 0.4                # Weight for pathway specificity
  specificity_scaling_factor: 10.0       # Scales specificity (0.01 → 0.1, 0.10 → 1.0)
  base_boost: 0.15                       # Baseline confidence boost
  min_genes_for_high_confidence: 3       # KE gene count penalty threshold
  low_gene_penalty: 0.8                  # Penalty for KEs with < 3 genes
  max_confidence: 0.95                   # Maximum confidence cap
```

**Refined Formula**:

```text
1. overlap_ratio = matching_genes / ke_genes
2. specificity = matching_genes / pathway_total_genes
3. specificity_boost = min(1.0, specificity × specificity_scaling_factor)
4. base_confidence = (overlap_ratio × overlap_weight) +
                     (specificity_boost × specificity_weight) + base_boost
5. ke_gene_penalty = 1.0 if ke_genes >= min_genes_for_high_confidence, else low_gene_penalty
6. confidence = min(max_confidence, base_confidence × ke_gene_penalty)
```

**Examples**:

| KE Genes | Matching | Pathway Genes | Overlap | Specificity | Confidence | Note |
| -------- | -------- | ------------- | ------- | ----------- | ---------- | ---- |
| 1        | 1        | 100           | 100%    | 1%          | **0.472**  | Low confidence: 1 gene + large pathway   |
| 5        | 5        | 50            | 100%    | 10%         | **0.95**   | High confidence: good match + specificity |
| 8        | 4        | 50            | 50%     | 8%          | **0.67**   | Medium: partial overlap                   |
| 11       | 7        | 87            | 64%     | 8%          | **0.726**  | Good: high overlap, some specificity      |
| 2        | 1        | 20            | 50%     | 5%          | **0.328**  | Low: only 2 KE genes (penalty)            |

**Calculation Breakdown (1 KE gene, 1/100 pathway genes)**:

```text
overlap_ratio = 1/1 = 1.0
specificity = 1/100 = 0.01
specificity_boost = min(1.0, 0.01 × 10) = 0.1
base_confidence = (1.0 × 0.4) + (0.1 × 0.4) + 0.15 = 0.59
ke_gene_penalty = 0.8 (only 1 gene)
confidence = 0.59 × 0.8 = 0.472
```

**Tuning Guidelines**:

- **Increase `overlap_weight`** (0.4 → 0.5): Emphasize KE gene coverage more
- **Increase `specificity_weight`** (0.4 → 0.5): Penalize large pathways more strongly
- **Increase `specificity_scaling_factor`** (10.0 → 15.0): Amplify pathway size penalty
- **Increase `base_boost`** (0.15 → 0.20): Raise all confidence scores
- **Decrease `min_genes_for_high_confidence`** (3 → 2): Be more lenient with low-gene KEs
- **Increase `low_gene_penalty`** (0.8 → 0.9): Reduce penalty for 1-2 gene KEs

**Key Improvements**:

- **Pathway Size Matters**: 1-gene match in 500-gene pathway → ~0.45 confidence (was 0.95)
- **Small Pathway Bonus**: Matching genes in smaller pathways → higher confidence
- **Gene Count Penalty**: 1-2 gene KEs are penalized (insufficient evidence)
- **Balanced Scoring**: Combines KE perspective (overlap) and pathway perspective (specificity)

**Impact**: Gene-based suggestions now show more nuanced confidence scores that reflect both gene overlap quality and pathway specificity. UI displays both KE gene ratios (e.g., "3/8 KE genes") and pathway gene ratios (e.g., "3/50 pathway genes").

### Text Similarity Scoring

> **Deprecated in v1.5** — `text` weight is 0.0; the section below
> documents YAML keys that exist but are not consulted by the production
> ranker.

Text similarity analyzes how well a pathway title/description matches the Key Event title.

**Key Parameters** (`pathway_suggestion.text_similarity`):

```yaml
text_similarity:
  important_bio_terms_weight: 2.0  # Weight multiplier for biological terms

  high_overlap_weights:            # When similarity > 0.5
    jaccard: 0.65                  # Jaccard coefficient weight
    sequence: 0.25                 # Sequence matcher weight
    substring: 0.10                # Substring score weight

  medium_overlap_weights:          # When 0.3 < similarity ≤ 0.5
    jaccard: 0.50
    sequence: 0.30
    substring: 0.20

  low_overlap_weights:             # When similarity ≤ 0.3
    jaccard: 0.40
    sequence: 0.35
    substring: 0.25
```

**Important Biological Terms** (weighted 2× by default):

- pathway, protein, gene, receptor, enzyme, metabolism
- signaling, regulation, transcription, expression
- cell, cellular, tissue, organ, biological

**Combined Similarity Formula**:

```text
title_sim = weighted_average(jaccard, sequence, substring) for title
desc_sim = weighted_average(jaccard, sequence, substring) for description
combined = (title_sim × 0.7) + (desc_sim × 0.3)
```

**Confidence Score Tiers**:

```yaml
confidence_scoring:
  tier_high:           # When combined_sim > 0.8
    threshold: 0.8
    base: 0.48
    multiplier: 0.6
    # Formula: 0.48 + (combined_sim - 0.8) × 0.6

  tier_medium:         # When 0.6 < combined_sim ≤ 0.8
    threshold: 0.6
    base: 0.30
    multiplier: 0.6

  tier_low:            # When 0.4 < combined_sim ≤ 0.6
    threshold: 0.4
    base: 0.18
    multiplier: 0.6

  tier_minimal:        # When combined_sim ≤ 0.4
    threshold: 0.0
    base: 0.08
    multiplier: 0.25
```

**Examples**:

- Similarity 0.90 (high): `0.48 + (0.90 - 0.8) × 0.6` = **0.54**
- Similarity 0.70 (medium): `0.30 + (0.70 - 0.6) × 0.6` = **0.36**
- Similarity 0.50 (low): `0.18 + (0.50 - 0.4) × 0.6` = **0.24**
- Similarity 0.25 (minimal): `0.08 + 0.25 × 0.25` = **0.1425**

### Biological Level Adjustments

Confidence scores are adjusted based on the Key Event's biological level.

**Parameters** (`pathway_suggestion.biological_level_adjustments`):

```yaml
biological_level_adjustments:
  molecular:
    boost: 0.10        # +10% confidence for molecular KEs
    rationale: "Molecular KEs closely match pathway mechanisms"

  cellular:
    boost: 0.05        # +5% confidence for cellular KEs

  tissue:
    boost: 0.00        # No adjustment for tissue KEs

  organ:
    boost: -0.03       # -3% confidence for organ KEs

  individual:
    boost: -0.05       # -5% confidence for individual KEs

  population:
    boost: -0.08       # -8% confidence for population KEs
```

**Rationale**: Molecular and cellular events are more directly represented in pathway models than higher-level phenotypic outcomes.

### Dynamic Thresholds

Controls the minimum confidence required for a pathway to appear in suggestions.

**Parameters** (`pathway_suggestion.dynamic_thresholds`):

```yaml
dynamic_thresholds:
  base_threshold: 0.25         # Default minimum confidence

  adjustments_by_specificity:
    high_specificity_terms:    # Specific processes (stricter)
      boost: 0.05              # Threshold → 0.30
      terms: ["apoptosis", "proliferation", "differentiation"]

    broad_terms:               # General processes (more lenient)
      boost: -0.05             # Threshold → 0.20
      terms: ["function", "activity", "regulation"]
```

**Effect**: Determines how many suggestions appear:

- **Lower threshold** (0.20): More suggestions, including borderline matches
- **Higher threshold** (0.30): Fewer, higher-confidence suggestions only

### Final Confidence Bounds

**Parameters** (`pathway_suggestion.confidence_final_bounds`):

```yaml
confidence_final_bounds:
  minimum: 0.08    # Floor - no score goes below this
  maximum: 0.98    # Ceiling - no score goes above this
```

**Purpose**: Prevents extreme values and maintains score interpretability.

---

## KE-Pathway Assessment Scoring

The assessment workflow guides users through 4 questions to evaluate a KE-pathway mapping.

### Question 2: Evidence Basis

**Parameters** (`ke_pathway_assessment.evidence_quality`):

```yaml
evidence_quality:
  known: 3        # Known, documented connection
  likely: 2       # Likely based on knowledge
  possible: 1     # Possible but uncertain
  uncertain: 0    # No clear basis
```

**Interpretation**: Based on user's existing knowledge (no forced research required).

### Question 3: Pathway Specificity

**Parameters** (`ke_pathway_assessment.pathway_specificity`):

```yaml
pathway_specificity:
  specific: 2     # Pathway is specific to this KE
  includes: 1     # Pathway includes this KE among others
  loose: 0        # Pathway is only loosely related
```

**Purpose**: Identifies pathways that are too broad and may need refinement.

### Question 4: KE Coverage

**Parameters** (`ke_pathway_assessment.ke_coverage`):

```yaml
ke_coverage:
  complete: 1.5   # Pathway captures complete KE mechanism
  keysteps: 1.0   # Pathway captures key steps only
  minor: 0.5      # Pathway captures minor aspects
```

**Purpose**: Identifies gaps in pathway representation of the KE.

### Biological Level Bonus

**Parameters** (`ke_pathway_assessment.biological_level`):

```yaml
biological_level:
  bonus: 1.0      # Bonus points for molecular/cellular/tissue KEs
  qualifying_levels:
    - molecular
    - cellular
    - tissue
```

**Rationale**: Molecular-level KEs are closer to pathway mechanisms than phenotypic outcomes.

### Confidence Thresholds

**Parameters** (`ke_pathway_assessment.confidence_thresholds`):

```yaml
confidence_thresholds:
  high: 5.0       # Score ≥ 5.0 → High confidence
  medium: 2.5     # Score ≥ 2.5 → Medium confidence
                  # Score < 2.5 → Low confidence
```

**Maximum Score**: 6.5 points (3 + 2 + 1.5 + 1.0 bonus)

**Scoring Formula**:

```text
base_score = evidence_quality + pathway_specificity + ke_coverage
final_score = base_score + (biological_level_bonus if applicable)

if final_score ≥ 5.0: confidence = "high"
elif final_score ≥ 2.5: confidence = "medium"
else: confidence = "low"
```

**Examples**:

- Known + Specific + Complete + Molecular: `3 + 2 + 1.5 + 1 = 7.5` → **High** (capped at 6.5)
- Likely + Includes + Key steps + No bonus: `2 + 1 + 1.0 = 4.0` → **Medium**
- Possible + Loose + Minor + No bonus: `1 + 0 + 0.5 = 1.5` → **Low**

---

## Hybrid Scoring Weights

> **Zeroed in v1.5.** The production ranker is BioBERT-only (see
> [v1.5 Ranking — Pure-Semantic + Ontology Boost](#v15-ranking--pure-semantic--ontology-boost)).
> The block below documents the YAML keys for back-compat and explains
> the historical rationale; the listed defaults below are the *legacy*
> values, not what ships in v1.5.

The original pathway suggestion engine combined three independent matching methods using configurable weights.

**Parameters** (`pathway_suggestion.hybrid_weights`):

```yaml
# v1.5 production defaults — all zeroed:
#   gene: 0.0
#   text: 0.0
#   embedding: 0.0
#   multi_evidence_bonus: 0.0
#
# Historical (pre-v1.5) defaults shown below for reference only.
hybrid_weights:
  gene: 0.35          # Gene-based scoring weight (legacy)
  text: 0.25          # Text-based scoring weight (legacy)
  embedding: 0.40     # Semantic/BioBERT scoring weight (legacy)
  multi_evidence_bonus: 0.05  # Bonus when multiple methods agreed (legacy)
```

### Current Weights and Rationale

| Method             | Weight | Rationale                                                        |
| ------------------ | ------ | ---------------------------------------------------------------- |
| **Gene-Based**     | 35%    | Provides unique biological validation signal from gene associations |
| **Text-Based**     | 25%    | Reduced weight due to 67% coverage and lower average accuracy    |
| **Semantic/BioBERT** | 40%  | Highest weight - 100% coverage with average score of 0.864       |

**Why These Weights?**

Extended testing across 15 diverse Key Events revealed significant differences in method performance:

1. **Semantic Matching (40%)**
   - 100% coverage: Always produces relevant suggestions
   - Average score: 0.864 - consistently high accuracy
   - Captures biological meaning beyond exact terminology

2. **Gene-Based Matching (35%)**
   - Provides unique biological signal not available from text methods
   - High confidence when genes match (biological validation)
   - Coverage varies by KE (depends on gene associations in AOP-Wiki)

3. **Text-Based Matching (25%)**
   - 67% coverage: Misses pathways with different terminology
   - Average score: 0.548 - lower accuracy than semantic
   - Valuable for exact terminology matches but less robust

**Multi-Evidence Bonus**: When a pathway is suggested by multiple methods, it receives a +5% confidence bonus, reinforcing findings through independent validation.

### Tuning Hybrid Weights

**Emphasize Biological Validation:**

```yaml
hybrid_weights:
  gene: 0.45
  text: 0.20
  embedding: 0.35
```

**Emphasize Semantic Understanding:**

```yaml
hybrid_weights:
  gene: 0.30
  text: 0.20
  embedding: 0.50
```

**Equal Weighting (Exploratory):**

```yaml
hybrid_weights:
  gene: 0.33
  text: 0.33
  embedding: 0.34
```

---

## Parameter Interactions

### Gene vs Text vs Semantic Balance

When multiple matching methods produce suggestions for the same pathway:

- **Semantic-based provides broadest coverage** (100% of KEs get suggestions)
- **Gene-based provides biological validation** (high confidence when available)
- **Text-based provides terminology matching** (exact matches score well)

**Balancing Strategy**:

- To emphasize biological validation: Increase `hybrid_weights.gene`
- To emphasize semantic understanding: Increase `hybrid_weights.embedding`
- To emphasize exact terminology: Increase `hybrid_weights.text`

### Threshold vs Confidence Relationship

```text
dynamic_threshold ← controls → number of suggestions
confidence_scoring ← controls → suggestion quality/ranking
```

- **High threshold + High confidence parameters**: Very few, very confident suggestions
- **Low threshold + High confidence parameters**: Many suggestions, well-ranked
- **High threshold + Low confidence parameters**: Few suggestions, conservative scores
- **Low threshold + Low confidence parameters**: Many suggestions, low scores

### Assessment Score Distribution

The 4-question assessment produces scores roughly distributed as:

- **High (≥5.0)**: ~20-30% of mappings (strong evidence + good specificity)
- **Medium (2.5-5.0)**: ~50-60% of mappings (moderate quality)
- **Low (<2.5)**: ~10-20% of mappings (weak or uncertain)

**Adjusting Distribution**:

- More High ratings: Lower `high` threshold (5.0 → 4.5)
- Fewer Low ratings: Lower `medium` threshold (2.5 → 2.0)
- Stricter overall: Increase both thresholds

---

## Use Cases and Examples

### Use Case 1: Demo/Presentation Mode

**Goal**: Show more suggestions to demonstrate system capabilities.

**Changes**:

```yaml
pathway_suggestion:
  gene_scoring:
    base_boost: 0.20        # Up from 0.15

  dynamic_thresholds:
    base_threshold: 0.18    # Down from 0.25

  confidence_final_bounds:
    minimum: 0.05           # Down from 0.08
```

**Effect**: More pathways appear in suggestions, including borderline matches.

### Use Case 2: Research Mode (Conservative)

**Goal**: Only show high-confidence, well-validated suggestions.

**Changes**:

```yaml
pathway_suggestion:
  gene_scoring:
    base_boost: 0.12        # Down from 0.15

  dynamic_thresholds:
    base_threshold: 0.35    # Up from 0.25

  text_similarity:
    important_bio_terms_weight: 1.5  # Down from 2.0
```

**Effect**: Fewer suggestions, but higher quality and more reliable.

### Use Case 3: Gene-Focused Analysis

**Goal**: Prioritize gene overlap heavily over text matching.

**Changes**:

```yaml
pathway_suggestion:
  gene_scoring:
    multiplier: 0.92        # Up from 0.85
    base_boost: 0.22        # Up from 0.15
    max_confidence: 0.98    # Up from 0.95

  dynamic_thresholds:
    base_threshold: 0.20    # Down from 0.25
```

**Effect**: Gene-based suggestions dominate results, partial overlaps still shown.

### Use Case 4: Lenient Assessment

**Goal**: More mappings qualify as "high confidence".

**Changes**:

```yaml
ke_pathway_assessment:
  confidence_thresholds:
    high: 4.0              # Down from 5.0
    medium: 2.0            # Down from 2.5

  biological_level:
    bonus: 1.2             # Up from 1.0
```

**Effect**: ~40-50% of mappings reach "high" confidence instead of ~20-30%.

### Use Case 5: Strict Curation

**Goal**: High bar for accepting mappings, identify weak ones.

**Changes**:

```yaml
ke_pathway_assessment:
  evidence_quality:
    known: 3.5             # Up from 3
    likely: 2.2            # Up from 2

  confidence_thresholds:
    high: 5.5              # Up from 5.0
    medium: 3.0            # Up from 2.5
```

**Effect**: Fewer "high" ratings, clearer distinction between quality levels.

---

## Troubleshooting

### No Suggestions Appearing

**Possible Causes**:

1. **Threshold too high**: Check `dynamic_thresholds.base_threshold`
2. **No genes found**: KE may lack gene associations in AOP-Wiki
3. **Text similarity too low**: KE title doesn't match pathway terminology

**Solutions**:

```yaml
# Lower threshold temporarily
dynamic_thresholds:
  base_threshold: 0.15    # Try 0.15 instead of 0.25

# Check if gene-based matching is working
# Test with KE 1508 (CYP2E1) - should find 8 pathways
```

### Too Many Suggestions

**Possible Causes**:

1. **Threshold too low**: Many borderline matches appearing
2. **Base boost too high**: Even poor matches get inflated scores

**Solutions**:

```yaml
# Raise threshold
dynamic_thresholds:
  base_threshold: 0.30    # Up from 0.25

# Reduce base boost
gene_scoring:
  base_boost: 0.12        # Down from 0.15
```

### Gene-Based Scores Too Low

**Check**:

1. Is `gene_scoring.multiplier` too low?
2. Is `gene_scoring.base_boost` too low?
3. Are genes being found? (Check browser console/logs)

**Solutions**:

```yaml
gene_scoring:
  multiplier: 0.90        # Up from 0.85
  base_boost: 0.20        # Up from 0.15
```

### Assessment Always Shows "Low" Confidence

**Check**:

1. Are thresholds too high?
2. Is biological level bonus applying?
3. Are users selecting "uncertain" / "loose" / "minor" frequently?

**Solutions**:

```yaml
confidence_thresholds:
  high: 4.5              # Down from 5.0
  medium: 2.0            # Down from 2.5

biological_level:
  bonus: 1.2             # Up from 1.0
```

### Config Changes Not Reflected

**Checklist**:

1. Saved `scoring_config.yaml`?
2. Valid YAML syntax? Test with: `python -c "import yaml; yaml.safe_load(open('scoring_config.yaml'))"`
3. Restarted Flask? `pkill -f "python.*app.py" && python app.py &`
4. Cleared browser cache? (Ctrl+Shift+R)
5. Check browser console for "Scoring config loaded" message

**Validation**:

```bash
# Test YAML syntax
python -c "import yaml; print(yaml.safe_load(open('scoring_config.yaml')))"

# Check Flask logs
tail -f /tmp/flask_test.log | grep -i config

# Test API endpoint
curl http://localhost:5000/api/scoring-config | python -m json.tool
```

---

## Advanced Topics

### Custom Biological Terms

To add domain-specific terms to the important terms list, edit:

```yaml
text_similarity:
  important_bio_terms_weight: 2.0
  custom_important_terms:
    - "inflammation"
    - "oxidative"
    - "mitochondrial"
```

**Note**: Requires code modification in `pathway_suggestions.py` to implement.

### Combining Multiple KEs

For analyzing pathways relevant to multiple KEs:

1. Lower `base_threshold` to see broader suggestions
2. Increase `gene_scoring.multiplier` to reward multi-KE gene overlaps
3. Test with pathway search rather than single KE suggestions

### Performance Considerations

**Caching**:

- SPARQL queries cached for 24 hours
- Frontend config cached for 5 minutes
- Changing config requires Flask restart

**Impact of Parameter Changes**:

- Threshold changes: Immediate effect on suggestion count
- Confidence formula changes: Affects ranking/display
- No performance penalty from config complexity

---

## BioBERT Score Transformation

### Problem: Inflated Semantic Similarity Scores

Raw BioBERT cosine similarities tend to cluster at 0.85-0.95 for all biomedical text because:

- Biological texts share common vocabulary (gene, protein, pathway, etc.)
- Pre-trained on PubMed means all biomedical texts have baseline similarity
- Cosine similarity rarely goes negative for biomedical domain

This makes it hard to differentiate between excellent matches and marginal ones.

### Solution: Power Transformation

The score transformation applies `score^exponent` to spread scores across a wider range.

**Parameters** (`pathway_suggestion.embedding_based_matching.score_transformation`):

```yaml
score_transformation:
  method: "power"           # Options: power, linear, none
  power_exponent: 1.5       # score^1.5 compresses high end
  scale_factor: 0.75        # For linear method only
  output_min: 0.0           # Floor
  output_max: 0.85          # Ceiling
```

### Transformation Methods

**Power Method** (`method: "power"`):

- Formula: `transformed = base_score ^ power_exponent`
- Effect: Compresses high scores, spreads distribution
- Recommended for BioBERT similarity

**Linear Method** (`method: "linear"`):

- Formula: `transformed = base_score × scale_factor`
- Effect: Simple scaling down of all scores
- Use for quick adjustments

**None** (`method: "none"`):

- No transformation, uses raw normalized scores
- Use for debugging or comparison

### Score Transformation Effects

| Raw Cosine | Normalized (0-1) | Power 1.5  | Power 2.0  |
| ---------- | ---------------- | ---------- | ---------- |
| 0.90       | 0.95             | **0.82**   | 0.70       |
| 0.80       | 0.90             | **0.72**   | 0.56       |
| 0.70       | 0.85             | **0.62**   | 0.46       |
| 0.60       | 0.80             | **0.52**   | 0.36       |
| 0.50       | 0.75             | **0.43**   | 0.25       |
| 0.30       | 0.65             | **0.33**   | 0.17       |

### Tuning the Exponent

- **Lower exponent (1.2-1.4)**: Less aggressive compression, scores stay higher
- **Default exponent (1.5)**: Good balance for typical biomedical queries
- **Higher exponent (1.8-2.0)**: More aggressive, only very good matches score high

### Examples

#### Scenario: Scores Too High

```yaml
# Problem: Even poor matches score 0.8+
# Solution: Increase exponent
score_transformation:
  method: "power"
  power_exponent: 2.0      # More aggressive
  output_max: 0.80         # Lower ceiling
```

#### Scenario: Scores Too Low

```yaml
# Problem: Good matches only scoring 0.4-0.5
# Solution: Decrease exponent or use linear
score_transformation:
  method: "power"
  power_exponent: 1.2      # Less aggressive
  output_max: 0.90         # Higher ceiling
```

#### Scenario: Compare Before/After

```yaml
# Turn off transformation to see raw scores
score_transformation:
  method: "none"
```

---

## Configuration File Template

See the actual `scoring_config.yaml` file for the complete configuration with inline comments and default values.

---

## Version History

- **v1.2.0** (2026-01-26): Updated hybrid scoring weights - Changed weights based on extended testing across 15 KEs:
  - Gene: 35% (unchanged - unique biological signal)
  - Text: 35% → 25% (reduced - 67% coverage, avg score 0.548)
  - Semantic: 30% → 40% (increased - 100% coverage, avg score 0.864)
  - Added comprehensive hybrid weights documentation section
  - Added rationale for weight choices based on empirical testing

- **v1.1.0** (2026-01-23): BioBERT score transformation
  - Power transformation to address inflated semantic similarity scores
  - Configurable exponent, scale factor, and output bounds
  - Comprehensive scoring documentation added

- **v1.0.0** (2026-01-13): Initial configurable scoring system
  - 65+ parameters externalized
  - Gene-based pathway matching fixed
  - Full frontend/backend integration

---

## Support

For issues or questions:

1. Check Flask logs: `tail -f /tmp/flask_test.log`
2. Validate YAML: `python -c "import yaml; yaml.safe_load(open('scoring_config.yaml'))"`
3. Review CLAUDE.md "Scoring Configuration System" section
4. Test with known working KE (e.g., KE 1508 - CYP2E1)

---

**Last Updated**: 2026-03-03
**Configuration Version**: 1.2.0
**Application Version**: v2.5.0
