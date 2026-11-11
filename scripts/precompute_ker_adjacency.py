"""
Pre-compute KER adjacency data for all AOPs from AOP-Wiki

Usage:
    python scripts/precompute_ker_adjacency.py

Output:
    data/ker_adjacency.json — dict keyed by AOP ID with kes and kers arrays,
    including KE type classification (MIE/KE/AO) per AOP.
"""

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

# KER edges query — fetches upstream/downstream KE pairs per AOP
SPARQL_KER_EDGES_PRIMARY = """
PREFIX aopo: <http://aopkb.org/aop_ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>

SELECT DISTINCT ?aopId ?aopTitle ?keUpId ?keUpTitle ?keDownId ?keDownTitle
WHERE {
  ?aop a aopo:AdverseOutcomePathway ;
       rdfs:label ?aopId ;
       dc:title ?aopTitle .
  ?ker a aopo:KeyEventRelationship ;
       aopo:has_upstream_key_event ?keUp ;
       aopo:has_downstream_key_event ?keDown .
  ?keUp rdfs:label ?keUpId ; dc:title ?keUpTitle .
  ?keDown rdfs:label ?keDownId ; dc:title ?keDownTitle .
  ?aop aopo:has_key_event_relationship ?ker .
}
ORDER BY ?aopId
"""

# Fallback query — fetch all KERs globally (without AOP constraint)
SPARQL_KER_EDGES_FALLBACK = """
PREFIX aopo: <http://aopkb.org/aop_ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>

SELECT DISTINCT ?keUpId ?keUpTitle ?keDownId ?keDownTitle
WHERE {
  ?ker a aopo:KeyEventRelationship ;
       aopo:has_upstream_key_event ?keUp ;
       aopo:has_downstream_key_event ?keDown .
  ?keUp rdfs:label ?keUpId ; dc:title ?keUpTitle .
  ?keDown rdfs:label ?keDownId ; dc:title ?keDownTitle .
}
"""

# KE type classification query — fetches MIE and AO designations per AOP
SPARQL_KE_TYPES = """
PREFIX aopo: <http://aopkb.org/aop_ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?aopId ?mieId ?aoId
WHERE {
  ?aop a aopo:AdverseOutcomePathway ;
       rdfs:label ?aopId .
  OPTIONAL { ?aop aopo:has_molecular_initiating_event ?mie . ?mie rdfs:label ?mieId . }
  OPTIONAL { ?aop aopo:has_adverse_outcome ?ao . ?ao rdfs:label ?aoId . }
}
"""


def sparql_request(query: str, label: str) -> list[dict]:
    """Execute a SPARQL query and return bindings list."""
    logger.info("Executing SPARQL query: %s", label)
    response = requests.post(
        AOPWIKI_SPARQL_ENDPOINT,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
        timeout=120,
    )
    response.raise_for_status()
    bindings = response.json().get("results", {}).get("bindings", [])
    logger.info("Received %d rows for query: %s", len(bindings), label)
    return bindings


def fetch_ker_edges_primary() -> tuple[dict, bool]:
    """
    Fetch KER edges using the primary SPARQL query (AOP-linked KERs).

    Returns (aop_kers, success) where aop_kers is keyed by aop_id
    with list of {upstream, upstream_title, downstream, downstream_title} dicts.
    """
    try:
        bindings = sparql_request(SPARQL_KER_EDGES_PRIMARY, "KER edges (primary)")
    except Exception as e:
        logger.error("Primary KER edges SPARQL failed: %s", e)
        return {}, False

    if not bindings:
        logger.warning("Primary KER edges query returned 0 rows — will use fallback")
        return {}, False

    aop_kers: dict[str, dict] = {}
    for row in bindings:
        aop_id = row.get("aopId", {}).get("value", "")
        aop_title = row.get("aopTitle", {}).get("value", "")
        ke_up_id = row.get("keUpId", {}).get("value", "")
        ke_up_title = row.get("keUpTitle", {}).get("value", "")
        ke_down_id = row.get("keDownId", {}).get("value", "")
        ke_down_title = row.get("keDownTitle", {}).get("value", "")

        if not aop_id or not ke_up_id or not ke_down_id:
            continue

        if aop_id not in aop_kers:
            aop_kers[aop_id] = {"title": aop_title, "kers": [], "ke_titles": {}}

        aop_kers[aop_id]["kers"].append(
            {"upstream": ke_up_id, "downstream": ke_down_id}
        )
        aop_kers[aop_id]["ke_titles"][ke_up_id] = ke_up_title
        aop_kers[aop_id]["ke_titles"][ke_down_id] = ke_down_title

    logger.info("Primary query: found KERs for %d AOPs", len(aop_kers))
    return aop_kers, True


