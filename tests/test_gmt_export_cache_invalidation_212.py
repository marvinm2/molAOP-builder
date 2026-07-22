"""GMT export cache invalidation (#212).

The cache under static/exports/ was keyed on the calendar date and written
once: ``KE-{TYPE}_{YYYY-MM-DD}_{tier}.gmt``, regenerated only when the file was
absent. Any change to mapping state — approval, rejection, admin edit,
deletion, accepted change proposal — was therefore invisible in the GMT for the
rest of the day, while /api/v1/*-mappings showed it immediately.

Every test here drives the public download routes and asserts on the served
bytes, because that is the surface the molAOP Analyser consumes. They all pass
trivially on a cold cache, so each one performs a first request *before* the
mutation to make sure a stale file actually exists to be invalidated.
"""
import os
import tempfile

import pytest

import src.blueprints.main as main_bp_mod
from app import app as flask_app
from src.core.models import Database, GoMappingModel, ReactomeMappingModel


GO_GENES = {
    "GO:0006974": ["TP53", "BRCA1"],
    "GO:0006954": ["IL6", "TNF"],
    "GO:0072593": ["SOD1"],
}

REACTOME_GENES = {
    "R-HSA-100": ["TP53", "MDM2"],
    "R-HSA-200": ["BRCA1"],
}


