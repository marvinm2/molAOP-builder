"""
Tests for the /api/preview/<resource>/<format_name> endpoint (Plan 36-05).

Security contract: the endpoint must only open files in PREVIEW_ALLOWLIST.
Path-traversal strings in URL segments must NOT cause files outside the
allowlist to be read — they simply resolve to "available: false".

Mirrors the conftest.py + monkeypatch pattern from test_main_blueprint.py.
"""
import os
import tempfile

import pytest

import src.blueprints.main as main_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_allowlist_key(monkeypatch):
    """Return the first (resource, format_name) key that has an existing file,
    after monkeypatching the allowlist to point at a real temp file."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".gmt", delete=False,
        encoding="utf-8"
    )
    # Write 25 lines so the 20-line cap is exercised
    for i in range(1, 26):
        tmp.write(f"KE_{i}\thttp://aopwiki.org/ke/{i}\tGENE{i}A\tGENE{i}B\n")
    tmp.flush()
    tmp.close()

    fake_allowlist = {
        ("wp", "gmt"): tmp.name,
        ("go", "gmt"): tmp.name,
    }
    monkeypatch.setattr(main_module, "PREVIEW_ALLOWLIST", fake_allowlist)
    return ("wp", "gmt"), tmp.name


class TestDownloadPreview:
    """Endpoint contract tests for /api/preview/<resource>/<format_name>."""

    def test_preview_valid_returns_lines(self, client, monkeypatch):
        """Valid allowlisted (resource, format) → 200, available=True, lines list."""
        (resource, fmt), tmpfile = _first_allowlist_key(monkeypatch)
        try:
            resp = client.get(f"/api/preview/{resource}/{fmt}")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["available"] is True
            assert isinstance(data["lines"], list)
            assert len(data["lines"]) > 0
        finally:
            os.unlink(tmpfile)

    def test_preview_unknown_returns_unavailable(self, client):
        """Unknown (resource, format) not in allowlist → 200, available=False, lines=[]."""
        resp = client.get("/api/preview/bogus/bogus")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is False
        assert data["lines"] == []

    def test_preview_path_traversal_rejected(self, client):
        """Path-traversal segments in URL → available=False; no file outside allowlist opened.

        The URL decoder normalises %2f to '/' in Flask's URL routing, which means
        a literal '..%2f..%2fetc' segment is decoded and Flask's routing layer
        returns 404 before our view runs.  We therefore exercise the traversal
        guard via a dot-segment string that *does* reach the view (e.g. '..foo')
        — if the allowlist look-up is the only gate (as required) it will simply
        return available=False without touching the filesystem.
        """
        # Attempt 1: double-encoded slash — Flask routing returns 404 before view
        resp1 = client.get("/api/preview/..%252f..%252fetc/passwd")
        # Either 404 (routing rejects) or 200 with available=False — both are safe
        assert resp1.status_code in (200, 404)
        if resp1.status_code == 200:
            assert resp1.get_json()["available"] is False

        # Attempt 2: dot-prefixed segment that reaches the view but is not in allowlist
        resp2 = client.get("/api/preview/..foo/..bar")
        assert resp2.status_code == 200
        data = resp2.get_json()
        assert data["available"] is False
        assert data["lines"] == []

    def test_gmt_preview_resolves_through_the_generator(self, client, monkeypatch):
        """Issue #224 — a GMT preview must name the file the download would give.

        The allowlist used to carry literal GMT paths with a hardcoded
        2026-03-04 date. A GMT cache file is named for the day it was written
        and, since #212, for a revision fingerprint of the mapping table too, so
        those names stopped existing the following day and could never be
        recreated. Every request returned a valid 200 carrying
        ``available: false`` — indistinguishable from a preview switched off on
        purpose, which is why it survived so long.

        This asserts the endpoint asks _get_or_generate_gmt rather than a
        constant. It fails on the pre-#224 code, where no call is made at all.
        """
        calls = []

        def _fake_generate(mapping_type, min_confidence=None):
            calls.append(mapping_type)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".gmt", delete=False, encoding="utf-8"
            )
            tmp.write("KE_1\thttp://aopwiki.org/ke/1\tGENEA\tGENEB\n")
            tmp.close()
            return tmp.name, os.path.basename(tmp.name)

        monkeypatch.setattr(main_module, "PREVIEW_ALLOWLIST", {})
        monkeypatch.setattr(main_module, "_get_or_generate_gmt", _fake_generate)

        resp = client.get("/api/preview/wp/gmt")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True, (
            "the GMT preview is unavailable again — a literal path has probably "
            "been reintroduced into PREVIEW_ALLOWLIST"
        )
        assert data["lines"]
        assert calls == ["wp"], (
            "the preview did not resolve through the same generator the "
            "download route uses"
        )

    def test_every_gmt_pair_the_downloads_page_offers_can_resolve(self, client, monkeypatch):
        """No GMT pair may be silently unavailable (#224).

        The three pairs the Downloads page actually requests are (wp, gmt),
        (go, gmt) and (reactome, gmt); the -centric pairs are mapped but not yet
        offered in the UI. All six are asserted here so adding a card to the page
        cannot quietly land on a dead pair.
        """
        seen = []

        def _fake_generate(mapping_type, min_confidence=None):
            seen.append(mapping_type)
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".gmt", delete=False, encoding="utf-8"
            )
            tmp.write("KE_1\thttp://aopwiki.org/ke/1\tGENEA\n")
            tmp.close()
            return tmp.name, os.path.basename(tmp.name)

        monkeypatch.setattr(main_module, "PREVIEW_ALLOWLIST", {})
        monkeypatch.setattr(main_module, "_get_or_generate_gmt", _fake_generate)

        for resource, fmt in main_module.PREVIEW_GMT_TYPES:
            resp = client.get(f"/api/preview/{resource}/{fmt}")
            assert resp.get_json()["available"] is True, f"{resource}/{fmt}"

        assert sorted(seen) == sorted(main_module.PREVIEW_GMT_TYPES.values())

    def test_no_preview_path_carries_a_hardcoded_date(self):
        """The shape of the #224 defect, not just this instance of it.

        A date baked into a path is unrecreatable by construction, so this
        guards the class rather than the five names that happened to carry one.
        """
        import re

        offenders = [
            f"{key}: {path}"
            for key, path in main_module.PREVIEW_ALLOWLIST.items()
            if re.search(r"\d{4}-\d{2}-\d{2}", str(path))
        ]
        assert not offenders, offenders

    def test_generator_failure_degrades_instead_of_500ing(self, client, monkeypatch):
        """A preview is a convenience; it must not break the Downloads page."""
        def _boom(mapping_type, min_confidence=None):
            raise RuntimeError("corpus missing")

        monkeypatch.setattr(main_module, "PREVIEW_ALLOWLIST", {})
        monkeypatch.setattr(main_module, "_get_or_generate_gmt", _boom)

        resp = client.get("/api/preview/wp/gmt")

        assert resp.status_code == 200
        assert resp.get_json() == {"lines": [], "available": False}

    def test_preview_caps_at_20_lines(self, client, monkeypatch):
        """Preview returns at most 20 lines even when the file has more."""
        (resource, fmt), tmpfile = _first_allowlist_key(monkeypatch)
        try:
            resp = client.get(f"/api/preview/{resource}/{fmt}")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["available"] is True
            # File has 25 lines — must be capped at 20
            assert len(data["lines"]) <= 20
        finally:
            os.unlink(tmpfile)
