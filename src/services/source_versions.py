"""
SourceVersionService — live upstream release-version fetch with 24 h in-process TTL.

Reuses the four capture_* functions from scripts/capture_source_versions.py (no
re-implementation of upstream fetches). Exposes a single public function:

    snapshot() -> dict
        Returns a per-resource dict:
        {
          "wikipathways": {"version": "2026-05-10", "unavailable": False},
          "reactome":     {"version": "v96",         "unavailable": False},
          "gene_ontology":{"version": "2026-03-25",  "unavailable": False},
          "aopwiki":      {"version": "2026-04-01",  "unavailable": False},
        }
        Each entry that could not be fetched has unavailable=True,
        version="unavailable". snapshot() NEVER raises.

No external caching libraries — module-level dict with time.time() TTL check.
"""
from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache (Pattern 2 — no cachetools, no Redis)
# ---------------------------------------------------------------------------

_CACHE: dict = {}        # {"snapshot": {...}, "fetched_at": float}
_TTL: int = 86400        # 24 hours in seconds

# ---------------------------------------------------------------------------
# Internal normalisation helpers
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalise_date(raw: str) -> str:
    """Return raw if already ISO YYYY-MM-DD; extract ISO portion otherwise."""
    if _ISO_DATE_RE.match(raw or ""):
        return raw
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw or "")
    return m.group(1) if m else raw


def _reactome_version(raw: str) -> str:
    """Prefix bare integer release numbers with 'v'; leave other strings as-is."""
    s = str(raw).strip()
    if s.isdigit():
        return f"v{s}"
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snapshot() -> dict:
    """
    Return per-resource version/unavailable dict, cached for 24 h.

    Never raises — each failed fetch produces an unavailable entry.
    """
    now = time.time()

    # Cache hit within TTL
    if _CACHE and now - _CACHE.get("fetched_at", 0) < _TTL:
        return _CACHE["snapshot"]

    # Lazy import here so the scripts package doesn't need to be on the Python
    # path at module import time (tests can monkeypatch before snapshot() runs).
    from scripts.capture_source_versions import (
        capture_wikipathways,
        capture_gene_ontology_upstream,
        capture_reactome,
        capture_aopwiki,
    )

    fetchers = {
        "wikipathways": (capture_wikipathways, _extract_wp),
        # Upstream, not the local OBO: the local file is what the corpus was
        # built from, so comparing it with itself never shows a new release.
        "gene_ontology": (capture_gene_ontology_upstream, _extract_go),
        "reactome":      (capture_reactome,      _extract_reactome),
        "aopwiki":       (capture_aopwiki,        _extract_aopwiki),
    }

    result: dict[str, dict] = {}
    for key, (fn, extractor) in fetchers.items():
        try:
            raw = fn()
            if raw.get("status") == "ok":
                result[key] = {"version": extractor(raw), "unavailable": False}
            else:
                logger.warning("source_versions: %s returned non-ok: %s", key, raw.get("reason", ""))
                result[key] = {"version": "unavailable", "unavailable": True}
        except Exception as exc:
            logger.warning("source_versions: %s fetch raised: %s", key, exc)
            result[key] = {"version": "unavailable", "unavailable": True}

    _CACHE["snapshot"] = result
    _CACHE["fetched_at"] = time.time()
    return result


# ---------------------------------------------------------------------------
# Per-resource version extractors
# ---------------------------------------------------------------------------

def _extract_wp(raw: dict) -> str:
    return _normalise_date(raw.get("release_date", ""))


def _extract_go(raw: dict) -> str:
    return _normalise_date(raw.get("release_date", ""))


def _extract_reactome(raw: dict) -> str:
    return _reactome_version(raw.get("release_version", ""))


def _extract_aopwiki(raw: dict) -> str:
    # AOP-Wiki returns snapshot_date (not release_date)
    date = raw.get("snapshot_date") or raw.get("release_date", "")
    return _normalise_date(date)
