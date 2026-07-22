"""Deposit assembly for the Zenodo release pipeline.

Pure-Python helpers that turn the curated mapping rows into the v3 deposit
shape: three per-resource ZIP archives (KE-WikiPathways.zip, KE-GO.zip,
KE-Reactome.zip), each containing GMT files split by confidence level plus
a Turtle file with full provenance, alongside a README that quantifies
per-tier counts and pins the upstream snapshot.

This module is the canonical source of the v3 layout. Both call sites use
it:

- `scripts/publish_zenodo.py` — the CLI / cron path that minted the first
  three Zenodo versions out-of-band.
- `src/blueprints/admin.py:publish_zenodo` — the in-app admin route, also
  surfaced from the UI as a "Publish to Zenodo" button.

No Flask, no HTTP — keeps the assembly testable in isolation. Zenodo API
I/O lives in `zenodo_uploader.py`; meta-file persistence is in
`zenodo_uploader.persist_meta_with_fallback`.
"""
from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from typing import Callable, Optional


# ---------- counts + change detection ----------

def counts(rows: list) -> dict:
    """Confidence-tier histogram for a list of mapping rows.

    Returns keys All / High / Medium / Low. "All" is the row count
    regardless of confidence; the named tiers count rows whose
    `confidence_level` matches case-insensitively. Rows with NULL or
    unrecognised confidence are still counted under All but not under
    any tier.
    """
    c = Counter((r.get("confidence_level") or "").lower() for r in rows)
    return {
        "All": len(rows),
        "High": c.get("high", 0),
        "Medium": c.get("medium", 0),
        "Low": c.get("low", 0),
    }


def changes_significant(current: dict, last: Optional[dict], min_delta: int) -> bool:
    """Whether the All-tier totals shifted by at least `min_delta` overall.

    `current` and `last` are dicts of the shape produced by `counts` keyed
    by resource: {"wp": {...}, "go": {...}, "reactome": {...}}. If `last`
    is empty (no prior deposit) the change is always significant.
    """
    if not last:
        return True
    delta = (
        abs(current["wp"]["All"] - last.get("wp", {}).get("All", 0))
        + abs(current["go"]["All"] - last.get("go", {}).get("All", 0))
        + abs(current["reactome"]["All"] - last.get("reactome", {}).get("All", 0))
    )
    return delta >= min_delta


# ---------- source-version helpers ----------

def slice_source_versions(manifest: dict, *resources: str) -> dict:
    """Return a manifest slice containing only the named upstream resources.

    Used by `build_resource_zip` to attach a per-resource sidecar without
    leaking unrelated sources into each ZIP. Empty dict if no listed
    resource is present.
    """
    if not manifest:
        return {}
    sources = manifest.get("sources", {})
    slice_sources = {k: sources[k] for k in resources if k in sources}
    if not slice_sources:
        return {}
    return {
        "captured_at": manifest.get("captured_at"),
        "sources": slice_sources,
    }


def format_versions_for_prose(manifest: dict) -> str:
    """One-line summary like "WP 2026-05-10 · GO 2026-01-23 · ..." for prose.

    Empty string if no source is in OK state — callers should branch on
    truthiness to avoid emitting an awkward bare colon.
    """
    sources = (manifest or {}).get("sources", {})
    tokens = []
    if sources.get("wikipathways", {}).get("status") == "ok":
        tokens.append(f"WP {sources['wikipathways'].get('release_date', '?')}")
    if sources.get("gene_ontology", {}).get("status") == "ok":
        tokens.append(f"GO {sources['gene_ontology'].get('release_date', '?')}")
    rx = sources.get("reactome", {})
    if rx.get("status") == "ok":
        token = f"Reactome v{rx.get('release_version', '?')}"
        if rx.get("release_date"):
            token += f" ({rx['release_date']})"
        tokens.append(token)
    if sources.get("aopwiki", {}).get("status") == "ok":
        tokens.append(f"AOP-Wiki {sources['aopwiki'].get('snapshot_date', '?')}")
    return " · ".join(tokens)


