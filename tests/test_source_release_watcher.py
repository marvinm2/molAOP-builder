"""The corpus should notice when its upstream sources move.

`SourceVersionService` has always fetched live release identifiers, and
`data/source_versions.json` has always recorded what was captured. Nothing
compared them, so the corpus fell months behind unnoticed — on 2026-08-03
WikiPathways was two months stale, Reactome one release behind, and AOP-Wiki
nearly three months, the last being the same drift that left 34 Key Events
uncurable in the GUI (#239).

The subtle part is that the two sides record the same release in different
shapes. Reactome stores `release_version: "96"` while the live path normalises
to `"v96"`; AOP-Wiki stores `snapshot_date` where the others store
`release_date`. Comparing the raw fields reports permanent drift and rebuilds
on every run, which is worse than not checking at all.
"""
import sys
import os

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import check_source_releases as watcher  # noqa: E402


def _stored(**sources):
    return {name: dict(status="ok", **fields) for name, fields in sources.items()}


# --- identifier normalisation ---------------------------------------------

def test_reactome_stored_version_is_normalised_like_the_live_one():
    """The trap: "96" and "v96" are the same release. Comparing them raw would
    mark Reactome drifted forever and rebuild it on every single run."""
    entry = {"status": "ok", "release_version": "96", "release_date": "2026-03-25"}
    assert watcher.stored_identifier("reactome", entry) == "v96"


def test_aopwiki_reads_snapshot_date_not_release_date():
    entry = {"status": "ok", "snapshot_date": "2026-05-06"}
    assert watcher.stored_identifier("aopwiki", entry) == "2026-05-06"


def test_wikipathways_reads_release_date():
    entry = {"status": "ok", "release_date": "2026-05-10"}
    assert watcher.stored_identifier("wikipathways", entry) == "2026-05-10"


def test_a_non_ok_stored_entry_yields_no_identifier():
    entry = {"status": "error", "release_date": "2026-05-10"}
    assert watcher.stored_identifier("wikipathways", entry) is None


# --- drift detection -------------------------------------------------------

def _patch(monkeypatch, live, stored):
    monkeypatch.setattr(watcher, "snapshot", lambda: live)
    monkeypatch.setattr(watcher, "stored_versions", lambda: stored)


def test_a_moved_source_is_reported_as_drifted(monkeypatch):
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-07-10", "unavailable": False}},
        stored=_stored(wikipathways={"release_date": "2026-05-10"}),
    )
    stored_id, live_id, drifted = watcher.compare()["wikipathways"]
    assert (stored_id, live_id, drifted) == ("2026-05-10", "2026-07-10", True)


def test_matching_versions_are_not_drift(monkeypatch):
    _patch(
        monkeypatch,
        live={"reactome": {"version": "v96", "unavailable": False}},
        stored=_stored(reactome={"release_version": "96"}),
    )
    assert watcher.compare()["reactome"][2] is False


def test_an_unavailable_upstream_is_not_drift(monkeypatch):
    """Rebuilding because an endpoint had a bad day would rebuild constantly
    and, worse, would look like a real release each time."""
    _patch(
        monkeypatch,
        live={"gene_ontology": {"version": None, "unavailable": True}},
        stored=_stored(gene_ontology={"release_date": "2026-01-23"}),
    )
    assert watcher.compare()["gene_ontology"][2] is False


def test_nothing_recorded_locally_is_not_drift(monkeypatch):
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-07-10", "unavailable": False}},
        stored={},
    )
    assert watcher.compare()["wikipathways"][2] is False


# --- exit codes, which are what a cron alerts on ---------------------------

def test_report_mode_exits_2_when_something_drifted(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "REBUILT_MARKER", str(tmp_path / ".corpus-rebuilt"))
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-07-10", "unavailable": False}},
        stored=_stored(wikipathways={"release_date": "2026-05-10"}),
    )
    monkeypatch.setattr(sys, "argv", ["check_source_releases.py"])
    assert watcher.main() == 2


def test_report_mode_exits_0_when_current(monkeypatch):
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-05-10", "unavailable": False}},
        stored=_stored(wikipathways={"release_date": "2026-05-10"}),
    )
    monkeypatch.setattr(sys, "argv", ["check_source_releases.py"])
    assert watcher.main() == 0


def test_a_failed_rebuild_exits_3_and_leaves_the_manifest_alone(monkeypatch):
    """The manifest must not advance past a rebuild that failed. Recording a
    version whose corpus was never rebuilt suppresses the next alert and makes
    the drift permanently invisible — the exact failure this script exists to
    prevent."""
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-07-10", "unavailable": False}},
        stored=_stored(wikipathways={"release_date": "2026-05-10"}),
    )
    monkeypatch.setattr(watcher, "rebuild", lambda source: False)
    captured = []
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: captured.append(a))
    monkeypatch.setattr(sys, "argv", ["check_source_releases.py", "--rebuild"])

    assert watcher.main() == 3
    assert captured == [], "capture_source_versions must not run after a failure"


