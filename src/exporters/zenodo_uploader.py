"""
Zenodo API deposit and versioning workflow.
Requires ZENODO_API_TOKEN environment variable.
Uses the Zenodo bucket PUT API (not the deprecated /files POST endpoint).
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ZENODO_BASE = "https://zenodo.org/api"

# Default mount path Docker Swarm uses for a secret named `zenodo_api_token`
# / `zenodo_sandbox_api_token`.
_SECRET_PATHS = {
    "ZENODO_API_TOKEN": "/run/secrets/zenodo_api_token",
    "ZENODO_SANDBOX_API_TOKEN": "/run/secrets/zenodo_sandbox_api_token",
}


def resolve_zenodo_token(var_name="ZENODO_API_TOKEN"):
    """Resolve a Zenodo API token from a Docker secret or the environment.

    Checked in order, first hit wins:

      1. ``<VAR>_FILE`` — path to a file holding the token. The conventional
         way to point a container at a secret mounted somewhere non-default.
      2. ``/run/secrets/<lowercased var>`` — where Swarm mounts a secret of
         that name, so no configuration is needed in the common case.
      3. ``<VAR>`` — the plain environment variable (back-compat).

    Secret files are preferred over the env var because an env var is readable
    by anyone who can run ``docker service inspect`` on the cluster, which is a
    wider audience than the people who should hold a publishing credential
    (#191). The env var still works so an existing deployment keeps running.

    Returns:
        The token string, or None when no source yields a non-empty value.
    """
    path = os.environ.get(f"{var_name}_FILE") or _SECRET_PATHS.get(var_name)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
            if token:
                return token
            logger.warning("Zenodo token file %s is empty — falling back", path)
        except FileNotFoundError:
            pass  # Expected whenever the secret isn't mounted.
        except OSError as e:
            logger.warning("Could not read Zenodo token file %s: %s", path, e)

    token = (os.environ.get(var_name) or "").strip()
    return token or None

# #158 follow-up: container user lacks write bit on the gluster-backed
# /app/data mount. Successful Zenodo publishes must never appear to fail
# just because we can't persist the local meta file — fall back to /tmp/
# and log loudly so the operator can copy it across.
META_FALLBACK_PATH = Path("/tmp/zenodo_meta_pending.json")


def persist_meta_with_fallback(meta_path, payload: dict) -> Path:
    """Write zenodo_meta.json with an EACCES fallback to /tmp/.

    Returns the actual Path written. On PermissionError the payload is
    persisted to META_FALLBACK_PATH and an error log instructs the operator
    to copy it back into place (uid alignment on the gluster mount is the
    upstream fix; see issue #158).
    """
    path = Path(meta_path)
    body = json.dumps(payload, indent=2) + "\n"
    try:
        path.write_text(body)
        return path
    except PermissionError:
        META_FALLBACK_PATH.write_text(body)
        logger.error(
            "Could not write %s (EACCES). Saved to %s — operator must copy "
            "it to %s on the host (or to "
            "/mnt/gluster/docker/molaop-builder/data/zenodo_meta.json if the "
            "host filesystem differs from the container view). #158 follow-up.",
            path, META_FALLBACK_PATH, path,
        )
        return META_FALLBACK_PATH


def zenodo_publish(files: dict, metadata: dict, existing_deposition_id: int = None) -> dict:
    """
    Publish or update a Zenodo dataset record.

    Args:
        files: {filename: content_str} — files to upload (GMT + Turtle + README)
        metadata: Zenodo metadata dict (title, creators, description, etc.)
        existing_deposition_id: int if updating an existing published record; None for first publish

    Returns:
        {"doi": "10.5281/zenodo.XXXXXXX", "deposition_id": XXXXXXX, "concept_doi": "..."}

    Raises:
        EnvironmentError: ZENODO_API_TOKEN not set
        requests.HTTPError: Zenodo API returned non-2xx
    """
    token = resolve_zenodo_token("ZENODO_API_TOKEN")
    if not token:
        raise EnvironmentError(
            "No Zenodo API token available — set the ZENODO_API_TOKEN environment "
            "variable, mount a Docker secret at /run/secrets/zenodo_api_token, or "
            "point ZENODO_API_TOKEN_FILE at a file containing the token"
        )

    auth_header = {"Authorization": f"Bearer {token}"}
    json_header = {**auth_header, "Content-Type": "application/json"}

    if existing_deposition_id:
        # New version of existing record
        logger.info("Creating new Zenodo version from deposition %s", existing_deposition_id)
        r = requests.post(
            f"{ZENODO_BASE}/deposit/depositions/{existing_deposition_id}/actions/newversion",
            headers=auth_header,
            timeout=30,
        )
        r.raise_for_status()
        draft_url = r.json()["links"]["latest_draft"]
        dep_id = int(draft_url.rstrip("/").split("/")[-1])
        # Get bucket URL for new draft
        r2 = requests.get(draft_url, headers=auth_header, timeout=30)
        r2.raise_for_status()
        draft = r2.json()
        bucket_url = draft["links"]["bucket"]
        # Zenodo's newversion API inherits the previous version's files
        # verbatim. Without deleting them, the new draft would publish with
        # whatever shape the prior deposit used PLUS our new uploads — this
        # is what broke v2. Delete each inherited file before we upload.
        inherited = draft.get("files", [])
        if inherited:
            logger.info(
                "Inherited %d file(s) from previous version — deleting before upload",
                len(inherited),
            )
            for f in inherited:
                fid = f["id"]
                fname = f.get("filename", "<unknown>")
                dr = requests.delete(
                    f"{ZENODO_BASE}/deposit/depositions/{dep_id}/files/{fid}",
                    headers=auth_header,
                    timeout=30,
                )
                if dr.status_code not in (200, 204):
                    raise RuntimeError(
                        f"Failed to delete inherited Zenodo file {fname}: "
                        f"{dr.status_code} {dr.text[:200]}"
                    )
    else:
        # First-time deposit
        logger.info("Creating new Zenodo deposit")
        r = requests.post(
            f"{ZENODO_BASE}/deposit/depositions",
            json={},
            headers=json_header,
            timeout=30,
        )
        r.raise_for_status()
        dep_id = r.json()["id"]
        bucket_url = r.json()["links"]["bucket"]

    # Upload each file via bucket PUT API
    for filename, content in files.items():
        logger.info("Uploading %s to Zenodo bucket", filename)
        r = requests.put(
            f"{bucket_url}/{filename}",
            data=content.encode("utf-8") if isinstance(content, str) else content,
            headers=auth_header,
            timeout=120,
        )
        r.raise_for_status()

    # Set metadata
    r = requests.put(
        f"{ZENODO_BASE}/deposit/depositions/{dep_id}",
        data=json.dumps({"metadata": metadata}),
        headers=json_header,
        timeout=30,
    )
    r.raise_for_status()

    # Publish
    logger.info("Publishing Zenodo deposit %s", dep_id)
    r = requests.post(
        f"{ZENODO_BASE}/deposit/depositions/{dep_id}/actions/publish",
        headers=auth_header,
        timeout=60,
    )
    r.raise_for_status()
    result = r.json()
    doi = result["doi"]
    concept_doi = result.get("conceptdoi", doi)
    logger.info("Published DOI: %s (concept: %s)", doi, concept_doi)
    return {"doi": doi, "deposition_id": result["id"], "concept_doi": concept_doi}


def _build_zenodo_metadata(published_at: str = None) -> dict:
    """Back-compat shim — prefer `zenodo_assembly.build_metadata` directly.

    The full v3 metadata builder (which can embed an upstream-snapshot
    summary into the description when a source-versions manifest is
    available) lives in `zenodo_assembly.build_metadata`. This shim is
    kept for any third-party caller still importing the underscore-prefixed
    name; it forwards to `build_metadata` without the source_versions
    block.
    """
    from src.exporters.zenodo_assembly import build_metadata
    pub_date = published_at or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return build_metadata(pub_date)