def fetch_ker_edges_fallback() -> dict:
    """
    Fallback: fetch all KERs globally and intersect with ke_aop_membership.json.

    Returns global_kers as list of {upstream, upstream_title, downstream, downstream_title}.
    """
    bindings = sparql_request(SPARQL_KER_EDGES_FALLBACK, "KER edges (fallback global)")
    global_kers = []
    for row in bindings:
        ke_up_id = row.get("keUpId", {}).get("value", "")
        ke_up_title = row.get("keUpTitle", {}).get("value", "")
        ke_down_id = row.get("keDownId", {}).get("value", "")
        ke_down_title = row.get("keDownTitle", {}).get("value", "")

        if not ke_up_id or not ke_down_id:
            continue
        global_kers.append(
            {
                "upstream": ke_up_id,
                "upstream_title": ke_up_title,
                "downstream": ke_down_id,
                "downstream_title": ke_down_title,
            }
        )

    # Load ke_aop_membership.json to assign KERs to AOPs
    membership_path = os.path.join(_PROJECT_ROOT, "data", "ke_aop_membership.json")
    if not os.path.exists(membership_path):
        logger.error(
            "ke_aop_membership.json not found at %s — cannot use fallback", membership_path
        )
        raise FileNotFoundError(
            f"ke_aop_membership.json required for fallback but not found: {membership_path}"
        )

    with open(membership_path, "r", encoding="utf-8") as f:
        ke_aop_membership = json.load(f)

    logger.info(
        "Loaded ke_aop_membership.json: %d KEs", len(ke_aop_membership)
    )

    # Build ke -> set of aop_ids index
    ke_to_aops: dict[str, dict] = {}
    for ke_id, aop_list in ke_aop_membership.items():
        ke_to_aops[ke_id] = {}
        for entry in aop_list:
            aop_id = entry.get("aop_id", "")
            aop_title = entry.get("aop_title", "")
            if aop_id:
                ke_to_aops[ke_id][aop_id] = aop_title

    # Assign global KERs to AOPs where both upstream and downstream belong to the same AOP
    aop_kers: dict[str, dict] = {}
    for ker in global_kers:
        up_id = ker["upstream"]
        down_id = ker["downstream"]

        up_aops = ke_to_aops.get(up_id, {})
        down_aops = ke_to_aops.get(down_id, {})

        # Find shared AOPs
        shared_aop_ids = set(up_aops.keys()) & set(down_aops.keys())
        for aop_id in shared_aop_ids:
            if aop_id not in aop_kers:
                aop_title = up_aops.get(aop_id, "")
                aop_kers[aop_id] = {"title": aop_title, "kers": [], "ke_titles": {}}

            aop_kers[aop_id]["kers"].append(
                {"upstream": up_id, "downstream": down_id}
            )
            aop_kers[aop_id]["ke_titles"][up_id] = ker["upstream_title"]
            aop_kers[aop_id]["ke_titles"][down_id] = ker["downstream_title"]

    logger.info("Fallback assigned KERs to %d AOPs", len(aop_kers))
    return aop_kers


def fetch_ke_types() -> tuple[dict, dict]:
    """
    Fetch MIE and AO designations per AOP.

    Returns (aop_mies, aop_aos) — dicts keyed by aop_id, values are sets of KE IDs.
    """
    try:
        bindings = sparql_request(SPARQL_KE_TYPES, "KE type classification")
    except Exception as e:
        logger.error("KE type SPARQL query failed: %s", e)
        return {}, {}

    aop_mies: dict[str, set] = {}
    aop_aos: dict[str, set] = {}

    for row in bindings:
        aop_id = row.get("aopId", {}).get("value", "")
        mie_id = row.get("mieId", {}).get("value", "")
        ao_id = row.get("aoId", {}).get("value", "")

        if not aop_id:
            continue

        if mie_id:
            aop_mies.setdefault(aop_id, set()).add(mie_id)
        if ao_id:
            aop_aos.setdefault(aop_id, set()).add(ao_id)

    logger.info(
        "KE type data: %d AOPs with MIEs, %d AOPs with AOs",
        len(aop_mies), len(aop_aos)
    )
    return aop_mies, aop_aos


