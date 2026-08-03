"""Regenerating KE embeddings must not move the Key Event snapshot.

`precompute_ke_embeddings.py` always re-fetched every Key Event from AOP-Wiki
SPARQL and rewrote `ke_metadata.json`. That file is what `/get_ke_options`
serves, so "rebuild the embeddings" also advanced the snapshot the curation UI
shows — a side effect nobody asked for, flagged in #225 and adjacent to #239.

The Key Events now come from the existing metadata file by default, and
refreshing the snapshot is opt-in via `--refresh-metadata`.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
# The precompute scripts import their shared helpers as a top-level module
# (`embedding_utils`), so scripts/ has to be importable to load this one.
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "scripts")))

from precompute_ke_embeddings import load_kes_from_metadata  # noqa: E402

SCRIPT = os.path.join(HERE, "..", "scripts", "precompute_ke_embeddings.py")
MAKEFILE = os.path.join(HERE, "..", "Makefile")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_metadata_refresh_is_opt_in():
    """The default must not re-fetch; the flag must exist to request it."""
    source = _read(SCRIPT)
    assert "refresh_metadata=False" in source, (
        "refreshing the KE snapshot must be opt-in, not the default"
    )
    assert "--refresh-metadata" in source


def test_fetch_is_reachable_only_via_a_flag_or_a_missing_file():
    """The AOP-Wiki fetch must be guarded, not called unconditionally.

    Pins the actual defect: the old code called it on every run. #239 moved
    the fetch into `refresh_ke_metadata()` so the snapshot can be refreshed
    without recomputing embeddings, so the guarantee is now that
    `precompute_all_ke_embeddings` reaches it only through the guard.
    """
    source = _read(SCRIPT)
    body = source[source.index("def precompute_all_ke_embeddings("):]
    guard = "if refresh_metadata or not os.path.exists(metadata_path):"
    assert guard in body, "the AOP-Wiki fetch must sit behind the opt-in guard"
    assert body.index(guard) < body.index("refresh_ke_metadata("), (
        "refresh_ke_metadata() must be reached only through the guard"
    )
    assert "fetch_all_kes()" not in body, (
        "the embedding path must not call fetch_all_kes() directly — it goes "
        "through refresh_ke_metadata(), which is the only writer of the snapshot"
    )


def test_metadata_only_refreshes_the_snapshot_without_embedding():
    """`--metadata-only` is the cheap refresh the KE dropdown needs (#239).

    It is a second opt-in route to the fetch, and deliberately so: coupling
    the snapshot refresh to the embedding pass is why it was never run often
    enough and fell 34 Key Events behind.
    """
    source = _read(SCRIPT)
    assert "--metadata-only" in source
    assert "def refresh_ke_metadata(" in source

    entry = source[source.index('if __name__'):]
    assert "if args.metadata_only:" in entry
    assert entry.index("refresh_ke_metadata()") < entry.index(
        "precompute_all_ke_embeddings("
    ), "--metadata-only must return before the embedding pass"


def test_load_kes_from_metadata_maps_every_field_the_embedding_text_uses(tmp_path):
    """Title and description are what the two embedding channels are built from."""
    meta = tmp_path / "ke_metadata.json"
    meta.write_text(json.dumps([
        {
            "KElabel": "KE 1115",
            "KEtitle": "Increase, Reactive oxygen species",
            "KEdescription": "A description.",
            "biolevel": "Cellular",
            "KEpage": "https://aopwiki.org/events/1115",
        },
        {
            "KElabel": "KE 55",
            "KEtitle": "No description here",
            "KEdescription": "",
            "biolevel": "Molecular",
            "KEpage": "https://aopwiki.org/events/55",
        },
    ]), encoding="utf-8")

    kes = load_kes_from_metadata(str(meta))

    assert [k["ke_id"] for k in kes] == ["KE 1115", "KE 55"]
    assert kes[0]["ke_title"] == "Increase, Reactive oxygen species"
    assert kes[0]["ke_description"] == "A description."
    # A KE with no description must yield "" — the with-description channel
    # falls back to the title alone, and None would break the concatenation.
    assert kes[1]["ke_description"] == ""


def test_entries_without_an_id_are_skipped(tmp_path):
    meta = tmp_path / "ke_metadata.json"
    meta.write_text(json.dumps([
        {"KEtitle": "Orphan with no KElabel"},
        {"KElabel": "KE 9", "KEtitle": "Real one"},
    ]), encoding="utf-8")

    assert [k["ke_id"] for k in load_kes_from_metadata(str(meta))] == ["KE 9"]


def test_makefile_exposes_both_corpus_targets():
    """#225: a script with no target is effectively undiscoverable."""
    makefile = _read(MAKEFILE)
    assert "ke-corpus:" in makefile
    assert "ke-corpus-refresh:" in makefile
    assert "--refresh-metadata" in makefile, (
        "the refresh variant must be the one that passes the flag"
    )


def test_title_only_embeddings_are_computed_not_copied():
    """The title-only file must never be a copy of the with-description set.

    That copy was the cause of #209 — a title-only request silently returned a
    title+description vector. Only the backward-compat `ke_embeddings.npz` may
    be a copy.
    """
    source = _read(SCRIPT)
    body = source[source.index("def precompute_all_ke_embeddings("):]

    assert "title_only_embeddings = compute_embeddings_batch(" in body, (
        "title-only embeddings must be computed from title-only text"
    )
    # The single permitted copy is with_desc -> ke_embeddings.npz.
    copies = [ln.strip() for ln in body.splitlines() if "shutil.copy" in ln]
    assert len(copies) == 1, f"unexpected copy operations: {copies}"
    assert "with_desc_path, output_path" in copies[0], (
        "the only permitted copy is with_desc -> the backward-compat file"
    )
