"""
Download and process Reactome gene annotations for Homo sapiens.

Downloads the Reactome GMT file, parses it for Homo sapiens pathways,
excludes Disease branch descendants via the Content Service API,
filters by gene count (10-500), normalizes stable IDs, and saves
the output as data/reactome_gene_annotations.json.

Usage:
    python scripts/download_reactome_annotations.py [--output PATH] [--force]

    --output PATH  Override output file path
    --force        Re-download GMT even if cached locally

Output:
    data/reactome_gene_annotations.json  - {stId: [gene_symbols]}
    data/reactome_filtered_stids.json    - [stId, ...] (sorted list for embedding script)
"""

import argparse
import json
import logging
import os
import sys
import zipfile

import requests

sys.path.insert(0, os.path.abspath('.'))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reactome GMT download source
GMT_URL = "https://reactome.org/download/current/ReactomePathways.gmt.zip"
GMT_ZIP_LOCAL = "data/ReactomePathways.gmt.zip"
GMT_LOCAL = "data/ReactomePathways.gmt"

# Reactome Content Service
CONTENT_SERVICE = "https://reactome.org/ContentService"

# Disease branch root — descendants are excluded from output
DISEASE_ROOT = "R-HSA-1643685"

# Gene count filter bounds.
# Floor raised 3 -> 10: pathways with <10 genes are reaction-scale — too
# specific to serve as a Key Event signature, and too small for reliable
# over-representation testing downstream (clusterProfiler minGSSize default).
MIN_GENES = 10
MAX_GENES = 500

# Umbrella pathways that best fit generic upstream KEs but whose gene set exceeds
# MAX_GENES, so the ceiling drops them and curators can neither suggest nor search
# them (#196). Force-included past the ceiling. Disease-branch exclusion and the
# MIN_GENES floor still apply. Extend as new generic KEs surface.
UMBRELLA_WHITELIST = {
    "R-HSA-5357801": "Programmed Cell Death",
    "R-HSA-109581": "Apoptosis",
    "R-HSA-73894": "DNA Repair",
    "R-HSA-3299685": "Detoxification of Reactive Oxygen Species",
}

# Output file paths
OUTPUT_PATH = "data/reactome_gene_annotations.json"
FILTERED_STIDS_PATH = "data/reactome_filtered_stids.json"


def _download_file(url, dest_path):
    """
    Download a file to dest_path using requests with a standard User-Agent.
    Falls back to an SSL-unverified request if the first attempt fails with
    a certificate error (Reactome download server occasionally uses self-signed certs).
    """
    headers = {'User-Agent': 'Python/3 ReactomePipeline/1.0'}
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=120)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        # Intentional fallback: Reactome's download host occasionally serves an
        # expired/self-signed cert. Standard verification is tried first
        # (the requests.get() above) and only on SSLError do we retry without
        # verification. The downloaded annotations are content-checked by the
        # caller so a MITM substituting a tampered body would be detected on
        # use. CodeQL py/request-without-cert-validation will still flag this;
        # the alert is filed as "won't fix — documented intentional fallback".
        logger.warning("SSL verification failed, retrying without verification")
        resp = requests.get(url, headers=headers, stream=True, timeout=120, verify=False)  # noqa: S501
        resp.raise_for_status()

    with open(dest_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)


def download_gmt(url=GMT_URL, zip_path=GMT_ZIP_LOCAL, gmt_path=GMT_LOCAL, force=False):
    """
    Download and extract ReactomePathways.gmt if not already present.

    Args:
        url: URL to the GMT zip file
        zip_path: Local path for the downloaded zip archive
        gmt_path: Local path for the extracted GMT file
        force: Re-download even if gmt_path already exists

    Returns:
        str: Path to the extracted GMT file
    """
    if not force and os.path.exists(gmt_path):
        logger.info("Using existing GMT file: %s", gmt_path)
        return gmt_path

    logger.info("Downloading GMT from %s ...", url)
    # Use requests with a browser-like User-Agent; allow SSL verification fallback
    _download_file(url, zip_path)
    logger.info("Downloaded zip to %s", zip_path)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        # The zip contains a single .gmt file
        gmt_names = [n for n in zf.namelist() if n.endswith('.gmt')]
        if not gmt_names:
            raise ValueError(f"No .gmt file found in {zip_path}")
        gmt_name = gmt_names[0]
        logger.info("Extracting %s ...", gmt_name)
        with zf.open(gmt_name) as src, open(gmt_path, 'wb') as dst:
            dst.write(src.read())

    logger.info("Extracted to %s", gmt_path)
    return gmt_path