def detect_cycles(aop_id: str, kers: list[dict]) -> list[list[str]]:
    """
    Detect cycles in a directed KER graph using DFS.

    Returns list of cycles (each cycle is a list of KE IDs forming the loop).
    """
    # Build adjacency list
    adj: dict[str, list[str]] = {}
    for ker in kers:
        up = ker["upstream"]
        down = ker["downstream"]
        adj.setdefault(up, []).append(down)

    cycles = []
    visited: set[str] = set()
    rec_stack: set[str] = set()
    path: list[str] = []

    def dfs(node: str):
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbor in adj.get(node, []):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in rec_stack:
                # Found a cycle — extract it from path
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)

        path.pop()
        rec_stack.discard(node)

    all_nodes = set()
    for ker in kers:
        all_nodes.add(ker["upstream"])
        all_nodes.add(ker["downstream"])

    for node in all_nodes:
        if node not in visited:
            dfs(node)

    if cycles:
        logger.warning(
            "AOP %s has %d cycle(s): %s",
            aop_id, len(cycles), cycles
        )

    return cycles


def build_output(aop_kers: dict, aop_mies: dict, aop_aos: dict) -> dict:
    """
    Build the final output JSON structure keyed by AOP ID.

    Each AOP entry contains:
    - title: AOP title
    - kes: list of {id, title, type} — type is MIE/AO/KE
    - kers: list of {upstream, downstream}
    """
    output: dict = {}

    for aop_id, data in aop_kers.items():
        kers = data["kers"]
        ke_titles = data["ke_titles"]
        title = data["title"]

        mie_set = aop_mies.get(aop_id, set())
        ao_set = aop_aos.get(aop_id, set())

        # Collect unique KE IDs appearing in KERs
        ke_ids_seen: set[str] = set()
        for ker in kers:
            ke_ids_seen.add(ker["upstream"])
            ke_ids_seen.add(ker["downstream"])

        # Build KE list with type classification
        kes = []
        for ke_id in sorted(ke_ids_seen):
            if ke_id in mie_set:
                ke_type = "MIE"
            elif ke_id in ao_set:
                ke_type = "AO"
            else:
                ke_type = "KE"
            kes.append(
                {
                    "id": ke_id,
                    "title": ke_titles.get(ke_id, ""),
                    "type": ke_type,
                }
            )

        # Detect and log cycles
        detect_cycles(aop_id, kers)

        output[aop_id] = {
            "title": title,
            "kes": kes,
            "kers": kers,
        }

    return output


def main():
    """Fetch KER adjacency data and write data/ker_adjacency.json."""
    logger.info("Starting KER adjacency precompute...")

    # Step 1: Try primary KER edges query, fall back if needed
    aop_kers, primary_ok = fetch_ker_edges_primary()
    if not primary_ok:
        logger.info("Using fallback KER edges approach...")
        aop_kers = fetch_ker_edges_fallback()

    logger.info("Total AOPs with KER data: %d", len(aop_kers))
    if not aop_kers:
        logger.error("No KER edges from AOP-Wiki; keeping the existing snapshot")
        sys.exit(1)

    # Step 2: Fetch KE type classifications (MIE / AO)
    aop_mies, aop_aos = fetch_ke_types()

    # Step 3: Build output structure
    output = build_output(aop_kers, aop_mies, aop_aos)

    # Step 4: Add metadata
    aop_count = len(output)
    total_kers = sum(len(v["kers"]) for v in output.values())
    total_kes = sum(len(v["kes"]) for v in output.values())

    output["_metadata"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aop_count": aop_count,
        "total_kers": total_kers,
        "total_kes": total_kes,
        "primary_query_used": primary_ok,
    }

    # Step 5: Write output
    output_path = os.path.join(_PROJECT_ROOT, "data", "ker_adjacency.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Write a sibling and rename: an in-place rewrite truncates first (a failed run
    # would leave a half file), and the rename also works on a file this uid does
    # not own, which is the case for snapshots once written by hand on the host.
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    os.replace(tmp, output_path)

    logger.info(
        "Wrote ker_adjacency.json: %d AOPs, %d KERs, %d KEs -> %s",
        aop_count, total_kers, total_kes, output_path,
    )


if __name__ == "__main__":
    main()
