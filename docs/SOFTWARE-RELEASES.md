# Releasing the software to Zenodo

Runbook for tagging this repository and archiving the source under a citable DOI.

> **This is one of two Zenodo channels.** This file covers the **software**. The curated
> mapping **dataset** is a separate deposit with its own DOI, its own licence and its own
> trigger — see [`docs/RELEASES.md`](RELEASES.md). Do not mix them up: the dataset is pushed
> by a script against the live database, the software is pulled by Zenodo from a GitHub
> Release.

## How the archive is triggered

Zenodo's GitHub integration is **already enabled** on this repository. There is no script to
run and no token to supply.

- **Publishing a GitHub Release** is what fires the webhook. Zenodo then downloads the source
  tarball of that tag and mints a DOI.
- **Pushing a tag does not.** `git push origin v2.9.0` archives nothing. This makes a throwaway
  tag a safe way to test CI without minting anything.
- **Pre-releases are archived too.** Marking a Release as a pre-release does *not* stop Zenodo
  — it mints a real DOI. Never use a pre-release as a rehearsal.

## What Zenodo reads

`.zenodo.json` in the repository root, taken **from the tagged commit**. That is the whole
reason the release-prep commit has to land before the tag: anything wrong in the tree at that
moment is frozen into a permanent record.

`CITATION.cff` is also present, but Zenodo **ignores it entirely** whenever `.zenodo.json`
exists. `CITATION.cff` is there for GitHub's "Cite this repository" button and for tools like
`cffconvert`. Keep the two in agreement — a test asserts the version fields match.

A minted version DOI cannot be withdrawn, only superseded by a later version.

## Checklist

1. Everything that must be true in the archive is already on `main` — citation metadata,
   licence and copyright, README that a stranger can follow, no placeholder DOIs, no claims
   about routes that do not serve.
2. One release-prep commit: `CHANGELOG.md` `[Unreleased]` → the version heading,
   `src/__init__.py::__version__`, and `CITATION.cff` `version` + `date-released`. One commit,
   because `tests/test_app_version.py` pins them to each other.
3. Merge; wait for CI green **on that exact commit**.
4. Annotated tag, push, confirm the tag build produced `ghcr.io/marvinm2/molaop-builder:<tag>`.
5. Publish a **full** GitHub Release (never a pre-release) with hand-written notes.
6. Verify the Zenodo record: creators (no `dependabot[bot]`), licence, version, files, the
   `vhp4safety` community request, and that the VHP4Safety grant resolved to a named award
   rather than a bare code. Record the concept DOI.
7. Backfill the DOI into the README badge, `CITATION.cff`, `.zenodo.json` and the VHP4Safety
   catalog entry. Backfilling `.zenodo.json` matters: it is re-read from every future tag, so
   an un-backfilled file silently reverts the record's cross-links on the next release.

## If the record does not appear

Check Zenodo's **Errors** tab on the GitHub integration page, and the webhook delivery log in
the repository settings. An invalid `.zenodo.json` fails **silently** — no Release-time error,
just no record. The recovery is to delete the GitHub Release, fix the file, and publish again;
the integration re-fires on the release event.

To exercise the metadata before it matters, link the repository on **sandbox.zenodo.org**,
publish a throwaway release there, inspect the resulting record, and unlink. Validating that
`.zenodo.json` is well-formed JSON proves nothing about whether Zenodo accepts the licence
identifier, the community, or the relation types.

## Freeze rules during a release window

Between the release-prep merge and the tag:

- **Do not run `make ke-corpus`.** Rebuilding the KE embeddings moves every suggestion score
  while `corpus_versions.py` keeps emitting the same corpus stamp (#286), so scores recorded
  during the window would be attributed to the wrong corpus.
- **Do not approve GO Molecular Function mappings.** The GMT export omits the namespace token
  (#152); as long as every GO mapping is `biological_process` that cannot mislead anyone.
