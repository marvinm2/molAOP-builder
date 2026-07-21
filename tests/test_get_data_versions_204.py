"""
`/get_data_versions` returned an empty object in production (#204).

Two defects compounded:

1. The route sent ``Accept: application/json``. Both the AOP-Wiki and
   WikiPathways SPARQL endpoints answer that with **HTTP 406 Not Acceptable** —
   they serve ``application/sparql-results+json``. Verified against both live
   endpoints.
2. The response handling was ``if status_code == 200: <set key>`` with no
   ``else``. A non-200 therefore set no key and raised nothing, so the route
   returned ``{}`` with a 200 status and no log line. The frontend
   (`static/js/main.js`) silently rendered nothing.

The combination meant a total outage of this endpoint looked identical to
"no data yet". These tests pin the header and the non-silence.
"""

import pytest


def _versions_source():
    import inspect

    from src.blueprints import api

    return inspect.getsource(api.get_data_versions)


def test_requests_sparql_results_json_not_plain_json():
    """application/json gets a 406 from both endpoints."""
    source = _versions_source()

    assert "application/sparql-results+json" in source, (
        "SPARQL endpoints require Accept: application/sparql-results+json"
    )
    assert '"Accept": "application/json"' not in source, (
        "Accept: application/json returns HTTP 406 from both AOP-Wiki and "
        "WikiPathways; it must not be reintroduced"
    )


def test_non_200_is_not_swallowed():
    """A failed upstream call must not silently vanish from the response."""
    source = _versions_source()

    assert "status_code != 200" in source, (
        "route must branch on non-200 explicitly; the original code only "
        "handled == 200, so a 406 produced a key-less, log-less empty result"
    )


@pytest.mark.parametrize("resource", ["aop_wiki", "wikipathways"])
def test_upstream_failure_still_reports_the_resource(monkeypatch, client, resource):
    """Every resource appears in the payload even when its fetch fails.

    The point of the fix is that a caller can distinguish "fetch failed" from
    "resource not covered". Before, both looked like an absent key.
    """
    import requests as _requests

    from src.blueprints import api

    class _Resp:
        status_code = 406
        text = "Not Acceptable"

        def json(self):  # pragma: no cover - should not be reached on 406
            raise ValueError("no json on a 406")

    monkeypatch.setattr(api.requests, "post", lambda *a, **kw: _Resp())

    resp = client.get("/get_data_versions")

    assert resp.status_code == 200
    body = resp.get_json()
    assert resource in body, (
        f"{resource} missing entirely from the payload on upstream failure — "
        "this is the silent-drop bug"
    )
    # And it must be marked as failed rather than looking like real data.
    assert body[resource].get("version") in ("Unknown", "unavailable", None) or (
        "Error" in str(body[resource].get("comment", ""))
    )


def test_all_resources_present_on_success(monkeypatch, client):
    """A healthy fetch reports both resources."""
    from src.blueprints import api

    class _Resp:
        status_code = 200

        def json(self):
            return {"results": {"bindings": [{}]}}

    monkeypatch.setattr(api.requests, "post", lambda *a, **kw: _Resp())

    body = client.get("/get_data_versions").get_json()

    assert "aop_wiki" in body
    assert "wikipathways" in body
