"""
Backfill upstream source-version columns on existing mapping rows.

Phase D of source-data versioning (DMP §7). Phase C stamps the deployed
snapshot's versions onto every new approval, but rows approved before
Phase C shipped have NULL version columns. This script fills those NULLs
with the *current* snapshot's values, using `data/source_versions.json`
as the source of truth.

This is the simplest possible backfill: every legacy row gets the same
versions, regardless of when it was approved. Downstream consumers can
treat these stamps as "the snapshot the dataset reached parity with at
backfill time" rather than per-row historical accuracy. A future
hand-curated release-calendar backfill could replace this with
per-row nearest-release lookups if needed.

The script is idempotent — only NULL columns are filled, so re-running
is a no-op. Use --dry-run to preview the SQL and row counts without
applying any changes.

Usage:
    python scripts/backfill_source_versions.py                 # default db
    python scripts/backfill_source_versions.py --db /tmp/k.db  # explicit
    python scripts/backfill_source_versions.py --dry-run       # preview
    python scripts/backfill_source_versions.py --manifest path # alt manifest

Exit codes:
    0  backfill completed (or dry-run completed)
    1  config / file error (manifest missing, db missing, malformed)
    2  manifest has insufficient data for any mapping table
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = _PROJECT_ROOT / "data" / "source_versions.json"
DEFAULT_DB = _PROJECT_ROOT / "ke_wp_mapping.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("backfill_source_versions")


# ---------- manifest loading ----------

def _load_manifest(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"manifest not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"manifest is not valid JSON: {e}") from e


def _ok(source: dict) -> bool:
    return isinstance(source, dict) and source.get("status") == "ok"


def _values_for_table(manifest: dict, table: str) -> Optional[dict]:
    """
    Resolve the column -> value dict to backfill for a given mapping table.

    Returns None if the manifest doesn't have enough info for this table
    (e.g. the WikiPathways source is 'unknown' and the AOP-Wiki anchor is
    missing — there's nothing useful to write).
    """
    sources = manifest.get("sources", {}) if manifest else {}
    aopwiki = sources.get("aopwiki", {})
    aopwiki_date = aopwiki.get("snapshot_date") if _ok(aopwiki) else None

    if table == "mappings":
        wp = sources.get("wikipathways", {})
        wp_date = wp.get("release_date") if _ok(wp) else None
        if not wp_date and not aopwiki_date:
            return None
        return {"wp_release_date": wp_date, "aopwiki_snapshot_date": aopwiki_date}

    if table == "ke_go_mappings":
        go = sources.get("gene_ontology", {})
        go_date = go.get("release_date") if _ok(go) else None
        if not go_date and not aopwiki_date:
            return None
        return {"go_release_date": go_date, "aopwiki_snapshot_date": aopwiki_date}

    if table == "ke_reactome_mappings":
        rx = sources.get("reactome", {})
        rx_version = rx.get("release_version") if _ok(rx) else None
        rx_date = rx.get("release_date") if _ok(rx) else None
        if not rx_version and not rx_date and not aopwiki_date:
            return None
        return {
            "reactome_release_version": rx_version,
            "reactome_release_date": rx_date,
            "aopwiki_snapshot_date": aopwiki_date,
        }

    raise ValueError(f"Unknown table {table!r}")


# ---------- backfill ----------

def _count_null_rows(conn: sqlite3.Connection, table: str, columns: list[str]) -> int:
    """Count rows where ANY of the named columns is currently NULL."""
    where = " OR ".join(f"{col} IS NULL" for col in columns)
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def _backfill_table(conn: sqlite3.Connection, table: str, fields: dict, dry_run: bool) -> tuple[int, int]:
    """
    Backfill NULLs on `table` with the given column -> value dict.

    Uses COALESCE so each column is updated only if it's currently NULL —
    rows with some columns already populated keep those values. The WHERE
    clause restricts the update to rows where at least one of the relevant
    columns is NULL, so re-running on a fully-stamped table is a no-op.

    Returns (rows_targeted, rows_affected). In a dry run rows_affected is 0.
    """
    # Drop any fields where the manifest had no value — we can only fill
    # columns we actually have data for.
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        log.info("%s: no fields to fill (manifest has no values for this table)", table)
        return 0, 0

    cols = list(fields.keys())
    targeted = _count_null_rows(conn, table, cols)
    if targeted == 0:
        log.info("%s: 0 rows need backfill (already fully stamped)", table)
        return 0, 0

    set_clauses = [f"{col} = COALESCE({col}, ?)" for col in cols]
    where_clauses = [f"{col} IS NULL" for col in cols]
    set_values = [fields[col] for col in cols]

    sql = (
        f"UPDATE {table} SET {', '.join(set_clauses)} "
        f"WHERE {' OR '.join(where_clauses)}"
    )

    log.info(
        "%s: %d rows targeted (any-of %s NULL) -> %s",
        table, targeted, cols, ", ".join(f"{c}={fields[c]!r}" for c in cols),
    )
    log.debug("SQL: %s ; params=%s", sql, set_values)

    if dry_run:
        log.info("  [dry-run] no changes written")
        return targeted, 0

    cursor = conn.execute(sql, set_values)
    affected = cursor.rowcount
    log.info("  -> %d rows updated", affected)
    return targeted, affected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help=f"Path to source-versions manifest (default: {DEFAULT_MANIFEST.relative_to(_PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"Path to the SQLite database (default: {DEFAULT_DB.relative_to(_PROJECT_ROOT) if str(DEFAULT_DB).startswith(str(_PROJECT_ROOT)) else DEFAULT_DB})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args(argv)

    try:
        manifest = _load_manifest(args.manifest)
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        return 1

    if not args.db.exists():
        log.error("database not found at %s", args.db)
        return 1

    log.info("Backfilling %s from %s%s",
             args.db, args.manifest, " (dry-run)" if args.dry_run else "")

    conn = sqlite3.connect(str(args.db), timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE;")  # not WAL on GlusterFS (#292)
    conn.execute("PRAGMA busy_timeout=5000;")

    total_targeted = 0
    total_affected = 0
    no_data_tables = []
    try:
        for table in ("mappings", "ke_go_mappings", "ke_reactome_mappings"):
            fields = _values_for_table(manifest, table)
            if fields is None:
                log.warning("%s: manifest has no data to backfill this table — skipping", table)
                no_data_tables.append(table)
                continue
            t, a = _backfill_table(conn, table, fields, args.dry_run)
            total_targeted += t
            total_affected += a
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    log.info(
        "Done: %d rows %s%s",
        total_targeted if args.dry_run else total_affected,
        "would be updated" if args.dry_run else "updated",
        f" (across {3 - len(no_data_tables)} tables)" if no_data_tables else "",
    )

    if len(no_data_tables) == 3:
        log.error("Manifest has no usable data for any mapping table")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