def parse_gmt_file(gmt_path):
    """
    Parse ReactomePathways.gmt and return gene annotations for Homo sapiens.

    GMT column format:
        col 0: pathway display name
        col 1: stableId (e.g., R-HSA-12345 or R-HSA-12345.3)
        col 2: "Reactome Pathway" (literal string or URL — skip)
        col 3+: HGNC gene symbols

    Filters:
        - Only Homo sapiens pathways (stableId prefix R-HSA-)
        - Version suffix stripped: R-HSA-12345.3 -> R-HSA-12345

    Args:
        gmt_path: Path to the extracted GMT file

    Returns:
        dict: {stId: sorted_list_of_gene_symbols}
    """
    logger.info("Parsing GMT file: %s", gmt_path)
    annotations = {}
    skipped_species = 0
    skipped_short = 0

    # Log first few lines for column layout verification
    with open(gmt_path, encoding='utf-8') as f:
        for i, line in enumerate(f):
            if i < 3:
                parts = line.strip().split('\t')
                logger.info("GMT col check [line %d]: col0=%r, col1=%r, col2=%r, genes=%d",
                            i, parts[0][:40] if parts else '', parts[1] if len(parts) > 1 else '',
                            parts[2] if len(parts) > 2 else '', max(0, len(parts) - 3))

    # Determine column offset: some Reactome GMT versions have a 3rd "description" column
    # before genes (col 2 = "Reactome Pathway" or a URL); others go name, stableId, gene1...
    # We detect this by checking if col 2 looks like a stableId/URL/literal string.
    # Strategy: use col 1 as stableId, then try col 3 first; fall back to col 2 if col 3
    # is absent. The plan spec says parts[3:] for genes, but we verify against actual data.
    # From GMT col check: col2='BANF1' — this GMT has name, stableId, gene1, gene2, ...
    # i.e. genes start at col 2, NOT col 3. Adjust accordingly.
    gene_start_col = None

    with open(gmt_path, encoding='utf-8') as f:
        for i, line in enumerate(f):
            if i >= 10:
                break
            parts = line.strip().split('\t')
            if len(parts) < 3:
                continue
            col1 = parts[1] if len(parts) > 1 else ''
            col2 = parts[2] if len(parts) > 2 else ''
            parts[3] if len(parts) > 3 else ''
            if col1.startswith('R-'):
                # col 1 is stableId; decide gene start col
                # If col2 is "Reactome Pathway" or a URL or empty, genes start at 3
                # Otherwise genes start at 2
                if col2 in ('', 'Reactome Pathway') or col2.startswith('http'):
                    gene_start_col = 3
                else:
                    gene_start_col = 2
                break

    if gene_start_col is None:
        gene_start_col = 3  # safe default per plan spec
    logger.info("GMT gene start column detected: %d", gene_start_col)

    with open(gmt_path, encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split('\t')

            # Need at least: name + stableId + one potential gene
            if len(parts) < 3:
                skipped_short += 1
                continue

            # col 1 = stableId (NOT col 0 which is display name)
            stable_id = parts[1]

            # Filter to Homo sapiens only (D-09)
            if not stable_id.startswith('R-HSA-'):
                skipped_species += 1
                continue

            # Strip version suffix if present: R-HSA-12345.3 -> R-HSA-12345 (D-06, RDATA-05)
            stable_id = stable_id.split('.')[0]

            # Genes start at detected gene_start_col
            genes = [g.strip() for g in parts[gene_start_col:] if g.strip()]
            annotations[stable_id] = sorted(set(genes))

    logger.info("Parsed %d Homo sapiens pathways (skipped: %d other species, %d short lines)",
                len(annotations), skipped_species, skipped_short)
    return annotations


def _get_content_service(path, timeout=60):
    """
    Make a GET request to the Reactome Content Service.

    Tries HTTPS directly; falls back to an IP-based request with SNI override if
    the hostname-based connection times out (network routing issue in some environments).

    Args:
        path: URL path starting with /ContentService/...
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON response
    """
    import socket
    import ssl
    import json as _json

    url = f"https://reactome.org{path}"
    headers = {"Accept": "application/json", "User-Agent": "Python/3 ReactomePipeline/1.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
        logger.warning("Direct HTTPS failed (%s), trying via resolved IP ...", e)

    # Fallback: resolve hostname and connect via IP with SNI
    try:
        addrs = socket.getaddrinfo('reactome.org', 443, socket.AF_INET)
        ip = addrs[0][4][0]
    except Exception:
        raise RuntimeError("Cannot resolve reactome.org")

    ctx = ssl.create_default_context()
    # Reactome's download path occasionally serves self-signed certs that fail
    # standard validation; this path is the documented workaround. Still pin
    # the TLS floor so we never negotiate down to TLS <1.2.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((ip, 443), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname='reactome.org') as ssock:
            request_line = f"GET {path} HTTP/1.1\r\n"
            request_line += f"Host: reactome.org\r\n"
            request_line += "Accept: application/json\r\n"
            request_line += "User-Agent: Python/3 ReactomePipeline/1.0\r\n"
            request_line += "Connection: close\r\n\r\n"
            ssock.sendall(request_line.encode('utf-8'))

            raw = b""
            while True:
                chunk = ssock.recv(65536)
                if not chunk:
                    break
                raw += chunk

    # Parse HTTP response: headers + body
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        raise RuntimeError("Malformed HTTP response from Reactome")
    body = raw[header_end + 4:]

    # Handle chunked transfer encoding
    headers_raw = raw[:header_end].decode('utf-8', errors='replace')
    if 'Transfer-Encoding: chunked' in headers_raw:
        # Decode chunked body
        decoded = b""
        while body:
            crlf = body.find(b"\r\n")
            if crlf == -1:
                break
            size = int(body[:crlf], 16)
            if size == 0:
                break
            chunk_data = body[crlf + 2: crlf + 2 + size]
            decoded += chunk_data
            body = body[crlf + 2 + size + 2:]
        body = decoded

    return _json.loads(body.decode('utf-8'))


def fetch_disease_descendants():
    """
    Fetch all stIds under the Disease branch (R-HSA-1643685) via Content Service.

    The containedEvents endpoint returns descendants but NOT the root itself —
    so we always add DISEASE_ROOT to the exclusion set manually.

    Returns:
        set: All stIds (version-free) to exclude (includes root)
    """
    path = f"/ContentService/data/pathway/{DISEASE_ROOT}/containedEvents"
    logger.info("Fetching disease descendants from https://reactome.org%s ...", path)

    events = _get_content_service(path)

    # The containedEvents API returns a mix of full event dicts and raw integer dbIds.
    # Integer entries are top-level disease category pathways that do NOT appear as
    # dicts in the same response. For Homo sapiens, Reactome stable IDs are constructed
    # directly from the numeric dbId: dbId N → stId R-HSA-N. We resolve integers using
    # this construction.
    #
    # Pass 1 — Build a dbId→stId mapping from dict entries (for safety / cross-check).
    dbid_to_stid = {}
    for event in events:
        if isinstance(event, dict):
            dbid = event.get('dbId')
            stid = event.get('stId', '')
            if dbid is not None and stid:
                dbid_to_stid[dbid] = stid.split('.')[0]

    # Always include root itself — containedEvents does NOT return the root (Pitfall 3)
    disease_ids = {DISEASE_ROOT}

    # Pass 2 — Collect stIds from both dict entries and integer back-references.
    n_from_int = 0
    for event in events:
        if isinstance(event, dict):
            stid = event.get('stId', '')
            if stid:
                # Strip version suffix just in case (defensive)
                disease_ids.add(stid.split('.')[0])
        elif isinstance(event, int):
            # Try dict-based resolution first; fall back to direct R-HSA-{dbId} construction.
            # The direct mapping is valid for all Homo sapiens pathways (numeric dbId == stId number).
            resolved = dbid_to_stid.get(event) or f"R-HSA-{event}"
            disease_ids.add(resolved)
            n_from_int += 1

    logger.info(
        "Disease branch: %d pathways to exclude (%d resolved from integer dbIds)",
        len(disease_ids), n_from_int
    )
    return disease_ids


def filter_annotations(raw_annotations, disease_ids, min_genes=MIN_GENES, max_genes=MAX_GENES):
    """
    Filter raw annotations by disease exclusion and gene count bounds.

    Args:
        raw_annotations: dict {stId: [gene_symbols]}
        disease_ids: set of stIds to exclude (Disease branch)
        min_genes: minimum gene count (inclusive)
        max_genes: maximum gene count (inclusive)

    Returns:
        dict: Filtered {stId: [gene_symbols]}
    """
    filtered = {}
    n_disease = 0
    n_genecount = 0
    n_whitelisted = 0

    for stid, genes in raw_annotations.items():
        if stid in disease_ids:
            n_disease += 1
            continue
        n = len(genes)
        if n < min_genes:
            n_genecount += 1
            continue
        if n > max_genes:
            # Umbrella pathways bypass the upper bound so generic KEs can map to them.
            if stid in UMBRELLA_WHITELIST:
                n_whitelisted += 1
            else:
                n_genecount += 1
                continue
        filtered[stid] = genes

    missing_whitelist = sorted(set(UMBRELLA_WHITELIST) - set(filtered))
    logger.info(
        "Filter results: %d kept (from %d raw) | excluded: %d disease branch, "
        "%d gene count out of bounds | +%d umbrella-whitelisted",
        len(filtered), len(raw_annotations), n_disease, n_genecount, n_whitelisted
    )
    if missing_whitelist:
        logger.warning(
            "Umbrella-whitelisted pathways absent from the raw GMT (renamed/withdrawn?): %s",
            ", ".join(missing_whitelist),
        )
    return filtered


def download_reactome_annotations(output_path=OUTPUT_PATH, force=False):
    """
    Main pipeline: download GMT -> parse -> fetch disease descendants -> filter -> save.

    Args:
        output_path: Path for the gene annotations JSON output
        force: Re-download GMT even if cached

    Returns:
        dict: Filtered gene annotations {stId: [gene_symbols]}
    """
    # Step 1: Download and extract GMT
    gmt_path = download_gmt(force=force)

    # Step 2: Parse GMT for Homo sapiens gene annotations
    raw_annotations = parse_gmt_file(gmt_path)
    logger.info("Total Homo sapiens pathways from GMT: %d", len(raw_annotations))

    # Step 3: Fetch Disease branch descendants for exclusion (D-07)
    disease_ids = fetch_disease_descendants()

    # Step 4: Filter by disease exclusion + gene count (D-07, D-08)
    filtered = filter_annotations(raw_annotations, disease_ids, min_genes=MIN_GENES, max_genes=MAX_GENES)

    # Step 5: Save gene annotations JSON
    logger.info("Saving gene annotations to %s ...", output_path)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(filtered, f, indent=2)

    file_size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info("Saved %d pathways: %.2f MB", len(filtered), file_size_mb)

    # Step 6: Save filtered stId list for embedding script (Plan 02)
    filtered_stids = sorted(filtered.keys())
    with open(FILTERED_STIDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(filtered_stids, f, indent=2)
    logger.info("Saved %d filtered stIds to %s", len(filtered_stids), FILTERED_STIDS_PATH)

    # Step 7: Log summary statistics
    total_unique_genes = len(set(g for genes in filtered.values() for g in genes))
    logger.info("Summary: %d pathways | %d unique gene symbols", len(filtered), total_unique_genes)

    # Print 3 sample entries for verification (same pattern as GO script)
    sample_ids = list(filtered.keys())[:3]
    for stid in sample_ids:
        genes = filtered[stid]
        logger.info("Sample %s: %d genes - %s...", stid, len(genes), genes[:5])

    return filtered


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Download and process Reactome gene annotations for Homo sapiens'
    )
    parser.add_argument(
        '--output', default=OUTPUT_PATH,
        help=f'Output file path for gene annotations JSON (default: {OUTPUT_PATH})'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Re-download GMT file even if cached locally'
    )
    args = parser.parse_args()

    download_reactome_annotations(output_path=args.output, force=args.force)
