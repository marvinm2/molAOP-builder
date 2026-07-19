"""
Static-JS smoke tests for the Phase-1 mapper fixes (#194, #195).

These are grep-style assertions against static/js/main.js — they don't execute the
JS, but they pin the presence of the fix so a future refactor can't silently drop it.

- #194: a Reactome success-modal populator exists and is called from the submit
  .done() handler (so the Thank-You modal is no longer stale/blank).
- #195: all three submit handlers key off the backend "session_expired" signal to
  show a session-expiry message rather than the generic failure text.
"""
import os

HERE = os.path.dirname(__file__)
MAIN_JS = os.path.join(HERE, "..", "static", "js", "main.js")


def _read_main_js():
    with open(MAIN_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def test_reactome_success_modal_populator_exists_and_is_called():
    """#194: showReactomeSuccessMessage is defined and invoked before reset."""
    body = _read_main_js()
    assert "showReactomeSuccessMessage(payload)" in body, (
        "showReactomeSuccessMessage(payload) definition missing"
    )
    # It must populate the shared summary element and show the modal.
    assert body.count("#submissionSummary") >= 3, (
        "Reactome success path should populate #submissionSummary like WP/GO"
    )
    # It must be wired into the submit .done() handler.
    assert "this.showReactomeSuccessMessage(payload)" in body, (
        "Reactome submit .done() handler does not call showReactomeSuccessMessage"
    )


def test_all_submit_handlers_detect_session_expired():
    """#195: WP, GO, and Reactome handlers all branch on the session_expired code."""
    body = _read_main_js()
    assert body.count("session_expired") >= 3, (
        "Expected all three submit handlers to detect the 'session_expired' code"
    )
    assert "Your session has expired" in body, (
        "Expected a clear session-expiry message in the mapper JS"
    )
