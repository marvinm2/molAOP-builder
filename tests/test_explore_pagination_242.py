"""#242 — Explore must reach every mapping, not just the first API page.

Both Explore tables requested a single page of `per_page=200` and discarded
`json.pagination`. The API clamps `per_page` at 200, so once the WikiPathways
corpus passed that the table silently held only the first 200 rows — sorted by
last-updated, so the hidden remainder was the *least* recently touched rows.

That is a curation gap rather than a display limit: "Propose Change" is bound to
a row present in the table, and it is the only route to revising or deleting an
approved mapping. A row Explore does not show cannot be corrected, re-tiered or
retired through the interface at all.

The failure mode is that everything looks correct below the threshold and breaks
quietly above it, so the tests below deliberately cross it: the API half seeds
`MAX_PER_PAGE + 1` mappings and walks the pages, and the client half asserts the
table follows `pagination.total_pages` instead of stopping at page 1.
"""
import os
import tempfile

import pytest

from app import app as flask_app
import src.blueprints.v1_api as v1_mod
from src.core.models import CacheModel, Database, GoMappingModel, MappingModel

MAX_PER_PAGE = 200

HERE = os.path.dirname(__file__)
EXPLORE_HTML = os.path.join(HERE, "..", "templates", "explore.html")


def _read_explore():
    with open(EXPLORE_HTML, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def v1_client():
    """Test client with the v1 blueprint wired to a fresh temp-file DB."""
    fd, db_path = tempfile.mkstemp()

    db = Database(db_path)
    mm = MappingModel(db)
    gm = GoMappingModel(db)
    cm = CacheModel(db)

    orig_mm = v1_mod.mapping_model
    orig_gm = v1_mod.go_mapping_model
    orig_cm = v1_mod.cache_model

    v1_mod.set_models(mm, gm, cm)

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as test_client:
        with flask_app.app_context():
            yield test_client, mm

    v1_mod.set_models(orig_mm, orig_gm, orig_cm)

    os.close(fd)
    os.unlink(db_path)


def _seed_approved(mm, ke_id, wp_id):
    mapping_id = mm.create_mapping(
        ke_id=ke_id,
        ke_title=f"Test KE {ke_id}",
        wp_id=wp_id,
        wp_title=f"Test Pathway {wp_id}",
        confidence_level="High",
        created_by="github:test_curator",
    )
    conn = mm.db.get_connection()
    try:
        conn.execute(
            "UPDATE mappings SET approved_by_curator=?, approved_at_curator=? WHERE id=?",
            ("test_curator", "2026-01-01T00:00:00", mapping_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_walking_pagination_reaches_every_mapping_past_the_cap(v1_client):
    """Seed one more mapping than a single page can hold and collect them all.

    Pins the API contract Explore now depends on: `total_pages` is honest, and
    following it yields every row exactly once.
    """
    client, mm = v1_client

    expected = set()
    for i in range(MAX_PER_PAGE + 1):
        ke_id, wp_id = f"KE {1000 + i}", f"WP{1000 + i}"
        _seed_approved(mm, ke_id, wp_id)
        expected.add((ke_id, wp_id))

    seen = []
    page = 1
    while True:
        resp = client.get(f"/api/v1/mappings?page={page}&per_page={MAX_PER_PAGE}")
        assert resp.status_code == 200
        body = resp.get_json()
        seen.extend((r["ke_id"], r["pathway_id"]) for r in body["data"])
        pagination = body["pagination"]
        assert pagination["total"] == MAX_PER_PAGE + 1
        if page >= pagination["total_pages"]:
            break
        page += 1

    assert page == 2, "one row past the cap must produce a second page"
    assert len(seen) == len(set(seen)), "a row must not appear on two pages"
    assert set(seen) == expected


def test_single_page_request_is_short_of_the_total(v1_client):
    """The original behaviour, pinned as the thing not to go back to.

    A lone `page=1&per_page=200` returns fewer rows than exist while reporting
    the true total — which is exactly why discarding `pagination` lost rows
    without anything looking wrong.
    """
    client, mm = v1_client

    for i in range(MAX_PER_PAGE + 1):
        _seed_approved(mm, f"KE {2000 + i}", f"WP{2000 + i}")

    body = client.get(f"/api/v1/mappings?page=1&per_page={MAX_PER_PAGE}").get_json()

    assert len(body["data"]) == MAX_PER_PAGE
    assert body["pagination"]["total"] == MAX_PER_PAGE + 1
    assert body["pagination"]["next"] is not None


def test_per_page_is_clamped_so_a_bigger_request_cannot_substitute(v1_client):
    """Asking for more than the cap does not raise it.

    Rules out "just request per_page=100000" as a fix, which would look like it
    worked against a small corpus.
    """
    client, mm = v1_client

    for i in range(MAX_PER_PAGE + 1):
        _seed_approved(mm, f"KE {3000 + i}", f"WP{3000 + i}")

    body = client.get("/api/v1/mappings?page=1&per_page=100000").get_json()

    assert body["pagination"]["per_page"] == MAX_PER_PAGE
    assert len(body["data"]) == MAX_PER_PAGE


def test_explore_tables_do_not_request_a_single_hardcoded_page():
    """Neither table may pin `page` to 1 in its request parameters."""
    body = _read_explore()

    assert "page: 1, per_page: 200" not in body, (
        "Explore must not request one hardcoded page — that is the #242 bug"
    )


def test_explore_tables_follow_the_pagination_envelope():
    """Both tables route through the paging helper, and it honours total_pages."""
    body = _read_explore()

    assert body.count("fetchAllMappings(") >= 3, (
        "expected the helper definition plus a call from the WP and Reactome tables"
    )
    assert "'/api/v1/mappings'" in body and "'/api/v1/reactome-mappings'" in body, (
        "both tables must go through the paging helper — Reactome has the same "
        "bug and is only masked by currently holding few rows"
    )
    assert "pagination.total_pages" in body, (
        "the helper must keep fetching while pages remain"
    )


def test_explore_warns_when_a_table_is_short():
    """A table holding fewer rows than the API reports must say so.

    A row count that quietly disagrees with the database is what made this
    dangerous rather than merely inconvenient.
    """
    body = _read_explore()

    assert "showTruncationNotice(" in body
    assert 'id="wp-truncation-notice"' in body
    assert 'id="reactome-truncation-notice"' in body
