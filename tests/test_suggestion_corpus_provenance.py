"""A suggestion score must carry the corpus that produced it.

`suggestion_score` is stored on every mapping and #236 treats it as review
evidence — a reviewer who cannot reproduce a curator's ranking cannot use the
score to judge the mapping. But a score is only meaningful against its own
corpus: regenerating the embeddings moves every score at once. Without a
recorded corpus, a reviewer comparing a stored score against a live ranking
cannot tell a genuine disagreement from a rebuild that happened in between.

That matters more now than it did, because the rebuild is about to run on a
schedule rather than by hand.
"""
import json
import os
import tempfile

import pytest

from src.core.models import Database, MappingModel, ProposalModel


@pytest.fixture
def models():
    fd, path = tempfile.mkstemp()
    db = Database(path)
    yield MappingModel(db), ProposalModel(db)
    os.close(fd)
    os.unlink(path)


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    """Point the corpus resolver at a manifest we control."""
    import src.services.corpus_versions as cv

    path = tmp_path / "corpus_manifest.json"
    path.write_text(json.dumps({
        "pathway_title_embeddings.npz": {"built_on": "2026-08-03", "rows": 803},
        "go_bp_embeddings.npz": {"built_on": "2026-07-11", "rows": 24547},
    }))
    monkeypatch.setattr(cv, "MANIFEST_PATH", str(path))
    return cv


# --- resolving a corpus version -------------------------------------------

def test_resource_maps_to_its_own_corpus(manifest):
    assert manifest.corpus_version("wp") == "wp:2026-08-03"
    assert manifest.corpus_version("go") == "go:2026-07-11"


def test_unbuilt_corpus_resolves_to_none_not_a_placeholder(manifest):
    """Reactome is absent from this manifest. None is the honest answer — a
    wrong provenance stamp is worse than an absent one, because an absent one
    is visibly absent."""
    assert manifest.corpus_version("reactome") is None


def test_unknown_resource_resolves_to_none(manifest):
    assert manifest.corpus_version("kegg") is None


def test_missing_manifest_does_not_raise(tmp_path, monkeypatch):
    """A deployment whose artifacts predate the manifest must behave exactly
    as it did before, not fail to submit."""
    import src.services.corpus_versions as cv
    monkeypatch.setattr(cv, "MANIFEST_PATH", str(tmp_path / "absent.json"))
    assert cv.corpus_version("wp") is None


def test_corrupt_manifest_does_not_raise(tmp_path, monkeypatch):
    import src.services.corpus_versions as cv
    bad = tmp_path / "corpus_manifest.json"
    bad.write_text("{not json")
    monkeypatch.setattr(cv, "MANIFEST_PATH", str(bad))
    assert cv.corpus_version("wp") is None


# --- the stamp survives the proposal -> mapping handoff --------------------

def test_corpus_round_trips_from_proposal_to_mapping(models):
    """The handoff at approval is where the score already travels; the corpus
    has to travel with it or the stamp is lost exactly when it starts to
    matter."""
    mm, pm = models
    proposal_id = pm.create_new_pair_proposal(
        ke_id="KE 18", ke_title="Activation, AhR",
        wp_id="WP2873", wp_title="Aryl hydrocarbon receptor pathway",
        connection_type="causative", confidence_level="high",
        provider_username="github:curator", suggestion_score=0.87,
        suggestion_corpus="wp:2026-08-03",
    )
    row = next(p for p in pm.get_all_proposals() if p["id"] == proposal_id)
    assert row["suggestion_score"] == 0.87
    assert row["suggestion_corpus"] == "wp:2026-08-03"

    mapping_id = mm.create_mapping(
        ke_id=row["ke_id"], ke_title=row["ke_title"],
        wp_id=row["wp_id"], wp_title=row["wp_title"],
        created_by="github:curator",
        suggestion_score=row["suggestion_score"],
        suggestion_corpus=row["suggestion_corpus"],
    )
    stored = next(m for m in mm.get_all_mappings() if m["id"] == mapping_id)
    assert stored["suggestion_score"] == 0.87
    assert stored["suggestion_corpus"] == "wp:2026-08-03"


