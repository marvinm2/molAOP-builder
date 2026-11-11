"""
Pre-compute AOP membership for all Key Events from AOP-Wiki

Usage:
    python scripts/precompute_ke_aop_membership.py

Output:
    data/ke_aop_membership.json — dict mapping KE label to list of {aop_id, aop_title} dicts
"""

import json
import logging
import os
import sys

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


def fetch_ke_aop_memberships():
    """
    Fetch all AOP->KE relationships from AOP-Wiki in a single SPARQL call.

    Returns a dict keyed by KE label (e.g. "KE 55") with lists of
    {aop_id, aop_title} dicts.
    """
    sparql_query = """
PREFIX aopo: <http://aopkb.org/aop_ontology#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?aopId ?aopTitle ?keId
WHERE {
    ?aop a aopo:AdverseOutcomePathway ;
         rdfs:label ?aopId ;
         dc:title ?aopTitle ;
         aopo:has_key_event ?ke .
    ?ke rdfs:label ?keId .
}
ORDER BY ?aopId ?keId
"""

    logger.info("Fetching all AOP->KE relationships from AOP-Wiki (single SPARQL call)...")
    try:
        response = requests.post(
            AOPWIKI_SPARQL_ENDPOINT,
            data={"query": sparql_query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=60,
        )
        response.raise_for_status()
    except Exception as e:
        logger.error("SPARQL request failed: %s", e)
        raise

    results = response.json().get("results", {}).get("bindings", [])
    logger.info("Received %d AOP-KE binding rows from SPARQL", len(results))

    # Group by KE label -> list of {aop_id, aop_title}
    membership: dict[str, list[dict]] = {}
    for row in results:
        ke_id = row.get("keId", {}).get("value", "")
        aop_id = row.get("aopId", {}).get("value", "")
        aop_title = row.get("aopTitle", {}).get("value", "")
        if not ke_id:
            continue
        if ke_id not in membership:
            membership[ke_id] = []
        if aop_id:
            membership[ke_id].append({"aop_id": aop_id, "aop_title": aop_title})

    return membership


def main():
    """Fetch AOP membership data and write data/ke_aop_membership.json."""
    membership = fetch_ke_aop_memberships()
    if not membership:
        logger.error("AOP-Wiki returned no AOP-KE rows; keeping the existing snapshot")
        sys.exit(1)

    output_path = os.path.join(_PROJECT_ROOT, "data", "ke_aop_membership.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Write a sibling and rename: an in-place rewrite truncates first (a failed run
    # would leave a half file), and the rename also works on a file this uid does
    # not own, which is the case for snapshots once written by hand on the host.
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(membership, f, indent=2, ensure_ascii=False)
    os.replace(tmp, output_path)

    total_aop_memberships = sum(len(v) for v in membership.values())
    logger.info(
        "Wrote ke_aop_membership.json: %d KEs, %d total AOP memberships -> %s",
        len(membership),
        total_aop_memberships,
        output_path,
    )


if __name__ == "__main__":
    main()
