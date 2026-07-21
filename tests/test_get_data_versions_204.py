"""
`/get_data_versions` — consolidated onto SourceVersionService (#204).

History: the route issued its own SPARQL against AOP-Wiki and WikiPathways,
duplicating what `src/services/source_versions.py` already did for the footer
badges. That duplicate drifted and broke silently in two ways at once —

1. It sent ``Accept: application/json``. Both SPARQL endpoints answer that with
   **HTTP 406**; they serve ``application/sparql-results+json``.
2. Its handler was ``if status_code == 200:`` with no ``else``, so a non-200 set
   no key and raised nothing. The route returned ``{}`` with a 200 status and no
   log line, making a total upstream failure look identical to "no data".

The route now delegates to `source_versions.snapshot()`. These tests pin the
properties that made the old version fail invisibly: every resource is always
represented, failures are labelled rather than dropped, and no second copy of
the upstream fetch logic creeps back in.
"""

import pytest

RESOURCES = ["wikipathways", "gene_ontology", "reactome", "aopwiki"]


@pytest.fixture
def stub_snapshot(monkeypatch):
    """Replace SourceVersionService.snapshot with a controllable stub."""

    def _install(payload):
        from src.blueprints import api

        monkeypatch.setattr(api.source_versions, "snapshot", lambda: payload)

    return _install


def test_reports_all_four_resources(client, stub_snapshot):
    """Coverage went from two resources to four; GO and Reactome were never here."""
    stub_snapshot(
        {
            "wikipathways": {"version": "2026-07-10", "unavailable": False},
            "gene_ontology": {"version": "2026-06-15", "unavailable": False},
            "reactome": {"version": "v97", "unavailable": False},
            "aopwiki": {"version": "2026-07-17", "unavailable": False},
        }
    )

    body = client.get("/get_data_versions").get_json()

    assert sorted(body) == sorted(RESOURCES)
    assert body["wikipathways"]["version"] == "2026-07-10"
    assert body["reactome"]["version"] == "v97"
    assert body["wikipathways"]["unavailable"] is False
    assert body["wikipathways"]["source"] == "WikiPathways"


def test_failed_resource_is_labelled_not_dropped(client, stub_snapshot):
    """The core regression: a failure must stay visible in the payload.

    Previously an upstream failure removed the key entirely, so a caller could
    not tell "fetch failed" from "resource not covered".
    """
    stub_snapshot(
        {
            "wikipathways": {"version": "unavailable", "unavailable": True},
            "gene_ontology": {"version": "2026-06-15", "unavailable": False},
            "reactome": {"version": "v97", "unavailable": False},
            "aopwiki": {"version": "2026-07-17", "unavailable": False},
        }
    )

    body = client.get("/get_data_versions").get_json()

    assert "wikipathways" in body, "failed resource was dropped from the payload"
    assert body["wikipathways"]["unavailable"] is True
    assert body["gene_ontology"]["unavailable"] is False


def test_empty_snapshot_still_lists_every_resource(client, stub_snapshot):
    """Even a total service failure must not return `{}` with a 200."""
    stub_snapshot({})

    resp = client.get("/get_data_versions")
    body = resp.get_json()

    assert resp.status_code == 200
    assert sorted(body) == sorted(RESOURCES)
    assert all(body[r]["unavailable"] is True for r in RESOURCES)


def test_snapshot_raising_returns_500_not_empty_body(client, monkeypatch):
    """snapshot() is documented never to raise; a breach is a real error.

    The old code's failure mode was a 200 with an empty body. If the service
    contract is ever violated we want a 500, not silence.
    """
    from src.blueprints import api

    def boom():
        raise RuntimeError("contract violated")

    monkeypatch.setattr(api.source_versions, "snapshot", boom)

    resp = client.get("/get_data_versions")

    assert resp.status_code == 500
    assert "error" in resp.get_json()


def test_route_does_not_reimplement_upstream_fetches():
    """Guard against a second copy of the SPARQL logic returning.

    The duplication is what allowed the two implementations to drift apart
    until one of them was silently dead.
    """
    import inspect

    from src.blueprints import api

    source = inspect.getsource(api.get_data_versions)

    assert "requests.post" not in source, (
        "route must delegate to source_versions, not issue its own HTTP calls"
    )
    assert "sparql" not in source.lower() or "SPARQL" in inspect.getdoc(
        api.get_data_versions
    ), "route should no longer build SPARQL queries itself"
    assert "source_versions.snapshot" in source
