#!/usr/bin/env python3
"""Detect upstream releases the deployed corpus has not caught up with.

The app has always known its upstream release identifiers — `SourceVersionService`
fetches them live for WikiPathways, GO, Reactome and AOP-Wiki, and
`data/source_versions.json` records what was captured. Nothing ever acted on a
difference between the two, so the corpus quietly fell months behind: measured
on 2026-08-03, WikiPathways was two months stale, Reactome one release behind
and AOP-Wiki nearly three months, which is the same drift that made 34 Key
Events uncurable in the GUI (#239).

This closes the loop. It compares live against stored and, with ``--rebuild``,
runs the corpus rebuild for each source that moved.

    python scripts/check_source_releases.py                 # report only
    python scripts/check_source_releases.py --rebuild       # rebuild what drifted
    python scripts/check_source_releases.py --only wikipathways --rebuild

Exit codes are chosen so a cron can alert on them:

    0  everything current (or a rebuild completed)
    1  an error prevented the check
    2  drift detected, and --rebuild was not passed
    3  a rebuild was attempted and failed

Preflight matters more than it looks. The artifacts live on a GlusterFS bind
mount whose files carry gid 1003 while the container runs as gid 1000, so an
in-place rewrite fails with EACCES *after* an expensive rebuild has already
run. This checks writability before spending forty minutes on BioBERT.
"""
import argparse
import json
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.services.source_versions import (  # noqa: E402
    _extract_aopwiki,
    _extract_go,
    _extract_reactome,
    _extract_wp,
    snapshot,
)

