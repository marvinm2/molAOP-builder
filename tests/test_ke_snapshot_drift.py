"""Phase 37 (#239) — a stale KE snapshot must be visible, not silent.

The Key Event dropdown is a fixed Select2 option list served from
`data/ke_metadata.json`, with no search fallback the way pathways have. A Key
Event added to AOP-Wiki since the snapshot was taken therefore cannot be
curated at all — and nine Key Events that already held approved mappings in
production could not be re-selected in the UI that produced them.

Two causes, needing different remedies, which is why this reports rather than
flipping the service to "degraded":

* **Staleness.** Measured against the live endpoint while fixing this, AOP-Wiki
  held 1595 Key Events with a maximum label of KE 2446 against a snapshot of
  1561 stopping at KE 2404. A refresh fixes these.
* **Withdrawal.** KE 123 carries an approved mapping here but no longer exists
  in AOP-Wiki at all — no triple anywhere carries the literal. No refresh
  cadence fixes that, and the service must not be permanently unhealthy for it.

Note the issue's own hypothesis — that a non-OPTIONAL `foaf:page` in
`fetch_all_kes()` drops Key Events — was measured and disproved: the harvest
query and a bare `?KE a aopo:KeyEvent` count both return 1595. That predicate
is deliberately left alone.
"""
import os
import tempfile

import pytest

from src.core.models import Database, MappingModel


@pytest.fixture
def container_with_snapshot(monkeypatch):
    """A service container whose KE snapshot deliberately lags the mappings."""
    from src.services.container import ServiceContainer

    fd, db_path = tempfile.mkstemp()
    db = Database(db_path)
    mm = MappingModel(db)

    # Three mapped Key Events; the snapshot carries only one of them.
    for ke_id, wp_id in [("KE 18", "WP2873"), ("KE 2410", "WP100"), ("KE 123", "WP1")]:
        mm.create_mapping(
            ke_id=ke_id, ke_title=f"Title for {ke_id}", wp_id=wp_id,
            wp_title=f"Pathway {wp_id}", connection_type="causative",
            # created_by carries a CHECK constraint requiring a provider prefix.
            confidence_level="high", created_by="github:tester",
        )

    container = ServiceContainer.__new__(ServiceContainer)
    container._database = db
    container._ke_metadata_index = {"KE 18": {"KElabel": "KE 18", "biolevel": "Molecular"}}
    monkeypatch.setattr(
        type(container), "database", property(lambda self: db), raising=False
    )

    yield container

    os.close(fd)
    os.unlink(db_path)


def test_drift_check_names_the_missing_key_events(container_with_snapshot):
    """KE 2410 (stale) and KE 123 (withdrawn) both hold mappings and both
    fall out of the dropdown — the check has to name them, since nothing else
    in the app ever did."""
    drift = container_with_snapshot._ke_snapshot_drift()

    assert drift["checked"] is True
    assert drift["missing_from_snapshot"] == ["KE 123", "KE 2410"]
    assert drift["mapped_key_events"] == 3
    assert drift["snapshot_size"] == 1


def test_no_drift_reported_when_the_snapshot_covers_every_mapping(
    container_with_snapshot,
):
    container_with_snapshot._ke_metadata_index = {
        "KE 18": {}, "KE 2410": {}, "KE 123": {},
    }
    assert container_with_snapshot._ke_snapshot_drift()["missing_from_snapshot"] == []


def test_check_is_skipped_rather_than_forcing_a_corpus_load(container_with_snapshot):
    """The health route must never trigger a BioBERT / corpus load, so the
    check reads the already-built index only."""
    container_with_snapshot._ke_metadata_index = None
    drift = container_with_snapshot._ke_snapshot_drift()
    assert drift["checked"] is False
    assert "reason" in drift


def test_drift_is_reported_without_flipping_the_service_to_degraded():
    """A Key Event withdrawn upstream is permanent, so reporting it as
    unhealthy would leave the service degraded forever with no action that
    clears it. The drift report must therefore live inside the nested
    "services" dict, which app.py's `all(health_status.values())` does not
    aggregate over — the same reasoning as the #209 comment on embeddings_ok,
    inverted."""
    import inspect
    from src.services.container import ServiceContainer

    source = inspect.getsource(ServiceContainer.get_health_status)
    assert 'status["services"]["ke_snapshot"]' in source
    assert 'status["ke_snapshot"]' not in source


def _ke_script_source():
    """Read the harvest script as text.

    It is not importable from the test process — it does `from embedding_utils
    import ...`, which resolves only when run with scripts/ on sys.path. Read
    rather than import, so pinning its shape does not require reorganising it.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "scripts", "precompute_ke_embeddings.py")) as fh:
        return fh.read()


def _function_body(source, name):
    lines = source.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"def {name}("))
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].startswith("def ") or lines[i].startswith("if __name__")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_metadata_refresh_skips_the_embedding_pass():
    """The refresh has to be cheap or it will not be run often enough, which
    is how the snapshot fell 34 Key Events behind in the first place. It must
    not load BioBERT to rewrite a JSON file."""
    source = _ke_script_source()
    assert "def refresh_ke_metadata(" in source
    assert "--metadata-only" in source

    body = _function_body(source, "refresh_ke_metadata")
    assert "init_embedding_service" not in body
    assert "compute_embeddings_batch" not in body
    assert "save_metadata" in body


def test_foaf_page_is_still_required_deliberately():
    """Pins the measured finding so nobody 'fixes' the disproved hypothesis.

    Making foaf:page OPTIONAL was #239's suggested fix #2, on the theory that
    it silently drops Key Events. Measured against the live endpoint, the
    harvest query and a bare `?KE a aopo:KeyEvent` count both return 1595 —
    the predicate drops nothing, and the change would be a no-op.
    """
    body = _function_body(_ke_script_source(), "fetch_all_kes")
    assert "foaf:page ?KEpage" in body
    assert "OPTIONAL { ?KE dc:description" in body