def format_snapshot_table_md(manifest: Optional[dict]) -> str:
    """Markdown table of the source-version manifest, or a fallback note."""
    sources = (manifest or {}).get("sources", {})
    if not sources:
        return "_Snapshot manifest unavailable for this deposit._"

    rows = ["| Resource | Release | Captured |", "|----------|---------|----------|"]
    _labels = [
        ("wikipathways", "WikiPathways", "release_date"),
        ("gene_ontology", "Gene Ontology", "release_date"),
        ("reactome", "Reactome", None),
        ("aopwiki", "AOP-Wiki", "snapshot_date"),
    ]
    for key, label, primary in _labels:
        entry = sources.get(key, {})
        if entry.get("status") != "ok":
            value = "_unknown_"
        elif key == "reactome":
            v = f"v{entry.get('release_version', '?')}"
            if entry.get("release_date"):
                v += f" ({entry['release_date']})"
            value = v
        else:
            value = entry.get(primary, "—")
        captured = (entry.get("captured_at") or "")[:10] or "—"
        rows.append(f"| {label} | {value} | {captured} |")
    return "\n".join(rows)


# ---------- deposit assembly ----------

def build_resource_zip(
    prefix: str,
    gmt_fn: Callable,
    ttl_fn: Callable,
    mappings: list,
    today: str,
    gmt_kwargs: Optional[dict] = None,
    source_versions_slice: Optional[dict] = None,
) -> bytes:
    """Build one per-resource ZIP archive for the Zenodo deposit.

    Layout inside the ZIP:
        {prefix}/{prefix}_{today}_{Level}.gmt   (one per non-empty tier)
        {prefix}/{prefix.lower()}-mappings.ttl  (full provenance)
        {prefix}/source_versions.json           (optional sidecar)

    A `_Level.gmt` file is omitted if the level has zero mappings — that's
    the convention v3 established.
    """
    buf = io.BytesIO()
    gmt_kwargs = gmt_kwargs or {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for label, conf in (
            ("All", None),
            ("High", "high"),
            ("Medium", "medium"),
            ("Low", "low"),
        ):
            # `confidence=`, not `min_confidence=`: the deposit's tier files are
            # a partition, which is what build_readme() documents and what
            # counts() histograms. Since #206 min_confidence is a threshold, so
            # using it here would make _Medium.gmt cumulative and _Low.gmt a
            # byte-copy of _All.gmt, contradicting the README of a citable,
            # permanent deposit.
            content = gmt_fn(mappings, confidence=conf, **gmt_kwargs)
            if content:
                zf.writestr(f"{prefix}/{prefix}_{today}_{label}.gmt", content)
        ttl_content = ttl_fn(mappings)
        if ttl_content:
            zf.writestr(f"{prefix}/{prefix.lower()}-mappings.ttl", ttl_content)
        if source_versions_slice:
            zf.writestr(
                f"{prefix}/source_versions.json",
                json.dumps(source_versions_slice, indent=2, ensure_ascii=False) + "\n",
            )
    return buf.getvalue()


def build_readme(
    today: str,
    wp_n: dict,
    go_n: dict,
    rx_n: dict,
    source_versions: Optional[dict] = None,
) -> bytes:
    snapshot_table = format_snapshot_table_md(source_versions)
    return (
        f"""# Molecular AOP Builder — Curated KE → WikiPathways / GO / Reactome Mappings

**Published:** {today}
**Source application:** https://molaop-builder.vhp4safety.nl
**Repository:** https://github.com/marvinm2/molAOP-builder
**License:** CC0 1.0 Universal (public domain dedication)

This deposit contains the current curated mappings between Key Events (KEs) of the Adverse Outcome Pathway framework and three molecular-pathway / ontology resources. Each mapping has been proposed by a curator, scored by a BioBERT-based suggestion engine, assessed against a structured confidence rubric, and approved by an administrator before inclusion in the dataset.

## Contents

The deposit is organised as three per-resource ZIP archives plus this README:

| Archive                  | Resource                                              | Approved mappings (this version) |
|--------------------------|-------------------------------------------------------|----------------------------------|
| `KE-WikiPathways.zip`     | WikiPathways pathways (`WP####`)                      | **{wp_n['All']}** total · {wp_n['High']} High · {wp_n['Medium']} Medium · {wp_n['Low']} Low |
| `KE-GO.zip`               | Gene Ontology Biological Process / Molecular Function | **{go_n['All']}** total · {go_n['High']} High · {go_n['Medium']} Medium · {go_n['Low']} Low |
| `KE-Reactome.zip`         | Reactome pathways (`R-HSA-#######`)                   | **{rx_n['All']}** total · {rx_n['High']} High · {rx_n['Medium']} Medium · {rx_n['Low']} Low |

Each archive expands to a folder containing:

- `*_{{YYYY-MM-DD}}_{{Level}}.gmt` — Gene Matrix Transposed (GMT) gene-set files, one row per KE → pathway/GO mapping. Gene identifiers are HGNC symbols. Loadable directly by clusterProfiler (`enricher()`) and fgsea (`gmtPathways()`).
- `*-mappings.ttl` — RDF / Turtle serialisation with full provenance for every approved mapping for that resource: proposer, approving curator, approval timestamp, BioBERT suggestion score, confidence level, connection type. Suitable for SPARQL queries and ontology integration.
- `source_versions.json` — snapshot manifest pinning this archive to a specific release of each upstream resource (e.g. WikiPathways 2026-05-10, AOP-Wiki 2026-05-06). Mappings approved before the source-versioning rollout have NULL fields on the row itself; the manifest documents the snapshot the dataset reached parity with at backfill time.

## Confidence levels

Each mapping is assessed by the approving curator against a structured rubric covering relationship type, evidence basis, KE-specificity, and mechanism coverage. The rubric produces an integer score (0–7.5 with a biological-level bonus) which is then bucketed into one of three named levels; **All** is the unfiltered superset:

- **All** — the unfiltered set of approved mappings, irrespective of confidence. Use this when you want maximum coverage and will filter or weight downstream yourself.
- **High** — direct and specific biological link with strong experimental evidence. Recommended for downstream pathway-enrichment analyses where false positives are costly.
- **Medium** — partial or indirect biological relationship with moderate evidence. Useful as a broader hypothesis set.
- **Low** — weak, speculative, or unclear biological connection. Included for completeness; downstream users should treat with caution.

A `_<Level>.gmt` file appears only if there is at least one mapping at that level at the time of deposit. Missing levels indicate zero mappings in that bucket.

## Identifiers

- Key Event IDs follow AOP-Wiki canonical numbering (e.g. `KE 1234`)
- WikiPathways IDs follow `WP####`
- Gene Ontology IDs follow OBO Foundry CURIEs (`GO:#######`)
- Reactome IDs follow stable identifiers (`R-HSA-#######`)
- Every mapping carries a stable UUID, visible in the RDF/Turtle export and in the `/api/v1/...` REST endpoints on the live builder.

## Upstream resource snapshot

This deposit was assembled against the following upstream releases. The same versions are recorded per-mapping in the Turtle exports (predicates `vocab#wpReleaseDate`, `vocab#goReleaseDate`, `vocab#reactomeReleaseVersion`, `vocab#reactomeReleaseDate`, `vocab#aopWikiSnapshotDate`) and as a sidecar `source_versions.json` inside each per-resource ZIP.

{snapshot_table}

## Citation

If you use these mappings, please cite this Zenodo record (the concept DOI always resolves to the latest version) and acknowledge the upstream resources: AOP-Wiki, WikiPathways, the Gene Ontology Consortium / UniProt-GOA, and Reactome.
"""
    ).encode("utf-8")


def build_metadata(today: str, source_versions: Optional[dict] = None) -> dict:
    """Zenodo metadata block for the v3 deposit shape.

    Title, description, creators, license, version. Embeds a one-line
    upstream-snapshot summary into the description when the source-versions
    manifest is present.
    """
    snapshot_line = format_versions_for_prose(source_versions)
    description = (
        "Curated database of Key Event (KE) mappings to three molecular-pathway and ontology "
        "resources: WikiPathways (KE-WikiPathways), Gene Ontology Biological Process and "
        "Molecular Function (KE-GO), and Reactome (KE-Reactome). Mappings are bundled in three "
        "per-resource ZIP archives, each containing GMT gene-set files split by confidence "
        "level (All / High / Medium / Low) for clusterProfiler and fgsea, and RDF/Turtle for "
        "SPARQL and linked-data consumption. Each mapping carries a stable UUID and full "
        "curation provenance (proposer, approving curator, approval timestamp, BioBERT "
        "suggestion score, confidence level, connection type). "
    )
    if snapshot_line:
        description += f"Upstream snapshot for this deposit: {snapshot_line}. "
    description += (
        "Produced by the Molecular AOP Builder at https://molaop-builder.vhp4safety.nl ; "
        "source at https://github.com/marvinm2/molAOP-builder ."
    )
    return {
        "title": "Molecular AOP Builder — Curated KE → WikiPathways / GO / Reactome Mappings",
        "upload_type": "dataset",
        "description": description,
        "creators": [{
            "name": "Martens, Marvin",
            "affiliation": "Department of Translational Genomics, Maastricht University",
            "orcid": "0000-0003-2230-0840",
        }],
        "keywords": [
            "Adverse Outcome Pathway", "AOP", "Key Event", "WikiPathways", "Gene Ontology",
            "Reactome", "toxicology", "pathway analysis", "GMT", "RDF", "BioBERT", "curation",
        ],
        "license": "cc-zero",
        "publication_date": today,
        "version": today,
        "access_right": "open",
    }


def assemble_deposit_files(
    today: str,
    wp: list,
    go: list,
    rx: list,
    source_versions: Optional[dict] = None,
    gmt_kwargs_wp: Optional[dict] = None,
) -> dict:
    """End-to-end deposit assembly: returns {filename: bytes}.

    Imports the GMT and Turtle generators lazily so this module stays
    importable without the full app context (the rdf_exporter pulls in
    rdflib, which is fine, but keeping the import local avoids surprising
    side effects for callers that only want `counts()` etc.).

    `gmt_kwargs_wp` is the place to pass `cache_model=...` for the WP GMT
    generator, which uses the cache to populate gene lists.
    """
    from src.exporters.gmt_exporter import (
        generate_ke_wp_gmt, generate_ke_go_gmt, generate_ke_reactome_gmt,
    )
    from src.exporters.rdf_exporter import (
        generate_ke_wp_turtle, generate_ke_go_turtle, generate_ke_reactome_turtle,
    )

    wp_n = counts(wp)
    go_n = counts(go)
    rx_n = counts(rx)

    return {
        "KE-WikiPathways.zip": build_resource_zip(
            "KE-WikiPathways", generate_ke_wp_gmt, generate_ke_wp_turtle, wp, today,
            gmt_kwargs=gmt_kwargs_wp or {},
            source_versions_slice=slice_source_versions(source_versions or {}, "wikipathways", "aopwiki"),
        ),
        "KE-GO.zip": build_resource_zip(
            "KE-GO", generate_ke_go_gmt, generate_ke_go_turtle, go, today,
            source_versions_slice=slice_source_versions(source_versions or {}, "gene_ontology", "aopwiki"),
        ),
        "KE-Reactome.zip": build_resource_zip(
            "KE-Reactome", generate_ke_reactome_gmt, generate_ke_reactome_turtle, rx, today,
            source_versions_slice=slice_source_versions(source_versions or {}, "reactome", "aopwiki"),
        ),
        "README.md": build_readme(today, wp_n, go_n, rx_n, source_versions=source_versions),
    }
