"""Ontology-propagated GO gene annotations (#208).

`data/go_{ns}_gene_annotations.json` holds **direct** GAF annotations only: a
gene appears under the exact term it was annotated to and nowhere else. That
violates the GO true-path rule — a gene annotated to a term is annotated to all
of its ancestors — and it made every GO-derived Key Event gene set smaller than
it should be, sometimes inverted:

    GO:0006915  apoptotic process        667 genes
    GO:0012501  programmed cell death     35   <- its parent
    GO:0008219  cell death                 7   <- its grandparent

A parent cannot be smaller than its child. The practical damage was to
curation: good KE->GO practice picks the most descriptive term that still
faithfully covers the event, which for a generic Key Event such as "Increase,
Cell death" is GO:0008219 — and that resolved to 7 genes, at or below the
Analyser's MIN_KE_GENES floor. The semantically right answer produced a useless
gene set, so curators were pushed toward over-specific terms.

Preferred source is `data/go_{ns}_gene_annotations_propagated.json`, written by
`scripts/precompute_go_hierarchy.py`, which already computes the closure in the
course of its IC calculation. Falling back to computing the closure here is
supported so a fresh checkout or CI run (where `data/*.json` is gitignored)
still works, but it is a lossy reconstruction: the precompute script remaps
obsolete-term annotations onto their replacements using the OBO before
propagating, and this module has no OBO. On the current corpus that leaves 178
of 24547 BP terms slightly below their recorded `propagated_gene_count`.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_ANNOTATIONS = "data/go_{ns}_gene_annotations.json"
DEFAULT_PROPAGATED = "data/go_{ns}_gene_annotations_propagated.json"
DEFAULT_HIERARCHY = "data/go_{ns}_hierarchy.json"

# {cache key: (source fingerprint, value)}. The fingerprint is what makes the
# cache safe to hold indefinitely: data/ is a bind mount in every deployment,
# so the corpus can be refreshed under a running process. A plain
# populate-once dict served the pre-refresh gene sets for the lifetime of the
# worker, and the GMT export layer — which fingerprints the same files to
# decide staleness — would then regenerate, reproduce the old content, and
# stamp the *new* corpus fingerprint on it, asserting a corpus version the
# file does not contain and never retrying.
_cache = {}


def _source_fingerprint(paths):
    """(size, mtime) of each file a cache entry was built from.

    Three stat() calls against a JSON parse of tens of megabytes, so this is
    still a cache in every sense that matters; it just cannot go on serving a
    corpus that is no longer on disk.
    """
    parts = []
    for path in paths:
        if not path:
            continue
        try:
            st = os.stat(path)
            parts.append(f"{path}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{path}:-")
    return ";".join(parts)


def _read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not read %s: %s", path, e)
        return None


def build_closure(direct, hierarchy):
    """Union each term's direct genes into the term and all of its ancestors.

    Annotation keys absent from the hierarchy are skipped — they are obsolete
    or out-of-namespace terms the hierarchy deliberately excludes (66 BP terms,
    285 gene-term pairs on the current corpus).
    """
    closure = {}
    for go_id, genes in direct.items():
        entry = hierarchy.get(go_id)
        if entry is None:
            continue
        targets = [go_id]
        ancestors = entry.get("ancestors") or ()
        targets.extend(ancestors)
        for target in targets:
            bucket = closure.get(target)
            if bucket is None:
                closure[target] = bucket = set()
            bucket.update(genes)
    return {go_id: sorted(genes) for go_id, genes in closure.items()}


def get_go_annotations(
    namespace="bp",
    annotations_path=None,
    propagated_path=None,
    hierarchy_path=None,
    use_cache=True,
):
    """Return {go_id: [gene symbols]} with ontology propagation applied.

    Degrades in three steps, each logged: the precomputed propagated file, then
    a closure built from the direct annotations plus the hierarchy, then the
    direct annotations unchanged. The last is wrong under the true-path rule but
    is what the application did before #208, so it is a safe floor rather than a
    hard failure.
    """
    ns = namespace.lower()
    propagated_path = propagated_path or DEFAULT_PROPAGATED.format(ns=ns)
    annotations_path = annotations_path or DEFAULT_ANNOTATIONS.format(ns=ns)
    hierarchy_path = hierarchy_path or DEFAULT_HIERARCHY.format(ns=ns)

    fingerprint = _source_fingerprint(
        (propagated_path, annotations_path, hierarchy_path)
    )
    if use_cache and ns in _cache:
        cached_fingerprint, cached = _cache[ns]
        if cached_fingerprint == fingerprint:
            return cached
        logger.info(
            "GO %s annotation corpus changed on disk — reloading", ns.upper()
        )

    result = _read_json(propagated_path)
    if result is not None:
        logger.info(
            "Loaded %d propagated GO %s annotations from %s",
            len(result), ns.upper(), propagated_path,
        )
    else:
        direct = _read_json(annotations_path) or {}
        hierarchy = _read_json(hierarchy_path)
        if direct and hierarchy:
            result = build_closure(direct, hierarchy)
            logger.warning(
                "%s not found — built the GO %s closure at load time from %d "
                "direct terms (%d propagated). This is a lossy reconstruction: "
                "obsolete-term remapping needs the OBO. Regenerate with "
                "scripts/precompute_go_hierarchy.py.",
                propagated_path, ns.upper(), len(direct), len(result),
            )
        else:
            result = direct
            if direct:
                logger.warning(
                    "No GO %s hierarchy at %s — serving DIRECT annotations, "
                    "which violate the true-path rule (#208). Generic terms "
                    "will resolve to near-empty gene sets.",
                    ns.upper(), hierarchy_path,
                )

    if use_cache:
        _cache[ns] = (fingerprint, result)
    return result


def get_go_annotations_merged(**kwargs):
    """BP and MF annotations in one dict, BP first so MF wins on collision.

    Mirrors the existing dict.update() ordering in gmt_exporter. The two
    namespaces are disjoint in practice.

    Note MF is NOT yet propagated: data/go_mf_hierarchy.json predates the
    propagated_gene_count field and the MF corpus is unfiltered, so umbrella
    terms such as GO:0003824 "catalytic activity" (5614 genes after closure)
    would enter gene evidence unbounded. Tracked separately; MF therefore falls
    through to its direct annotations here.
    """
    merged = dict(get_go_annotations("bp", **kwargs))
    merged.update(get_go_annotations("mf", **kwargs))
    return merged


def get_go_direct_counts(namespace="bp", annotations_path=None, use_cache=True):
    """Return {go_id: direct gene count} straight from the GAF-derived file.

    The propagated count answers "how many genes will this term actually test",
    which is what the Analyser's threshold acts on. The direct count answers
    "how much of that is annotated to this term itself" — a term with 891
    propagated and 7 direct is well-populated but only indirectly evidenced.
    Curators judging a mapping want both (#210), so both are surfaced rather
    than one standing in for the other.
    """
    ns = namespace.lower()
    key = f"{ns}:direct"
    path = annotations_path or DEFAULT_ANNOTATIONS.format(ns=ns)
    fingerprint = _source_fingerprint((path,))
    if use_cache and key in _cache:
        cached_fingerprint, cached = _cache[key]
        if cached_fingerprint == fingerprint:
            return cached

    raw = _read_json(path) or {}
    counts = {go_id: len(genes) for go_id, genes in raw.items()}
    if use_cache:
        _cache[key] = (fingerprint, counts)
    return counts


def reset_cache():
    """Drop the process-wide cache.

    Entries invalidate themselves when their source files change (see
    `_source_fingerprint`), so this is only needed when a test wants a cold
    load regardless of mtime.
    """
    _cache.clear()
