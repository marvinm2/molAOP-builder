"""
Pre-compute OECD development status for all AOPs from the AOP-Wiki RDF SPARQL endpoint.

Usage:
    python scripts/precompute_oecd_status.py          # Write data/aop_oecd_status.json
    python scripts/precompute_oecd_status.py --dry-run # Print JSON to stdout, no file write
    python scripts/precompute_oecd_status.py --endpoint URL  # Override SPARQL endpoint

Output:
    data/aop_oecd_status.json — dict with "_meta" provenance block and "aops" mapping
    each AOP label ("AOP N") to {"status": <str>, "title": <str>}. AOPs without an
    explicit OECD status (OPTIONAL did not match) receive "Unknown".

SPARQL predicate:
    NCI Thesaurus C25688 — <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C25688>
    This is the OECD development status predicate in AOP-Wiki RDF (verified 2026-05-16).
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests

# Ensure project root is on sys.path so src imports work
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AOP-Wiki SPARQL endpoint
AOPWIKI_SPARQL_ENDPOINT = "https://aopwiki.rdf.bigcat-bioinformatics.org/sparql"

# Canonical 7-status vocabulary from REQUIREMENTS.md AOPX-06.
# Stored in _meta for documentation; NOT used to reject or normalise values.
CANONICAL_STATUS_VOCABULARY = [
    "Under Development: Contributions and Comments Welcome",
    "Under Development",
    "Open for Adoption",
    "Under Review / Internal Review",
    "EAGMST Under Review",
    "EAGMST Approved",
    "WPHA/WNT Endorsed",
]

# SPARQL query for AOP OECD status (verified live 2026-05-16).
# ?status is OPTIONAL: an AOP with no OECD status simply yields no `status` binding.
_OECD_STATUS_QUERY = """
SELECT ?label ?title ?status WHERE {
  ?aop a <http://aopkb.org/aop_ontology#AdverseOutcomePathway> ;
       <http://www.w3.org/2000/01/rdf-schema#label> ?label ;
       <http://purl.org/dc/elements/1.1/title> ?title .
  OPTIONAL { ?aop <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C25688> ?status . }
}
"""


def fetch_aop_oecd_statuses(endpoint: str = AOPWIKI_SPARQL_ENDPOINT) -> dict[str, dict]:
    """
    Query the AOP-Wiki SPARQL endpoint for OECD status of all AOPs.

    Returns a dict keyed by AOP label ("AOP N") with values:
        {"title": <str>, "status": <str | None>}

    Status is None when the OPTIONAL ?status binding did not match (AOP has no OECD status).
    Status is stored verbatim — never normalised or validated against the canonical list.
    """
    logger.info("Fetching AOP OECD statuses from %s ...", endpoint)
    try:
        response = requests.post(
            endpoint,
            data={"query": _OECD_STATUS_QUERY},
            headers={"Accept": "application/sparql-results+json"},
            timeout=60,
        )
        response.raise_for_status()
    except Exception as exc:
        logger.error("SPARQL request failed: %s", exc)
        raise

    bindings = response.json().get("results", {}).get("bindings", [])
    logger.info("Received %d AOP bindings from SPARQL", len(bindings))

    raw: dict[str, dict] = {}
    for row in bindings:
        label = row.get("label", {}).get("value", "")
        title = row.get("title", {}).get("value", "")
        # status is absent from a binding when the OPTIONAL did not match
        status = row.get("status", {}).get("value") if "status" in row else None
        if not label:
            continue
        raw[label] = {"title": title, "status": status}

    return raw


def build_oecd_status_index(endpoint: str = AOPWIKI_SPARQL_ENDPOINT) -> dict:
    """
    Build the full OECD status index with FAIR-first provenance metadata.

    Returns:
        {
            "_meta": {
                "generated_at": "2026-05-16T12:00:00Z",
                "source": "<endpoint URL>",
                "vocabulary": [<7 canonical statuses> + "Unknown"]
            },
            "aops": {
                "AOP N": {"status": "<status or 'Unknown'>", "title": "<title>"}
            }
        }

    Every AOP from the SPARQL result set receives an entry. A None status
    (OPTIONAL not matched) becomes "Unknown" — locked decision per PLAN.md.
    Status strings are stored verbatim; Pitfall 4 applies (never normalise).
    """
    raw = fetch_aop_oecd_statuses(endpoint=endpoint)

    aops: dict[str, dict] = {}
    for label, data in raw.items():
        aops[label] = {
            "status": data["status"] if data["status"] is not None else "Unknown",
            "title": data["title"],
        }

    # Log status distribution
    from collections import Counter
    dist = Counter(e["status"] for e in aops.values())
    logger.info("Status distribution across %d AOPs:", len(aops))
    for status_val, count in sorted(dist.items(), key=lambda x: -x[1]):
        logger.info("  %-55s %d", status_val, count)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "_meta": {
            "generated_at": generated_at,
            "source": endpoint,
            "vocabulary": CANONICAL_STATUS_VOCABULARY + ["Unknown"],
        },
        "aops": aops,
    }


def main() -> None:
    """Parse args, generate the index, write or print data/aop_oecd_status.json."""
    parser = argparse.ArgumentParser(
        description="Pre-compute OECD development status for all AOPs from AOP-Wiki RDF."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the JSON to stdout instead of writing to disk.",
    )
    parser.add_argument(
        "--endpoint",
        default=AOPWIKI_SPARQL_ENDPOINT,
        help="Override the AOP-Wiki SPARQL endpoint URL.",
    )
    args = parser.parse_args()

    index = build_oecd_status_index(endpoint=args.endpoint)

    if not index.get("aops"):
        logger.error("AOP-Wiki returned no OECD status rows; keeping the existing snapshot")
        sys.exit(1)

    if args.dry_run:
        print(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True))
        logger.info("Dry-run: JSON printed to stdout. %d AOPs.", len(index["aops"]))
        return

    output_path = os.path.join(_PROJECT_ROOT, "data", "aop_oecd_status.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Write a sibling and rename: an in-place rewrite truncates first (a failed run
    # would leave a half file), and the rename also works on a file this uid does
    # not own, which is the case for snapshots once written by hand on the host.
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, output_path)

    logger.info(
        "Wrote aop_oecd_status.json: %d AOPs -> %s",
        len(index["aops"]),
        output_path,
    )


if __name__ == "__main__":
    main()
