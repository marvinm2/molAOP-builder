#!/usr/bin/env python3
"""Report mappings whose stored confidence tier disagrees with their assessment.

Until #237 the confidence tier was whatever the browser computed, and the
browser could lose the +1.0 biological-level bonus, storing a tier one level
low. This script recomputes the tier for every approved mapping that carries a
complete four-question assessment and prints the ones that do not match.

It **reports only**. Curated tiers are somebody's recorded judgement, and a
mismatch can also mean a curator deliberately overrode the computed value, so
rewriting them without review is not something a script should decide. Use the
output to drive a review, not a migration.

Usage:
    python scripts/audit_confidence_tiers.py [--db PATH] [--resource wp|reactome|all]
    python scripts/audit_confidence_tiers.py --csv mismatches.csv

On the deployed service:
    ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) \
        python /app/scripts/audit_confidence_tiers.py'
"""
import argparse
import csv
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.assessment_scoring import (  # noqa: E402
    compute_ke_pathway_confidence,
    score_ke_pathway_assessment,
)
from src.core.config_loader import ConfigLoader  # noqa: E402

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "ke_wp_mapping.db")
DEFAULT_KE_META = os.path.join(
    os.path.dirname(__file__), "..", "data", "ke_metadata.json"
)

RESOURCES = {
    "wp": ("mappings", "wp_id", "wp_title"),
    "reactome": ("ke_reactome_mappings", "reactome_id", "pathway_name"),
}


def load_ke_biolevels(path):
    """Return {KElabel: biolevel} from the KE metadata snapshot."""
    if not os.path.exists(path):
        print(f"WARNING: {path} not found — no mapping can be scored.", file=sys.stderr)
        return {}
    with open(path) as fh:
        return {ke["KElabel"]: ke.get("biolevel") or "" for ke in json.load(fh)}


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def audit_resource(conn, resource, biolevels, config):
    table, id_col, title_col = RESOURCES[resource]
    present = columns(conn, table)
    required = {"proposed_basis", "proposed_specificity", "proposed_coverage"}
    if not required <= present:
        print(f"  {table}: no assessment columns — skipping")
        return [], 0, 0

    rows = conn.execute(
        f"""SELECT id, ke_id, ke_title,
                   {id_col} AS pathway_id, {title_col} AS pathway_title,
                   confidence_level, proposed_basis,
                   proposed_specificity, proposed_coverage
            FROM {table}"""
    ).fetchall()

    mismatches = []
    scorable = 0
    unknown_ke = 0

    for row in rows:
        if not (row["proposed_basis"] and row["proposed_specificity"]
                and row["proposed_coverage"]):
            continue
        if row["ke_id"] not in biolevels:
            unknown_ke += 1
            continue

        scorable += 1
        biolevel = biolevels[row["ke_id"]]
        score = score_ke_pathway_assessment(
            row["proposed_basis"], row["proposed_specificity"],
            row["proposed_coverage"], biolevel, config,
        )
        computed = compute_ke_pathway_confidence(
            row["proposed_basis"], row["proposed_specificity"],
            row["proposed_coverage"], biolevel, config,
        )
        if computed != row["confidence_level"]:
            mismatches.append({
                "resource": resource,
                "id": row["id"],
                "ke_id": row["ke_id"],
                "ke_title": row["ke_title"],
                "pathway_id": row["pathway_id"],
                "pathway_title": row["pathway_title"],
                "biolevel": biolevel,
                "basis": row["proposed_basis"],
                "specificity": row["proposed_specificity"],
                "coverage": row["proposed_coverage"],
                "score": score,
                "stored": row["confidence_level"],
                "computed": computed,
            })

    return mismatches, scorable, unknown_ke


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--ke-metadata", default=DEFAULT_KE_META)
    parser.add_argument("--resource", choices=["wp", "reactome", "all"], default="all")
    parser.add_argument("--csv", help="also write the mismatches to this CSV path")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: no database at {args.db}", file=sys.stderr)
        return 1

    config = ConfigLoader.load_config().ke_pathway_assessment
    biolevels = load_ke_biolevels(args.ke_metadata)
    print(f"KE metadata: {len(biolevels)} Key Events")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    resources = list(RESOURCES) if args.resource == "all" else [args.resource]
    all_mismatches = []
    for resource in resources:
        table = RESOURCES[resource][0]
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            print(f"  {table}: absent — skipping")
            continue
        mismatches, scorable, unknown_ke = audit_resource(
            conn, resource, biolevels, config
        )
        print(f"  {table}: {scorable} scorable, {len(mismatches)} mismatched"
              + (f", {unknown_ke} skipped (Key Event absent from the snapshot)"
                 if unknown_ke else ""))
        all_mismatches.extend(mismatches)

    if not all_mismatches:
        print("\nNo mismatches. Every scorable mapping's tier follows "
              "from its assessment.")
        return 0

    print(f"\n{len(all_mismatches)} mapping(s) whose stored tier does not follow "
          f"from the recorded assessment:\n")
    for m in all_mismatches:
        too_low = _rank(m["computed"]) > _rank(m["stored"])
        direction = "stored LOW" if too_low else "stored HIGH"
        print(f"  [{m['resource']}] {m['ke_id']} -> {m['pathway_id']}  "
              f"{m['stored']} != {m['computed']}  ({direction}, score {m['score']})")
        print(f"      {m['ke_title'][:60]} -> {m['pathway_title'][:50]}")
        print(f"      {m['basis']}/{m['specificity']}/{m['coverage']} "
              f"at {m['biolevel']!r}")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(all_mismatches[0]))
            writer.writeheader()
            writer.writerows(all_mismatches)
        print(f"\nWritten to {args.csv}")

    print("\nThis script does not modify the database. Review each row before "
          "deciding whether the stored tier or the assessment is the error.")
    return 0


def _rank(tier):
    return {"low": 0, "medium": 1, "high": 2}.get(tier, -1)


if __name__ == "__main__":
    sys.exit(main())
