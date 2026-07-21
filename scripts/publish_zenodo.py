"""
Publish (or version-bump) the curated KE → WikiPathways / GO / Reactome
mapping database to Zenodo.

Assembles three per-resource ZIP archives (KE-WikiPathways.zip, KE-GO.zip,
KE-Reactome.zip), each containing GMT files split by confidence level
(All / High / Medium / Low) plus a Turtle file with full curation
provenance, alongside a README that quantifies per-tier mapping counts
and explains the confidence rubric.

If the concept DOI in data/zenodo_meta.json already exists, a new
version is minted under it (inherited files from the previous version
are deleted first, so the new release contains only the intended
shape). Otherwise the first version is created and the concept DOI
captured.

Usage
-----
    docker exec <container> python /app/scripts/publish_zenodo.py [opts]

Options
-------
    --dry-run        Build the deposit in memory and print what would
                     happen; do NOT touch Zenodo or modify meta file.
    --sandbox        Use https://sandbox.zenodo.org instead of production.
                     Requires ZENODO_SANDBOX_API_TOKEN env var.
    --force          Publish even when per-resource mapping counts are
                     unchanged since the last recorded deposit.
    --min-delta N    Skip the publish if the total approved-mapping count
                     across all three resources differs by less than N
                     rows from the last recorded deposit. Default: 1.

Exit codes
----------
    0   Publish completed, or skipped because nothing has changed.
    1   Configuration error (missing token, app context, etc.).
    2   Zenodo API error during publish.
    3   Another invocation is holding the lock.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

import requests

DEFAULT_META_PATH = Path("data/zenodo_meta.json")
LOCK_PATH = Path("/tmp/molaop-zenodo-publish.lock")
PROD_BASE = "https://zenodo.org/api"
SANDBOX_BASE = "https://sandbox.zenodo.org/api"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("publish_zenodo")


# The pure-Python deposit assembly (counts, ZIPs, README, metadata) lives
# in src/exporters/zenodo_assembly.py so both this script and the admin
# `publish_zenodo` route share one implementation. The thin underscore-
# prefixed shims below preserve the script's original symbol names for
# back-compat with anything that might import from here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.exporters.zenodo_assembly import (   # noqa: E402
    counts as _counts,
    changes_significant as _changes_significant_impl,
    build_resource_zip as _build_resource_zip,
    slice_source_versions as _slice_source_versions,
    format_versions_for_prose as _format_versions_for_prose,
    format_snapshot_table_md as _format_snapshot_table_md,
    build_readme as _build_readme,
    build_metadata as _build_metadata,
)

# Explicit re-export list — tests/test_source_version_ui_and_zenodo.py imports
# `_format_versions_for_prose` and `_format_snapshot_table_md` by their
# underscore-aliased names from this module. Listing them in __all__ tells
# static analyzers (ruff, CodeQL) the imports are intentional and used.
__all__ = [
    "_counts", "_changes_significant_impl",
    "_build_resource_zip", "_slice_source_versions",
    "_format_versions_for_prose", "_format_snapshot_table_md",
    "_build_readme", "_build_metadata",
]


def _changes_significant(current: dict, last: Optional[dict], min_delta: int) -> bool:
    """Logging shim around zenodo_assembly.changes_significant."""
    if last:
        delta = (
            abs(current["wp"]["All"] - last.get("wp", {}).get("All", 0))
            + abs(current["go"]["All"] - last.get("go", {}).get("All", 0))
            + abs(current["reactome"]["All"] - last.get("reactome", {}).get("All", 0))
        )
        log.info("Mapping-count delta since last deposit: %d (threshold: %d)", delta, min_delta)
    return _changes_significant_impl(current, last, min_delta)


# ---------- Zenodo I/O ----------

def _zenodo_new_or_newversion(base: str, h_auth: dict, h_json: dict, existing_id: Optional[int]):
    """Return (deposition_id, bucket_url) — either fresh or a new-version draft."""
    if existing_id:
        log.info("Creating new Zenodo version from deposition %s", existing_id)
        r = requests.post(f"{base}/deposit/depositions/{existing_id}/actions/newversion",
                          headers=h_auth, timeout=30)
        r.raise_for_status()
        draft_url = r.json()["links"]["latest_draft"]
        dep_id = int(draft_url.rstrip("/").split("/")[-1])
        r2 = requests.get(draft_url, headers=h_auth, timeout=30)
        r2.raise_for_status()
        draft = r2.json()
        # Delete every inherited file so the new version only contains what we upload.
        inherited = draft.get("files", [])
        log.info("Inherited %d file(s) from previous version — deleting", len(inherited))
        for f in inherited:
            fid, fname = f["id"], f["filename"]
            dr = requests.delete(f"{base}/deposit/depositions/{dep_id}/files/{fid}",
                                 headers=h_auth, timeout=30)
            if dr.status_code not in (204, 200):
                raise RuntimeError(f"Failed to delete inherited file {fname}: {dr.status_code} {dr.text[:200]}")
        return dep_id, draft["links"]["bucket"]
    else:
        log.info("Creating first-ever Zenodo deposit (no existing concept)")
        r = requests.post(f"{base}/deposit/depositions", json={}, headers=h_json, timeout=30)
        r.raise_for_status()
        body = r.json()
        return body["id"], body["links"]["bucket"]


def _upload_files(bucket_url: str, files: dict, h_auth: dict) -> None:
    for fname, data in files.items():
        if isinstance(data, str):
            data = data.encode("utf-8")
        log.info("Uploading %s (%d bytes)", fname, len(data))
        r = requests.put(f"{bucket_url}/{fname}", data=data, headers=h_auth, timeout=600)
        r.raise_for_status()


# ---------- meta file persistence ----------
#
# Shared implementation lives in src/exporters/zenodo_uploader.py so the
# admin route (src/blueprints/admin.py:publish_zenodo) and this script
# fall back identically when the container can't write the gluster mount.
# Imported lazily — running this script straight off the filesystem
# (outside the Flask app context) needs sys.path to point at /app.
def _write_meta(meta_path: Path, payload: dict) -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.exporters.zenodo_uploader import persist_meta_with_fallback
    return persist_meta_with_fallback(meta_path, payload)


# ---------- main ----------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Assemble but do not publish")
    p.add_argument("--sandbox", action="store_true", help="Use sandbox.zenodo.org")
    p.add_argument("--force", action="store_true", help="Publish even if no counts changed")
    p.add_argument("--min-delta", type=int, default=1, help="Min total row-count change to trigger publish (default 1)")
    p.add_argument("--meta-path", type=Path, default=DEFAULT_META_PATH, help="Path to zenodo_meta.json")
    args = p.parse_args()

    # Acquire lock first to avoid concurrent runs. lock_fp is held for the
    # lifetime of this main() call and released in the finally block below.
    lock_fp = None
    try:
        lock_fp = open(LOCK_PATH, "w")  # noqa: SIM115 — process-lifetime lock, closed in finally
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        if lock_fp is not None:
            lock_fp.close()
        log.error("Could not acquire lock %s — another publish in progress? %s", LOCK_PATH, e)
        return 3

    try:
        # Token + base URL
        from src.exporters.zenodo_uploader import resolve_zenodo_token

        if args.sandbox:
            base = SANDBOX_BASE
            tok_var = "ZENODO_SANDBOX_API_TOKEN"
        else:
            base = PROD_BASE
            tok_var = "ZENODO_API_TOKEN"
        # Docker secret first, env var as fallback — see resolve_zenodo_token (#191).
        token = resolve_zenodo_token(tok_var)
        if not args.dry_run and not token:
            log.error(
                "No Zenodo token for %s — set the env var, mount a Docker secret at "
                "/run/secrets/%s, or point %s_FILE at a token file",
                tok_var, tok_var.lower(), tok_var,
            )
            return 1
        h_auth = {"Authorization": f"Bearer {token}"} if token else {}
        h_json = {**h_auth, "Content-Type": "application/json"} if token else {}

        # Boot the Flask app to get access to the model layer
        sys.path.insert(0, "/app")  # in case launched outside /app
        try:
            from app import create_app
        except Exception as e:
            log.error("Could not import create_app: %s", e)
            return 1
        app = create_app()
        with app.app_context():
            from src.blueprints import admin as a
            from src.exporters.gmt_exporter import (
                generate_ke_wp_gmt, generate_ke_go_gmt, generate_ke_reactome_gmt,
            )
            from src.exporters.rdf_exporter import (
                generate_ke_wp_turtle, generate_ke_go_turtle, generate_ke_reactome_turtle,
            )

            wp = a.mapping_model.get_all_mappings() if a.mapping_model else []
            go = a.go_mapping_model.get_all_mappings() if a.go_mapping_model else []
            rx = a.reactome_mapping_model.get_all_mappings() if a.reactome_mapping_model else []
            wp_n, go_n, rx_n = _counts(wp), _counts(go), _counts(rx)
            today = datetime.date.today().isoformat()
            current_counts = {"wp": wp_n, "go": go_n, "reactome": rx_n}

            log.info(
                "Current counts — WP: %d (H:%d M:%d L:%d) | GO: %d (H:%d M:%d L:%d) | "
                "Reactome: %d (H:%d M:%d L:%d)",
                wp_n["All"], wp_n["High"], wp_n["Medium"], wp_n["Low"],
                go_n["All"], go_n["High"], go_n["Medium"], go_n["Low"],
                rx_n["All"], rx_n["High"], rx_n["Medium"], rx_n["Low"],
            )

            # Load previous meta + skip-if-unchanged gate
            existing_meta = {}
            if args.meta_path.exists():
                try:
                    existing_meta = json.loads(args.meta_path.read_text())
                except Exception as e:
                    log.warning("Could not parse %s — treating as empty: %s", args.meta_path, e)
            existing_id = existing_meta.get("deposition_id")
            last_counts = existing_meta.get("counts")

            if not args.force and not _changes_significant(current_counts, last_counts, args.min_delta):
                log.info("[SKIP] Mapping counts unchanged since last deposit — nothing to do")
                return 0

            # Phase E.2: load the upstream source-versions manifest so each
            # per-resource ZIP gets a sidecar pinning it to a specific
            # snapshot, and the README + Zenodo description reference the
            # same versions. Missing or unreadable manifest is degraded
            # gracefully — deposit still publishes, just without the
            # snapshot block.
            source_versions = {}
            sv_path = Path("data/source_versions.json")
            try:
                if sv_path.exists():
                    source_versions = json.loads(sv_path.read_text(encoding="utf-8"))
                    log.info("Loaded source_versions manifest from %s", sv_path)
                else:
                    log.warning("source_versions.json not found at %s — deposit will omit snapshot block", sv_path)
            except Exception as e:
                log.warning("Could not parse source_versions.json: %s — deposit will omit snapshot block", e)

            # Build deposit contents
            files = {
                "KE-WikiPathways.zip": _build_resource_zip(
                    "KE-WikiPathways", generate_ke_wp_gmt, generate_ke_wp_turtle, wp, today,
                    gmt_kwargs={"cache_model": a.cache_model_ref},
                    source_versions_slice=_slice_source_versions(source_versions, "wikipathways", "aopwiki"),
                ),
                "KE-GO.zip":       _build_resource_zip(
                    "KE-GO", generate_ke_go_gmt, generate_ke_go_turtle, go, today,
                    source_versions_slice=_slice_source_versions(source_versions, "gene_ontology", "aopwiki"),
                ),
                "KE-Reactome.zip": _build_resource_zip(
                    "KE-Reactome", generate_ke_reactome_gmt, generate_ke_reactome_turtle, rx, today,
                    source_versions_slice=_slice_source_versions(source_versions, "reactome", "aopwiki"),
                ),
                "README.md":       _build_readme(today, wp_n, go_n, rx_n, source_versions=source_versions),
            }
            for name, blob in files.items():
                log.info("Assembled %s (%d bytes)", name, len(blob))

            metadata = _build_metadata(today, source_versions=source_versions)

            if args.dry_run:
                log.info("[DRY-RUN] Would publish a new version under existing_id=%s with %d file(s).",
                         existing_id, len(files))
                log.info("[DRY-RUN] Metadata title:   %s", metadata["title"])
                log.info("[DRY-RUN] Metadata version: %s", metadata["version"])
                log.info("[DRY-RUN] Endpoint:         %s", base)
                return 0

            # Real publish
            try:
                dep_id, bucket_url = _zenodo_new_or_newversion(base, h_auth, h_json, existing_id)
                log.info("Draft id=%s  bucket=%s", dep_id, bucket_url)
                _upload_files(bucket_url, files, h_auth)
                r = requests.put(
                    f"{base}/deposit/depositions/{dep_id}",
                    data=json.dumps({"metadata": metadata}),
                    headers=h_json, timeout=30,
                )
                r.raise_for_status()
                r = requests.post(
                    f"{base}/deposit/depositions/{dep_id}/actions/publish",
                    headers=h_auth, timeout=60,
                )
                r.raise_for_status()
                result = r.json()
            except requests.HTTPError as e:
                log.error("Zenodo API error: %s — body: %s", e, getattr(e.response, "text", "")[:500])
                return 2

            # Persist updated meta
            new_meta = {
                "deposition_id": result["id"],
                "doi": result["doi"],
                "concept_doi": result.get("conceptdoi", existing_meta.get("concept_doi")),
                "published_at": today,
                "version": metadata["version"],
                "counts": current_counts,
            }
            written_to = _write_meta(args.meta_path, new_meta)

            log.info("[DONE] DOI=%s  concept=%s  version=%s  meta=%s",
                     new_meta["doi"], new_meta["concept_doi"], new_meta["version"], written_to)
            return 0
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        return 1
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                log.debug("flock release failed (process exit will clear it): %s", e)
            try:
                lock_fp.close()
            except Exception as e:
                log.debug("lock_fp close failed: %s", e)


if __name__ == "__main__":
    sys.exit(main())