def test_corpus_is_selected_by_the_bulk_read(models):
    """Guards the recurring bulk-export SELECT drift: a column that is written
    but not selected is invisible to every consumer, which is how #240's score
    reached the RDF exporter as NULL."""
    mm, _ = models
    mm.create_mapping(
        ke_id="KE 18", ke_title="T", wp_id="WP2873", wp_title="P",
        created_by="github:t", suggestion_score=0.5,
        suggestion_corpus="wp:2026-08-03",
    )
    assert "suggestion_corpus" in mm.get_all_mappings()[0]


def test_update_can_set_the_corpus_on_an_existing_mapping(models):
    """The new-pair approve path writes the score via update_mapping, not
    create_mapping, so the corpus has to be settable there too."""
    mm, _ = models
    mapping_id = mm.create_mapping(
        ke_id="KE 18", ke_title="T", wp_id="WP2873", wp_title="P",
        created_by="github:t",
    )
    assert mm.update_mapping(
        mapping_id=mapping_id, suggestion_score=0.42,
        suggestion_corpus="wp:2026-08-03",
    )
    stored = next(m for m in mm.get_all_mappings() if m["id"] == mapping_id)
    assert stored["suggestion_corpus"] == "wp:2026-08-03"


def test_legacy_rows_keep_a_null_corpus(models):
    """NULL is the correct value for every pre-existing row: those scores were
    produced by a corpus nobody recorded. Backfilling a guess would assert
    provenance that was never captured."""
    mm, _ = models
    mapping_id = mm.create_mapping(
        ke_id="KE 18", ke_title="T", wp_id="WP2873", wp_title="P",
        created_by="github:t", suggestion_score=0.5,
    )
    stored = next(m for m in mm.get_all_mappings() if m["id"] == mapping_id)
    assert stored["suggestion_score"] == 0.5
    assert stored["suggestion_corpus"] is None


# --- the manifest is written by a rebuild ----------------------------------

def test_rebuilding_an_artifact_records_it(tmp_path, monkeypatch):
    import scripts.embedding_utils as eu
    manifest = tmp_path / "corpus_manifest.json"
    monkeypatch.setattr(eu, "CORPUS_MANIFEST_PATH", str(manifest))

    eu.record_corpus_build(str(tmp_path / "pathway_title_embeddings.npz"), rows=803)
    written = json.loads(manifest.read_text())
    entry = written["pathway_title_embeddings.npz"]
    assert entry["rows"] == 803
    assert len(entry["built_on"]) == len("YYYY-MM-DD")


def test_recording_one_artifact_leaves_the_others_alone(tmp_path, monkeypatch):
    """Corpora are rebuilt independently — refreshing WikiPathways must not
    claim GO was rebuilt at the same time."""
    import scripts.embedding_utils as eu
    manifest = tmp_path / "corpus_manifest.json"
    manifest.write_text(json.dumps({"go_bp_embeddings.npz": {"built_on": "2026-07-11"}}))
    monkeypatch.setattr(eu, "CORPUS_MANIFEST_PATH", str(manifest))

    eu.record_corpus_build(str(tmp_path / "pathway_title_embeddings.npz"), rows=803)
    written = json.loads(manifest.read_text())
    assert written["go_bp_embeddings.npz"]["built_on"] == "2026-07-11"
    assert "pathway_title_embeddings.npz" in written


def test_an_unwritable_manifest_never_fails_the_rebuild(tmp_path, monkeypatch):
    """Best-effort by design: the cost of failing here is a missing provenance
    stamp, the cost of raising is a corpus left half-rebuilt."""
    import scripts.embedding_utils as eu
    monkeypatch.setattr(
        eu, "CORPUS_MANIFEST_PATH", str(tmp_path / "nope" / "deeper" / "m.json")
    )
    eu.record_corpus_build(str(tmp_path / "pathway_title_embeddings.npz"), rows=1)
