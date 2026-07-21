"""
Capture upstream source versions for the curated mapping dataset.

Writes data/source_versions.json — a manifest of the upstream release each
of the four resources (WikiPathways, Gene Ontology, Reactome, AOP-Wiki)
was at when this script was last run. The running app reads this manifest
at boot and stamps every approved mapping with the current snapshot's
version, so downstream consumers can pin their analyses against a
specific upstream release.

Usage:
    python scripts/capture_source_versions.py              # all four
    python scripts/capture_source_versions.py --source wp  # one source
    python scripts/capture_source_versions.py --dry-run    # print, don't write

Exit codes:
    0  all sources captured (some may be 'unknown' if upstream was unreachable)
    1  manifest write failed
    2  invoked with --strict and at least one source returned 'unknown'

DMP §7 anchor: source-data versioning (Phase A — capture only).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = _PROJECT_ROOT / "data" / "source_versions.json"
DEFAULT_OBO_PATH = _PROJECT_ROOT / "data" / "go-basic.obo"

WIKIPATHWAYS_SPARQL = "https://sparql.wikipathways.org/sparql"
AOPWIKI_SPARQL = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"
REACTOME_INFO_API = "https://reactome.org/ContentService/data/database/info"

HTTP_TIMEOUT = 30  # seconds — upstream SPARQL endpoints are occasionally slow

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("capture_source_versions")


# ---------- helpers ----------

def _path_for_manifest(p: Path) -> str:
    """Render `p` as a repo-relative POSIX path when possible, absolute otherwise."""
    try:
        return p.resolve().relative_to(_PROJECT_ROOT).as_posix()
    except ValueError:
        return str(p)


def _utcnow_iso() -> str:
    """RFC 3339 / ISO 8601 UTC timestamp with Z suffix."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unknown(reason: str, **extra: Any) -> dict:
    """Build a uniform 'unknown' record so every source slot is structurally valid."""
    rec = {"status": "unknown", "reason": reason, "captured_at": _utcnow_iso()}
    rec.update(extra)
    return rec


