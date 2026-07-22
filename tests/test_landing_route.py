"""
Tests for the landing/mapper route refactor and Reactome stats extension (Task 2).

Covers:
  1. GET / returns 200 and the response HTML contains the four headline counts
     (KE-WP, KE-GO, KE-Reactome, total) as data-target attributes — no JS needed.
  2. GET /mapper returns 200 and renders the mapper (index.html) content.
  3. get_mapping_stats() returns reactome_total and reactome_by_confidence keys,
     and total == wp_total + go_total + reactome_total.
  4. get_mapping_stats() does not raise when reactome_mapping_model is None (graceful zero).
"""
import pytest


@pytest.fixture
def client(tmp_path):
    """Minimal test client that creates a fresh in-memory DB per test."""
    import os
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
    os.environ.setdefault("GITHUB_CLIENT_ID", "dummy")
    os.environ.setdefault("GITHUB_CLIENT_SECRET", "dummy")

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as c:
        with app.app_context():
            yield c


# ---------------------------------------------------------------------------
# Test 1: GET / renders landing page with all four data-target counts
# ---------------------------------------------------------------------------

def test_landing_route_returns_200_with_counts(client):
    """GET / returns 200 and HTML contains data-target attributes for all four counts."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "data-target" in html, "Landing page must have data-target attributes for count-up animation"
    # The landing page must be server-rendered with the four count card values


@pytest.fixture
def seeded_client():
    """Client wired to a fresh DB holding a known, asymmetric set of mappings.

    Both #211 regression tests need non-zero counts: with an empty database
    every card legitimately reads 0, and an assertion that the rendered text
    equals data-target passes even against the literal "0" the bug produced.
    Asymmetric counts (4/3/2) also stop a helper that confused the three tables
    from passing by accident.

    Yields (client, stats_module, expected_counts).
    """
    import os
    import tempfile

    from app import app as flask_app
    import src.blueprints.main as main_mod
    import src.blueprints.v1_api as v1_mod
    from src.core.models import (
        CacheModel,
        Database,
        GoMappingModel,
        MappingModel,
        ReactomeMappingModel,
    )

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    mm, gm, rm, cm = (
        MappingModel(db),
        GoMappingModel(db),
        ReactomeMappingModel(db),
        CacheModel(db),
    )

    for i in range(4):
        mm.create_mapping(f"KE {i}", f"Title {i}", f"WP{i}", f"Pathway {i}",
                          confidence_level="high", created_by="github:tester")
    for i in range(3):
        gm.create_mapping(f"KE {i}", f"Title {i}", f"GO:000000{i}", f"Term {i}",
                          confidence_level="medium", created_by="github:tester")
    for i in range(2):
        rm.create_mapping(f"KE {i}", f"Title {i}", f"R-HSA-{i}", f"Reactome {i}",
                          created_by="github:tester")

    orig_main = (main_mod.mapping_model, main_mod.go_mapping_model,
                 main_mod.reactome_mapping_model)
    orig_v1 = (v1_mod.mapping_model, v1_mod.go_mapping_model, v1_mod.cache_model,
               v1_mod.reactome_mapping_model)

    main_mod.mapping_model = mm
    main_mod.go_mapping_model = gm
    main_mod.reactome_mapping_model = rm
    v1_mod.set_models(mm, gm, cm, reactome_mapping=rm)

    flask_app.config["TESTING"] = True
    try:
        with flask_app.test_client() as c:
            with flask_app.app_context():
                yield c, main_mod, {"wp": 4, "go": 3, "reactome": 2, "total": 9}
    finally:
        (main_mod.mapping_model, main_mod.go_mapping_model,
         main_mod.reactome_mapping_model) = orig_main
        v1_mod.set_models(orig_v1[0], orig_v1[1], orig_v1[2], reactome_mapping=orig_v1[3])
        os.close(fd)
        os.unlink(db_path)


def test_landing_cards_render_values_as_text_not_zero(seeded_client):
    """The served HTML must carry the real numbers as element text, not just in
    data-target.

    Regression guard for #211. The cards previously rendered a literal "0" and
    relied entirely on a 1200ms JS count-up to fill them in, so any capture
    before the animation finished — a screenshot, a throttled background tab, a
    print render — reported a number far below the truth, and with JS disabled
    they read "0" forever. The reported "35 / 3 / 2 / 40" was exactly this: a
    snapshot ~125ms into the curve of a 125/10/6/141 database.

    Note the sibling test above asserts only that the substring "data-target"
    appears, which was true while the bug was fully present.
    """
    import re

    client, main_mod, expected = seeded_client
    stats = main_mod.get_mapping_stats()
    html = client.get("/").get_data(as_text=True)

    rendered = re.findall(
        r'<div class="stat-card__value" data-target="(\d+)">([\d,]+)</div>', html
    )
    assert len(rendered) == 4, f"Expected four stat cards, found {len(rendered)}"

    for target, text in rendered:
        assert int(text.replace(",", "")) == int(target), (
            f"Card text {text!r} must equal its data-target {target!r} — a "
            "literal 0 here is the #211 bug"
        )

    assert [int(t) for t, _ in rendered] == [
        stats["wp_total"], stats["go_total"], stats["reactome_total"], stats["total"],
    ] == [expected["wp"], expected["go"], expected["reactome"], expected["total"]]


def test_landing_cards_agree_with_public_api(seeded_client):
    """The stat cards and /api/v1 must report the same totals.

    This is the drift guard #211 actually asks for. The two paths used to run
    independent raw SQL over the same tables and agreed only by coincidence;
    they now share MappingCountsMixin._count. Asserted as equality between
    paths, never against literals — the live counts move (GO went 10 -> 11
    during the investigation of this very issue).
    """
    client, main_mod, _ = seeded_client
    stats = main_mod.get_mapping_stats()

    for endpoint, key in (
        ("/api/v1/mappings", "wp_total"),
        ("/api/v1/go-mappings", "go_total"),
        ("/api/v1/reactome-mappings", "reactome_total"),
    ):
        resp = client.get(f"{endpoint}?per_page=1")
        assert resp.status_code == 200, endpoint
        assert resp.get_json()["pagination"]["total"] == stats[key], (
            f"{endpoint} disagrees with the landing card for {key}"
        )


# ---------------------------------------------------------------------------
# Test 2: GET /mapper returns 200 and renders the mapper content (index.html)
# ---------------------------------------------------------------------------

def test_mapper_route_returns_200(client):
    """GET /mapper returns 200 and renders mapper view."""
    resp = client.get("/mapper")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # The mapper page should have mapper-specific content
    # (checking for something that the index.html template produces)
    assert len(html) > 100, "Mapper page should have content"


# ---------------------------------------------------------------------------
# Test 3: get_mapping_stats() includes Reactome keys; total = sum of all three
# ---------------------------------------------------------------------------

def test_get_mapping_stats_includes_reactome():
    """get_mapping_stats() returns reactome_total, reactome_by_confidence; total is sum of all three."""
    import os
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
    os.environ.setdefault("GITHUB_CLIENT_ID", "dummy")
    os.environ.setdefault("GITHUB_CLIENT_SECRET", "dummy")

    from app import create_app
    app = create_app()
    with app.app_context():
        from src.blueprints.main import get_mapping_stats
        stats = get_mapping_stats()

        assert "reactome_total" in stats, "get_mapping_stats() must return reactome_total"
        assert "reactome_by_confidence" in stats, "get_mapping_stats() must return reactome_by_confidence"
        assert "wp_total" in stats
        assert "go_total" in stats
        assert "total" in stats

        expected_total = stats["wp_total"] + stats["go_total"] + stats["reactome_total"]
        assert stats["total"] == expected_total, (
            f"total ({stats['total']}) must equal wp_total + go_total + reactome_total "
            f"({stats['wp_total']} + {stats['go_total']} + {stats['reactome_total']} = {expected_total})"
        )


# ---------------------------------------------------------------------------
# Test 4: get_mapping_stats() is graceful when reactome_mapping_model is None
# ---------------------------------------------------------------------------

def test_get_mapping_stats_graceful_when_no_reactome():
    """get_mapping_stats() does not raise when reactome_mapping_model is None; reactome_total=0."""
    import os
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
    os.environ.setdefault("GITHUB_CLIENT_ID", "dummy")
    os.environ.setdefault("GITHUB_CLIENT_SECRET", "dummy")

    from app import create_app
    app = create_app()
    with app.app_context():
        import src.blueprints.main as main_mod
        original = main_mod.reactome_mapping_model
        try:
            main_mod.reactome_mapping_model = None
            from src.blueprints.main import get_mapping_stats
            stats = get_mapping_stats()  # must not raise
            assert stats["reactome_total"] == 0
            assert isinstance(stats["reactome_by_confidence"], dict)
        finally:
            main_mod.reactome_mapping_model = original