def _insert_go(model, ke_id, ke_title, go_id, go_name, confidence="high",
               updated_at="2026-01-01 00:00:00"):
    conn = model.db.get_connection()
    try:
        conn.execute(
            "INSERT INTO ke_go_mappings (ke_id, ke_title, go_id, go_name, "
            "connection_type, confidence_level, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'describes', ?, ?, ?)",
            (ke_id, ke_title, go_id, go_name, confidence, updated_at, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_reactome(model, uuid, ke_id, ke_title, reactome_id, pathway_name,
                     confidence="high", updated_at="2026-01-01 00:00:00"):
    conn = model.db.get_connection()
    try:
        conn.execute(
            "INSERT INTO ke_reactome_mappings (uuid, ke_id, ke_title, reactome_id, "
            "pathway_name, species, confidence_level, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'Homo sapiens', ?, ?, ?)",
            (uuid, ke_id, ke_title, reactome_id, pathway_name, confidence,
             updated_at, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def _exec(model, sql, params=()):
    conn = model.db.get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def gmt_env(tmp_path, monkeypatch):
    """Isolated export cache directory + GO/Reactome models on a temp DB."""
    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    go_model = GoMappingModel(db)
    rx_model = ReactomeMappingModel(db)

    cache_dir = tmp_path / "exports"
    cache_dir.mkdir()
    monkeypatch.setattr(main_bp_mod, "EXPORT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(main_bp_mod, "go_mapping_model", go_model)
    monkeypatch.setattr(main_bp_mod, "reactome_mapping_model", rx_model)

    from src.exporters import gmt_exporter
    monkeypatch.setattr(
        gmt_exporter, "_load_go_annotations_merged",
        lambda bp_path=None, mf_path=None: GO_GENES,
    )
    monkeypatch.setattr(
        gmt_exporter, "_load_reactome_annotations", lambda path=None: REACTOME_GENES,
    )

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    with flask_app.test_client() as client:
        with flask_app.app_context():
            yield {
                "client": client, "go": go_model, "rx": rx_model,
                "cache_dir": cache_dir,
            }

    os.close(fd)
    os.unlink(db_path)


def _body(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The reported defect: a mapping approved after the first download
# ---------------------------------------------------------------------------

def test_go_gmt_shows_mapping_approved_after_first_download(gmt_env):
    """The exact #212 reproduction, with KE 149 -> GO:0006954 as reported."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")

    before = _body(client, "/exports/gmt/ke-go")
    assert "GO:0006974" in before
    assert "GO:0006954" not in before

    _insert_go(go, "KE 149", "Increase, Inflammation", "GO:0006954", "inflammatory response")

    after = _body(client, "/exports/gmt/ke-go")
    assert "GO:0006954" in after, "newly approved mapping missing from the GMT"
    assert "IL6" in after
    assert "GO:0006974" in after


def test_reactome_gmt_shows_mapping_approved_after_first_download(gmt_env):
    client, rx = gmt_env["client"], gmt_env["rx"]
    _insert_reactome(rx, "u1", "KE 1", "Apop", "R-HSA-100", "p53")

    before = _body(client, "/exports/gmt/ke-reactome")
    assert "R-HSA-100" in before
    assert "R-HSA-200" not in before

    _insert_reactome(rx, "u2", "KE 5", "Cell", "R-HSA-200", "DNA")

    after = _body(client, "/exports/gmt/ke-reactome")
    assert "R-HSA-200" in after


def test_ke_centric_gmt_is_invalidated_too(gmt_env):
    """The KE-centric variants share the cache and were stale in the same way."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")

    before = _body(client, "/exports/gmt/ke-go-centric")
    assert "TNF" not in before

    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006954", "inflammatory response")

    after = _body(client, "/exports/gmt/ke-go-centric")
    assert "TNF" in after


# ---------------------------------------------------------------------------
# Every other write path that can change mapping state
# ---------------------------------------------------------------------------

def test_deleted_mapping_disappears_from_gmt(gmt_env):
    """Deletion, and by extension an approved 'propose deletion' change request."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _insert_go(go, "KE 149", "Increase, Inflammation", "GO:0006954", "inflammatory response")

    assert "GO:0006954" in _body(client, "/exports/gmt/ke-go")

    _exec(go, "DELETE FROM ke_go_mappings WHERE go_id = ?", ("GO:0006954",))

    after = _body(client, "/exports/gmt/ke-go")
    assert "GO:0006954" not in after
    assert "GO:0006974" in after


def test_in_place_edit_is_reflected(gmt_env):
    """An admin edit changes no row count — only updated_at moves."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1115", "Increase, ROS", "GO:0072593", "superoxide metabolic process",
               confidence="high")

    before = _body(client, "/exports/gmt/ke-go?min_confidence=high")
    assert "GO:0072593" in before

    _exec(
        go,
        "UPDATE ke_go_mappings SET confidence_level = 'low', "
        "updated_at = '2026-02-02 12:00:00' WHERE go_id = ?",
        ("GO:0072593",),
    )

    resp = client.get("/exports/gmt/ke-go?min_confidence=high")
    # Nothing survives the high threshold any more, so the route 503s rather
    # than serving the pre-edit file.
    assert resp.status_code == 503
    # ...while the row itself is still there, one tier down.
    assert "GO:0072593" in _body(client, "/exports/gmt/ke-go?min_confidence=low")


def test_rejection_leaves_the_export_untouched(gmt_env):
    """A rejected proposal never reaches ke_go_mappings, so the GMT must not move.

    Guards the other direction: fingerprinting the mapping table (not the
    proposal queue) is what makes rejection a no-op here.
    """
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")

    before = _body(client, "/exports/gmt/ke-go")
    _exec(
        go,
        "INSERT INTO ke_go_proposals (user_name, user_email, user_affiliation, "
        "status, ke_id, go_id) VALUES ('t', 't@e', 'x', 'rejected', 'KE 149', 'GO:0006954')",
    )
    assert _body(client, "/exports/gmt/ke-go") == before


# ---------------------------------------------------------------------------
# The cache must still be a cache
# ---------------------------------------------------------------------------

def test_unchanged_data_is_served_from_cache(gmt_env, monkeypatch):
    """No regeneration when nothing moved — this is a public download path."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _body(client, "/exports/gmt/ke-go")

    from src.exporters import gmt_exporter
    calls = []
    real = gmt_exporter.generate_ke_go_gmt

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(gmt_exporter, "generate_ke_go_gmt", counting)

    _body(client, "/exports/gmt/ke-go")
    _body(client, "/exports/gmt/ke-go")
    assert calls == [], "cached GMT was regenerated although the table had not changed"


def test_revision_stamp_is_written_next_to_the_cache_file(gmt_env):
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _body(client, "/exports/gmt/ke-go")

    stamps = list(gmt_env["cache_dir"].glob("KE-GO_*.gmt.rev"))
    assert len(stamps) == 1
    assert stamps[0].read_text().startswith("1:")


def test_missing_model_forces_regeneration_rather_than_a_stale_file(gmt_env, monkeypatch):
    """An unknown revision must mean 'stale', never 'reuse whatever is there'."""
    assert main_bp_mod._gmt_revision("go") is not None

    class _NoRevision:
        def get_all_mappings(self):
            return []

    monkeypatch.setattr(main_bp_mod, "go_mapping_model", _NoRevision())
    assert main_bp_mod._gmt_revision("go") is None


def test_edit_under_a_mixed_timestamp_format_is_still_detected(gmt_env):
    """The live tables hold two updated_at formats, and 'T' sorts after ' '.

    Reproduced on a copy of the production database: ke_go_mappings carries
    both '2026-07-22T14:20:46' (application layer) and '2026-07-22 19:45:08'
    (SQLite CURRENT_TIMESTAMP). A COUNT(*) + MAX(updated_at) fingerprint parks
    on the ISO row and never moves when a CURRENT_TIMESTAMP row is edited
    underneath it, so the export would go on serving the pre-edit file.
    """
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response",
               updated_at="2026-07-22T14:20:46")
    _insert_go(go, "KE 1825", "Increase, Cell death", "GO:0006954", "cell death",
               updated_at="2026-07-22 09:00:00")

    before = _body(client, "/exports/gmt/ke-go")
    assert "cell death" in before

    # Same row count; the new stamp sorts *below* the untouched ISO one.
    _exec(
        go,
        "UPDATE ke_go_mappings SET go_name = 'programmed cell death', "
        "updated_at = '2026-07-22 19:45:08' WHERE go_id = ?",
        ("GO:0006954",),
    )

    after = _body(client, "/exports/gmt/ke-go")
    assert "programmed cell death" in after, "edit hidden by the timestamp format mix"


# ---------------------------------------------------------------------------
# Gene-annotation corpus, the second staleness source
# ---------------------------------------------------------------------------

def test_corpus_change_changes_the_revision(gmt_env, tmp_path, monkeypatch):
    """data/ is a bind mount, so a corpus refresh needs no image rebuild."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    corpus = data_dir / "reactome_gene_annotations.json"
    corpus.write_text('{"R-HSA-100": ["TP53"]}')
    monkeypatch.setattr(main_bp_mod, "_DATA_DIR", data_dir)

    first = main_bp_mod._gmt_revision("reactome")
    corpus.write_text('{"R-HSA-100": ["TP53", "MDM2", "CDKN1A"]}')
    os.utime(corpus, (0, 0))
    assert main_bp_mod._gmt_revision("reactome") != first


def test_absent_corpus_files_do_not_raise(gmt_env, tmp_path, monkeypatch):
    monkeypatch.setattr(main_bp_mod, "_DATA_DIR", tmp_path / "nowhere")
    assert main_bp_mod._gmt_revision("go") is not None