def _sparql_max_modified(endpoint: str, type_iri: str, type_label: str) -> str | None:
    """
    Query an endpoint for MAX(dcterms:modified) over resources of `type_iri`.

    Returns the date portion (YYYY-MM-DD) of the most recent modification, or
    None if the endpoint cannot be reached or returns no value.
    """
    query = f"""
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT (MAX(?modified) AS ?latest) WHERE {{
  ?s a <{type_iri}> .
  ?s dcterms:modified ?modified .
}}
"""
    try:
        r = requests.post(
            endpoint,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("%s endpoint request failed: %s", type_label, e)
        return None

    bindings = r.json().get("results", {}).get("bindings", [])
    if not bindings:
        log.warning("%s MAX(?modified) returned no bindings", type_label)
        return None
    latest = bindings[0].get("latest", {}).get("value", "")
    if not latest:
        return None
    # latest is e.g. "2026-04-10T00:00:00Z" or "2026-04-10"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", latest)
    return m.group(1) if m else None


# ---------- per-source capture functions ----------

def capture_gene_ontology(obo_path: Path = DEFAULT_OBO_PATH) -> dict:
    """
    Read the data-version line from the local GO OBO file header.

    The OBO header looks like:
        format-version: 1.2
        data-version: releases/2026-01-23
    so we parse the date out of the data-version line and store both the
    raw label and the ISO date.
    """
    captured_at = _utcnow_iso()
    # The data mount may carry the full `go.obo` instead of `go-basic.obo`
    # (both share the same `data-version:` header). Fall back to the sibling
    # `go.obo` so the version fetch doesn't fail purely on which OBO variant
    # was provisioned on the mount.
    if not obo_path.exists():
        fallback = obo_path.with_name("go.obo")
        if fallback != obo_path and fallback.exists():
            obo_path = fallback
    if not obo_path.exists():
        return _unknown(
            "obo file not found",
            source_file=_path_for_manifest(obo_path),
            method="obo-header",
            captured_at=captured_at,
        )

    label = None
    try:
        # The header is the first dozen lines; cap the scan to avoid reading 30MB.
        with obo_path.open(encoding="utf-8") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                if line.startswith("data-version:"):
                    label = line.split(":", 1)[1].strip()
                    break
    except OSError as e:
        return _unknown(
            f"obo read error: {e}",
            source_file=_path_for_manifest(obo_path),
            method="obo-header",
            captured_at=captured_at,
        )

    if not label:
        return _unknown(
            "no data-version line in OBO header",
            source_file=_path_for_manifest(obo_path),
            method="obo-header",
            captured_at=captured_at,
        )

    # Pull a YYYY-MM-DD out of e.g. "releases/2026-01-23".
    m = re.search(r"(\d{4}-\d{2}-\d{2})", label)
    return {
        "status": "ok",
        "release_label": label,
        "release_date": m.group(1) if m else None,
        "method": "obo-header",
        "source_file": _path_for_manifest(obo_path),
        "captured_at": captured_at,
    }


def capture_wikipathways(endpoint: str = WIKIPATHWAYS_SPARQL) -> dict:
    """
    Resolve the WikiPathways release from the void:Dataset IRI.

    WP serves one release at a time and encodes the release date in the
    dataset IRI itself, e.g. <https://data.wikipathways.org/20260710/rdf/>.
    We query for the most recent matching dataset and extract the YYYYMMDD
    component, which is the canonical release date.

    The scheme must not be pinned. WikiPathways moved these dataset IRIs from
    ``http://`` to ``https://``, and the original ``STRSTARTS(..., "http://...")``
    filter then matched nothing — the query kept returning HTTP 200 with zero
    bindings, so the failure surfaced only as a permanently "unknown" version
    badge rather than an error. Matching either scheme keeps this working
    whichever way upstream serves them.
    """
    captured_at = _utcnow_iso()
    query = """
PREFIX void: <http://rdfs.org/ns/void#>
SELECT ?dataset WHERE {
  ?dataset a void:Dataset .
  FILTER(REGEX(STR(?dataset), "^https?://data\\\\.wikipathways\\\\.org/"))
} ORDER BY DESC(?dataset) LIMIT 1
"""
    try:
        r = requests.post(
            endpoint,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        return _unknown(
            f"wp sparql request failed: {e}",
            method="sparql:void-dataset-iri",
            endpoint=endpoint,
            captured_at=captured_at,
        )

    bindings = r.json().get("results", {}).get("bindings", [])
    if not bindings:
        return _unknown(
            "wp sparql returned no void:Dataset",
            method="sparql:void-dataset-iri",
            endpoint=endpoint,
            captured_at=captured_at,
        )
    iri = bindings[0].get("dataset", {}).get("value", "")
    m = re.search(r"data\.wikipathways\.org/(\d{4})(\d{2})(\d{2})/", iri)
    if not m:
        return _unknown(
            f"wp void:Dataset IRI did not match YYYYMMDD pattern: {iri}",
            method="sparql:void-dataset-iri",
            endpoint=endpoint,
            captured_at=captured_at,
        )
    return {
        "status": "ok",
        "release_date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
        "dataset_iri": iri,
        "method": "sparql:void-dataset-iri",
        "endpoint": endpoint,
        "captured_at": captured_at,
    }


def capture_reactome(api_url: str = REACTOME_INFO_API) -> dict:
    """
    Query the Reactome REST `/database/info` endpoint for version + release date.

    Returns the integer release version and the release date (when supplied).
    """
    captured_at = _utcnow_iso()
    try:
        r = requests.get(api_url, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        return _unknown(
            f"reactome api error: {e}",
            method="rest-api",
            endpoint=api_url,
            captured_at=captured_at,
        )

    try:
        info = r.json()
    except ValueError:
        return _unknown(
            "reactome api returned non-json",
            method="rest-api",
            endpoint=api_url,
            captured_at=captured_at,
        )

    # The Reactome /info payload exposes `version` (integer/string) and
    # `releaseDate` (ISO date). Field names occasionally change between
    # releases, so try a couple of common spellings.
    version = info.get("version") or info.get("releaseNumber")
    release_date = info.get("releaseDate") or info.get("release_date")
    if not version:
        return _unknown(
            "reactome api missing version field",
            method="rest-api",
            endpoint=api_url,
            captured_at=captured_at,
            raw_keys=list(info.keys()),
        )

    out = {
        "status": "ok",
        "release_version": str(version),
        "method": "rest-api",
        "endpoint": api_url,
        "captured_at": captured_at,
    }
    if release_date:
        # Normalise to YYYY-MM-DD if possible.
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(release_date))
        out["release_date"] = m.group(1) if m else str(release_date)
    return out


def capture_aopwiki(endpoint: str = AOPWIKI_SPARQL) -> dict:
    """
    Approximate the AOP-Wiki snapshot date as MAX(dcterms:modified) over AOPs.

    Falls back to MAX over KEs if the AOP query returns nothing, since some
    snapshots populate modified dates only on KE records.
    """
    captured_at = _utcnow_iso()
    date = _sparql_max_modified(
        endpoint,
        "http://aopkb.org/aop_ontology#AdverseOutcomePathway",
        "AOP-Wiki AOP",
    )
    if not date:
        date = _sparql_max_modified(
            endpoint,
            "http://aopkb.org/aop_ontology#KeyEvent",
            "AOP-Wiki KE (fallback)",
        )
    if not date:
        return _unknown(
            "sparql endpoint returned no modified date for AOPs or KEs",
            method="sparql:max(dcterms:modified)",
            endpoint=endpoint,
            captured_at=captured_at,
        )
    return {
        "status": "ok",
        "snapshot_date": date,
        "method": "sparql:max(dcterms:modified)",
        "endpoint": endpoint,
        "captured_at": captured_at,
    }


# ---------- orchestration ----------

CAPTURERS = {
    "wikipathways": capture_wikipathways,
    "gene_ontology": capture_gene_ontology,
    "reactome": capture_reactome,
    "aopwiki": capture_aopwiki,
}

# CLI aliases so callers can `--source wp` etc.
ALIASES = {
    "wp": "wikipathways",
    "go": "gene_ontology",
    "reactome": "reactome",
    "rx": "reactome",
    "aopwiki": "aopwiki",
    "aop": "aopwiki",
}


def build_manifest(sources: list[str] | None = None, *, obo_path: Path | None = None) -> dict:
    """
    Run the per-source capturers and assemble the manifest object.

    `obo_path` is threaded through to the GO capturer so callers (and the CLI)
    can override the default `data/go-basic.obo` location.
    """
    if sources is None:
        sources = list(CAPTURERS)
    payload: dict[str, dict] = {}
    for name in sources:
        log.info("Capturing %s ...", name)
        try:
            if name == "gene_ontology" and obo_path is not None:
                payload[name] = capture_gene_ontology(obo_path)
            else:
                payload[name] = CAPTURERS[name]()
        except Exception as e:  # noqa: BLE001 — capture surfaces failure as a record
            log.exception("Unhandled error in %s capturer", name)
            payload[name] = _unknown(f"unhandled: {e}", captured_at=_utcnow_iso())
        st = payload[name].get("status", "unknown")
        log.info("  -> %s (%s)", name, st)
    return {"captured_at": _utcnow_iso(), "sources": payload}


def _merge_with_existing(new_manifest: dict, existing_path: Path) -> dict:
    """
    Merge a partial capture (e.g. only --source go) into the existing manifest
    so that sources not touched this run aren't blown away.
    """
    if not existing_path.exists():
        return new_manifest
    try:
        old = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Existing manifest unreadable (%s); overwriting", e)
        return new_manifest
    merged = dict(old.get("sources", {}))
    merged.update(new_manifest["sources"])
    return {"captured_at": new_manifest["captured_at"], "sources": merged}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source",
        choices=sorted(set(list(CAPTURERS) + list(ALIASES))),
        action="append",
        default=None,
        help="Capture only the named source(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Output manifest path (default: {DEFAULT_OUTPUT_PATH.relative_to(_PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--obo-path",
        type=Path,
        default=DEFAULT_OBO_PATH,
        help=f"Override the GO OBO file path (default: {DEFAULT_OBO_PATH.relative_to(_PROJECT_ROOT)})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of writing")
    parser.add_argument("--strict", action="store_true", help="Exit 2 if any source returned 'unknown'")
    args = parser.parse_args(argv)

    sources = None
    if args.source:
        sources = sorted({ALIASES.get(s, s) for s in args.source})

    manifest = build_manifest(sources, obo_path=args.obo_path)
    if sources and len(sources) < len(CAPTURERS):
        manifest = _merge_with_existing(manifest, args.output)

    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        sys.stdout.write(text)
    else:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            log.error("Failed to write %s: %s", args.output, e)
            return 1
        log.info("Wrote %s", _path_for_manifest(args.output))

    unknowns = [k for k, v in manifest["sources"].items() if v.get("status") != "ok"]
    if unknowns:
        log.warning("Unknown sources: %s", ", ".join(unknowns))
        if args.strict:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
