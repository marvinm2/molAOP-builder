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


def _build_gmt_env(tmp_path, monkeypatch, stub_go_corpus=True):
    """Isolated export cache directory + GO/Reactome models on a temp DB.

    `stub_go_corpus=False` leaves `gmt_exporter._load_go_annotations_merged`
    alone so the real `src.services.go_annotation_index` path is exercised —
    which is the only way to see whether a corpus refresh reaches the served
    bytes rather than only the revision fingerprint.
    """
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
    if stub_go_corpus:
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


@pytest.fixture
def gmt_env(tmp_path, monkeypatch):
    yield from _build_gmt_env(tmp_path, monkeypatch)


@pytest.fixture
def gmt_env_real_go_corpus(tmp_path, monkeypatch):
    """`gmt_env` with the real GO corpus loader, over a temp `data/` directory."""
    import src.services.go_annotation_index as go_index

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(main_bp_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(
        go_index, "DEFAULT_PROPAGATED",
        str(data_dir / "go_{ns}_gene_annotations_propagated.json"),
    )
    monkeypatch.setattr(
        go_index, "DEFAULT_ANNOTATIONS",
        str(data_dir / "go_{ns}_gene_annotations.json"),
    )
    monkeypatch.setattr(
        go_index, "DEFAULT_HIERARCHY", str(data_dir / "go_{ns}_hierarchy.json"),
    )
    go_index.reset_cache()
    try:
        env = _build_gmt_env(tmp_path, monkeypatch, stub_go_corpus=False)
        for item in env:
            item["data_dir"] = data_dir
            yield item
    finally:
        go_index.reset_cache()


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


def _write_json(path, obj):
    import json as _json
    path.write_text(_json.dumps(obj), encoding="utf-8")


def test_go_corpus_refresh_reaches_the_served_bytes(gmt_env_real_go_corpus):
    """A corpus refresh must change the GMT *content*, not only its fingerprint.

    `_gmt_revision` fingerprints the files under data/, but the GO gene sets
    are served from a module-level dict in `src.services.go_annotation_index`
    that is populated on first use and never reloaded. So the fingerprint moved
    while the content did not — and the regeneration then stamped the *new*
    corpus fingerprint onto bytes built from the *old* corpus, after which the
    application would never retry. That is worse than not noticing at all: the
    sidecar asserts a corpus version the file does not contain.

    Reactome has no such cache (`_load_reactome_annotations` reads from disk on
    every call), which is why only the GO arm was affected.
    """
    env = gmt_env_real_go_corpus
    client, go, data_dir = env["client"], env["go"], env["data_dir"]

    bp = data_dir / "go_bp_gene_annotations_propagated.json"
    mf = data_dir / "go_mf_gene_annotations_propagated.json"
    _write_json(bp, {"GO:0006979": ["CAT", "SOD1"]})
    _write_json(mf, {})

    _insert_go(go, "KE 1392", "Increase, Oxidative Stress", "GO:0006979",
               "response to oxidative stress")

    before = _body(client, "/exports/gmt/ke-go")
    assert "SOD1" in before
    assert "SENTINELGENE" not in before

    # Refresh the corpus the way a bind-mount update would.
    _write_json(bp, {"GO:0006979": ["CAT", "SOD1", "SENTINELGENE"]})
    os.utime(bp, (0, 0))

    after = _body(client, "/exports/gmt/ke-go")
    assert "SENTINELGENE" in after, (
        "corpus refresh changed the fingerprint but not the served gene set"
    )

    # ...and the third request must not lose it again.
    assert "SENTINELGENE" in _body(client, "/exports/gmt/ke-go")


def test_go_corpus_refresh_does_not_leave_a_lying_revision_stamp(gmt_env_real_go_corpus):
    """The sidecar must never claim a corpus version the file was not built from."""
    env = gmt_env_real_go_corpus
    client, go, data_dir = env["client"], env["go"], env["data_dir"]

    bp = data_dir / "go_bp_gene_annotations_propagated.json"
    _write_json(bp, {"GO:0006979": ["CAT"]})
    _write_json(data_dir / "go_mf_gene_annotations_propagated.json", {})
    _insert_go(go, "KE 1392", "Increase, Oxidative Stress", "GO:0006979",
               "response to oxidative stress")
    _body(client, "/exports/gmt/ke-go")

    _write_json(bp, {"GO:0006979": ["CAT", "SENTINELGENE"]})
    os.utime(bp, (0, 0))
    resp = client.get("/exports/gmt/ke-go")
    assert resp.status_code == 200

    served = resp.headers["Content-Disposition"].split("filename=")[-1].strip('";')
    gmt = env["cache_dir"] / served
    stamp = env["cache_dir"] / (served + ".rev")
    assert stamp.read_text().strip() == main_bp_mod._gmt_revision("go")
    assert "SENTINELGENE" in gmt.read_text()


def test_unchanged_go_corpus_is_not_reloaded(gmt_env_real_go_corpus, monkeypatch):
    """Self-healing must not turn every request into a corpus re-read."""
    import src.services.go_annotation_index as go_index

    env = gmt_env_real_go_corpus
    client, go, data_dir = env["client"], env["go"], env["data_dir"]
    _write_json(data_dir / "go_bp_gene_annotations_propagated.json", {"GO:0006979": ["CAT"]})
    _write_json(data_dir / "go_mf_gene_annotations_propagated.json", {})
    _insert_go(go, "KE 1392", "Increase, Oxidative Stress", "GO:0006979", "oxidative")
    _body(client, "/exports/gmt/ke-go")

    reads = []
    real_read = go_index._read_json
    monkeypatch.setattr(
        go_index, "_read_json",
        lambda path: (reads.append(path), real_read(path))[1],
    )
    go_index.get_go_annotations_merged()
    go_index.get_go_annotations_merged()
    assert reads == [], "corpus re-read although no file had changed"


# ---------------------------------------------------------------------------
# Provenance: which mapping-table state produced the bytes in front of me
# ---------------------------------------------------------------------------
#
# Before this change the date-stamped filename was, by accident, a content
# identifier: the file was written once per day, so KE-GO_2026-07-22_All.gmt
# named one specific export and every download that day returned it byte for
# byte. Making the cache follow the mapping table is correct but removes that
# property, and a downstream analysis records exactly these filenames as its
# reference state. Two mechanisms replace it — a revision segment in the
# filename and a comment block inside the file — plus an X-Export-Revision
# response header for clients that stream the body and keep neither.


def _served(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    name = resp.headers["Content-Disposition"].split("filename=")[-1].strip('";')
    return resp, name, resp.get_data(as_text=True)


def test_gmt_names_the_revision_that_produced_it(gmt_env):
    from src.exporters.gmt_exporter import export_revision_id

    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")

    resp, name, body = _served(client, "/exports/gmt/ke-go")
    expected = export_revision_id(main_bp_mod._gmt_revision("go"))

    assert f"_r{expected}.gmt" in name
    assert resp.headers["X-Export-Revision"] == expected
    assert f"# export-revision: {expected}" in body
    assert body.startswith("# molAOP Builder GMT export")
    assert "# resource: KE-GO" in body
    assert "# confidence: all tiers" in body


def test_two_mapping_states_never_share_a_filename(gmt_env):
    """The regression this replaces: same name, same day, different content."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _, first_name, first_body = _served(client, "/exports/gmt/ke-go")

    # Re-download with nothing changed: same name, same bytes.
    _, again_name, again_body = _served(client, "/exports/gmt/ke-go")
    assert again_name == first_name
    assert again_body == first_body

    _insert_go(go, "KE 149", "Increase, Inflammation", "GO:0006954", "inflammatory response")
    _, second_name, second_body = _served(client, "/exports/gmt/ke-go")

    assert second_body != first_body
    assert second_name != first_name, "different content served under the same filename"


def test_provenance_block_is_invisible_to_the_analyser_parsers(gmt_env):
    """Both molAOP Analyser GMT parsers must skip the block.

    The predicates below are transcribed from the Analyser's
    ``services/api_service.py`` (``parse_gmt_reference_sets`` and
    ``parse_gmt_pathway_gene_map``): each splits the line on tab and
    ``continue``s on fewer than three fields, before its ID regex is reached.
    Every provenance line is therefore required to be a single tab-free field.
    Confirmed end to end by importing those two functions against a generated
    export; this test pins the property they depend on so the Builder cannot
    drift away from it on its own.
    """
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _, _, body = _served(client, "/exports/gmt/ke-go")

    comments = [l for l in body.splitlines() if l.startswith("#")]
    assert comments, "no provenance block"
    for line in comments:
        assert "\t" not in line
        assert len(line.split("\t")) < 3

    kept = [l for l in body.splitlines() if l.strip() and len(l.split("\t")) >= 3]
    assert kept, "provenance filtering removed the gene sets too"
    assert all(not l.startswith("#") for l in kept)


def test_empty_export_still_answers_503_rather_than_a_header_only_file(gmt_env):
    """A comment block must never make an empty export look like a successful one."""
    client = gmt_env["client"]
    resp = client.get("/exports/gmt/ke-go")
    assert resp.status_code == 503


def test_superseded_revisions_are_pruned_once_they_are_old(gmt_env):
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _, first_name, _ = _served(client, "/exports/gmt/ke-go")

    old = gmt_env["cache_dir"] / first_name
    stale_time = 1
    os.utime(old, (stale_time, stale_time))
    os.utime(gmt_env["cache_dir"] / (first_name + ".rev"), (stale_time, stale_time))

    _insert_go(go, "KE 149", "Increase, Inflammation", "GO:0006954", "inflammatory response")
    _, second_name, _ = _served(client, "/exports/gmt/ke-go")

    assert second_name != first_name
    assert not old.exists(), "superseded export was never cleaned up"
    assert not (gmt_env["cache_dir"] / (first_name + ".rev")).exists()
    assert (gmt_env["cache_dir"] / second_name).exists()


def test_a_just_written_revision_is_not_pruned_under_a_concurrent_reader(gmt_env):
    """Pruning waits an hour, so a file another worker is about to serve stays."""
    client, go = gmt_env["client"], gmt_env["go"]
    _insert_go(go, "KE 1194", "Increase, DNA damage", "GO:0006974", "DNA damage response")
    _, first_name, _ = _served(client, "/exports/gmt/ke-go")

    _insert_go(go, "KE 149", "Increase, Inflammation", "GO:0006954", "inflammatory response")
    _served(client, "/exports/gmt/ke-go")

    assert (gmt_env["cache_dir"] / first_name).exists()