# The stored manifest and the live snapshot record the same release in
# different shapes — Reactome stores `release_version: "96"` while the live
# path normalises it to "v96", and AOP-Wiki stores `snapshot_date` where the
# others store `release_date`. Comparing the raw fields would report permanent
# drift and rebuild on every run. Reusing the live extractors is what keeps the
# two sides on one axis; a local reimplementation would drift from them.
STORED_EXTRACTORS = {
    "wikipathways": _extract_wp,
    "gene_ontology": _extract_go,
    "reactome": _extract_reactome,
    "aopwiki": _extract_aopwiki,
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
logger = logging.getLogger("source-releases")

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STORED_PATH = os.path.join(PROJECT_ROOT, "data", "source_versions.json")
# Written when a rebuild actually replaced something, and consumed by whatever
# restarts the service. The app reads its artifacts at startup, so a rebuilt
# corpus is not live until it restarts — but restarting on a fixed schedule
# regardless would drop curator sessions on every week that nothing changed.
REBUILT_MARKER = os.path.join(PROJECT_ROOT, "data", ".corpus-rebuilt")

# What to rebuild when a source moves, and which artifacts that rebuild
# rewrites. The artifact list is what the writability preflight checks.
REBUILD = {
    "wikipathways": {
        "commands": [
            ["python", "scripts/download_wikipathways_annotations.py"],
            ["python", "scripts/precompute_pathway_title_embeddings.py"],
        ],
        "artifacts": [
            "data/wikipathways_gene_annotations.json",
            "data/wikipathways_filtered_ids.json",
            "data/wikipathways_gene_counts.json",
            "data/pathway_metadata.json",
            "data/pathway_title_embeddings.npz",
        ],
    },
    "gene_ontology": {
        "commands": [
            ["python", "scripts/precompute_go_hierarchy.py"],
            ["python", "scripts/download_go_annotations.py"],
            ["python", "scripts/precompute_go_embeddings.py"],
        ],
        "artifacts": [
            "data/go_bp_embeddings.npz",
            "data/go_bp_name_embeddings.npz",
            "data/go_bp_metadata.json",
        ],
    },
    "reactome": {
        "commands": [
            ["python", "scripts/download_reactome_annotations.py"],
            ["python", "scripts/precompute_reactome_embeddings.py"],
        ],
        "artifacts": [
            "data/reactome_pathway_embeddings.npz",
            "data/reactome_pathway_name_embeddings.npz",
        ],
    },
    "aopwiki": {
        # Deliberately the cheap refresh only. The KE dropdown going stale is
        # what blocks curation outright (#239); the embeddings degrade
        # gracefully, because a Key Event without one is encoded live on a
        # miss. Rebuilding those too would turn a 30-second job into a long
        # one for no curator-visible gain — run `make ke-corpus` separately.
        "commands": [
            ["python", "scripts/precompute_ke_embeddings.py", "--metadata-only"],
        ],
        "artifacts": ["data/ke_metadata.json"],
    },
}


def stored_versions():
    if not os.path.exists(STORED_PATH):
        logger.warning("No %s — treating every source as undetermined", STORED_PATH)
        return {}
    with open(STORED_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh).get("sources", {})


def stored_identifier(source, entry):
    """The stored release identifier, normalised the way the live path is."""
    if not entry or entry.get("status") != "ok":
        return None
    extractor = STORED_EXTRACTORS.get(source)
    if not extractor:
        return None
    value = extractor(entry)
    return str(value) if value else None


def compare():
    """Return {source: (stored, live, drifted)}."""
    live = snapshot()
    stored = stored_versions()
    result = {}
    for source, live_entry in live.items():
        live_id = (
            None if live_entry.get("unavailable")
            else str(live_entry.get("version"))
        )
        stored_id = stored_identifier(source, stored.get(source))
        # Unknown on either side is not drift. Rebuilding on "we could not
        # tell" would rebuild every time the endpoint had a bad day.
        drifted = bool(live_id and stored_id and live_id != stored_id)
        result[source] = (stored_id, live_id, drifted)
    return result


def preflight(artifacts):
    """Return the artifacts that exist but cannot be rewritten."""
    blocked = []
    for rel in artifacts:
        path = os.path.join(PROJECT_ROOT, rel)
        if os.path.exists(path) and not os.access(path, os.W_OK):
            blocked.append(rel)
        elif not os.path.exists(path):
            parent = os.path.dirname(path)
            if os.path.exists(parent) and not os.access(parent, os.W_OK):
                blocked.append(rel)
    return blocked


def rebuild(source):
    """Run the rebuild for one source. Returns True on success."""
    spec = REBUILD.get(source)
    if not spec:
        logger.warning("No rebuild defined for %s — skipping", source)
        return True

    blocked = preflight(spec["artifacts"])
    if blocked:
        logger.error(
            "Refusing to rebuild %s: cannot write %s. On the cluster this is "
            "the gid-1003 trap — the files predate the container's gid 1000. "
            "Remove and recreate them from inside the container so they "
            "inherit gid 1000 from the setgid data dir.",
            source, ", ".join(blocked),
        )
        return False

    for command in spec["commands"]:
        logger.info("[%s] running: %s", source, " ".join(command))
        proc = subprocess.run(command, cwd=PROJECT_ROOT)
        if proc.returncode != 0:
            logger.error(
                "[%s] %s exited %d — stopping this source's rebuild",
                source, command[-1], proc.returncode,
            )
            return False
    logger.info("[%s] rebuild complete", source)
    return True


def write_rebuilt_marker(sources):
    """Record that these sources were rebuilt and the service needs restarting.

    Deliberately additive: if a marker is already present from an earlier run
    that has not been applied yet, the new sources are merged in rather than
    replacing it. Two rebuilds between two restarts must not lose the first
    one's entry, or the restart would be skipped for a corpus that did change.
    """
    existing = []
    if os.path.exists(REBUILT_MARKER):
        try:
            with open(REBUILT_MARKER, "r", encoding="utf-8") as fh:
                existing = json.load(fh).get("sources", [])
        except Exception:
            pass
    merged = sorted(set(existing) | set(sources))
    try:
        with open(REBUILT_MARKER, "w", encoding="utf-8") as fh:
            json.dump({"sources": merged}, fh)
        logger.info(
            "Wrote %s — the service needs a restart to serve the new corpus",
            REBUILT_MARKER,
        )
    except Exception as e:
        logger.warning("Could not write the rebuilt marker: %s", e)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="rebuild the corpus for each source that has moved",
    )
    parser.add_argument(
        "--only", action="append", choices=sorted(REBUILD),
        help="restrict to these sources (repeatable)",
    )
    args = parser.parse_args()

    try:
        comparison = compare()
    except Exception as e:
        logger.error("Could not compare source versions: %s", e)
        return 1

    if args.only:
        comparison = {k: v for k, v in comparison.items() if k in args.only}

    logger.info("%-16s %-24s %-24s %s", "SOURCE", "DEPLOYED", "UPSTREAM", "")
    drifted = []
    for source, (stored_id, live_id, is_drifted) in sorted(comparison.items()):
        if is_drifted:
            note = "DRIFTED"
            drifted.append(source)
        elif live_id is None:
            note = "upstream unavailable"
        elif stored_id is None:
            note = "nothing recorded locally"
        else:
            note = "current"
        logger.info(
            "%-16s %-24s %-24s %s",
            source, stored_id or "-", live_id or "-", note,
        )

    if not drifted:
        logger.info("Nothing to rebuild.")
        return 0

    if not args.rebuild:
        logger.warning(
            "%d source(s) behind upstream: %s. Re-run with --rebuild to act.",
            len(drifted), ", ".join(drifted),
        )
        return 2

    failed = [s for s in drifted if not rebuild(s)]

    # Refresh the stored manifest so the next run compares against what is
    # actually deployed. Only when every rebuild succeeded — recording a
    # version whose corpus was not rebuilt would suppress the next alert and
    # make the drift permanently invisible, which is the failure this whole
    # script exists to prevent.
    if failed:
        logger.error("Rebuild failed for: %s. Leaving the stored manifest "
                     "untouched so the drift is reported again.", ", ".join(failed))
        return 3

    write_rebuilt_marker(drifted)

    # Advance the stored manifest ONLY for sources genuinely rebuilt. A bare
    # capture_source_versions.py rewrites all four entries regardless of what
    # this run touched, so a partial run (`--only aopwiki`, or one source
    # failing while others succeed) would record versions whose corpora were
    # never rebuilt — suppressing the next alert and making that drift
    # permanently invisible. Passing --source per rebuilt source makes the
    # capture script merge into the existing manifest instead of replacing it.
    logger.info("Refreshing %s for: %s", STORED_PATH, ", ".join(drifted))
    capture_cmd = ["python", "scripts/capture_source_versions.py"]
    for source in drifted:
        capture_cmd += ["--source", source]
    proc = subprocess.run(capture_cmd, cwd=PROJECT_ROOT)
    if proc.returncode != 0:
        logger.warning(
            "Rebuilds succeeded but capture_source_versions.py exited %d — the "
            "next run will report drift again for already-rebuilt sources.",
            proc.returncode,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