def test_a_successful_rebuild_refreshes_the_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher, "REBUILT_MARKER", str(tmp_path / ".corpus-rebuilt"))
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-07-10", "unavailable": False}},
        stored=_stored(wikipathways={"release_date": "2026-05-10"}),
    )
    monkeypatch.setattr(watcher, "rebuild", lambda source: True)
    calls = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(
        watcher.subprocess, "run", lambda cmd, **k: calls.append(cmd) or _Ok()
    )
    monkeypatch.setattr(sys, "argv", ["check_source_releases.py", "--rebuild"])

    assert watcher.main() == 0
    assert any("capture_source_versions.py" in " ".join(c) for c in calls)


# --- preflight -------------------------------------------------------------

def test_preflight_flags_an_unwritable_artifact(tmp_path, monkeypatch):
    """On the cluster the artifacts carry gid 1003 while the container runs as
    gid 1000, so an in-place rewrite fails with EACCES *after* an expensive
    rebuild has already run. Catching it first is the whole point."""
    artifact = tmp_path / "data" / "pathway_title_embeddings.npz"
    artifact.parent.mkdir()
    artifact.write_bytes(b"x")
    artifact.chmod(0o444)
    monkeypatch.setattr(watcher, "PROJECT_ROOT", str(tmp_path))

    assert watcher.preflight(["data/pathway_title_embeddings.npz"]) == [
        "data/pathway_title_embeddings.npz"
    ]


def test_preflight_passes_on_a_writable_artifact(tmp_path, monkeypatch):
    artifact = tmp_path / "data" / "pathway_title_embeddings.npz"
    artifact.parent.mkdir()
    artifact.write_bytes(b"x")
    monkeypatch.setattr(watcher, "PROJECT_ROOT", str(tmp_path))
    assert watcher.preflight(["data/pathway_title_embeddings.npz"]) == []


def test_rebuild_refuses_rather_than_wasting_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(watcher, "preflight", lambda artifacts: ["data/blocked.npz"])
    ran = []
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: ran.append(a))

    assert watcher.rebuild("wikipathways") is False
    assert ran == [], "no rebuild command may run once preflight has failed"


# --- the rebuild map -------------------------------------------------------

@pytest.mark.parametrize("source", sorted(watcher.REBUILD))
def test_every_rebuild_target_names_its_artifacts(source):
    """The artifact list is what preflight checks, so a source with commands
    but no artifacts would silently skip the writability guard."""
    spec = watcher.REBUILD[source]
    assert spec["commands"], source
    assert spec["artifacts"], source


def test_aopwiki_rebuild_is_the_cheap_metadata_only_path():
    """A stale KE dropdown blocks curation outright; stale KE embeddings only
    cost a live encode on a miss. Pulling BioBERT into a routine refresh would
    make it too slow to run often, which is how the snapshot got stale."""
    commands = watcher.REBUILD["aopwiki"]["commands"]
    assert commands == [
        ["python", "scripts/precompute_ke_embeddings.py", "--metadata-only"]
    ]


# --- the restart marker ----------------------------------------------------

def test_a_successful_rebuild_marks_the_service_for_restart(tmp_path, monkeypatch):
    """The app reads its artifacts at startup, so a rebuilt corpus is not live
    until it restarts. The marker is what lets the restart be conditional
    rather than a weekly session-dropping ritual."""
    import json as _json
    marker = tmp_path / ".corpus-rebuilt"
    monkeypatch.setattr(watcher, "REBUILT_MARKER", str(marker))

    watcher.write_rebuilt_marker(["wikipathways"])
    assert _json.loads(marker.read_text())["sources"] == ["wikipathways"]


def test_an_unapplied_marker_is_merged_not_replaced(tmp_path, monkeypatch):
    """Two rebuilds between two restarts must not lose the first one's entry,
    or the restart would be skipped for a corpus that genuinely changed."""
    import json as _json
    marker = tmp_path / ".corpus-rebuilt"
    marker.write_text(_json.dumps({"sources": ["reactome"]}))
    monkeypatch.setattr(watcher, "REBUILT_MARKER", str(marker))

    watcher.write_rebuilt_marker(["wikipathways"])
    assert _json.loads(marker.read_text())["sources"] == ["reactome", "wikipathways"]


def test_a_corrupt_marker_does_not_lose_the_new_rebuild(tmp_path, monkeypatch):
    import json as _json
    marker = tmp_path / ".corpus-rebuilt"
    marker.write_text("{not json")
    monkeypatch.setattr(watcher, "REBUILT_MARKER", str(marker))

    watcher.write_rebuilt_marker(["wikipathways"])
    assert _json.loads(marker.read_text())["sources"] == ["wikipathways"]


def test_no_marker_is_written_when_a_rebuild_failed(monkeypatch, tmp_path):
    """A failed rebuild must not schedule a restart — restarting onto a corpus
    that was not replaced serves the same data with a dropped session."""
    marker = tmp_path / ".corpus-rebuilt"
    monkeypatch.setattr(watcher, "REBUILT_MARKER", str(marker))
    _patch(
        monkeypatch,
        live={"wikipathways": {"version": "2026-07-10", "unavailable": False}},
        stored=_stored(wikipathways={"release_date": "2026-05-10"}),
    )
    monkeypatch.setattr(watcher, "rebuild", lambda source: False)
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["check_source_releases.py", "--rebuild"])

    assert watcher.main() == 3
    assert not marker.exists()
