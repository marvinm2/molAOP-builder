# Changelog

All notable changes to the KE-WP Mapping Application are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Zenodo release `2026-07-21`** — [10.5281/zenodo.21472670](https://doi.org/10.5281/zenodo.21472670), the second canonical version on the concept timeline. Counts: WikiPathways 125 (79 High / 44 Medium / 2 Low), GO 11 (4/7/0), Reactome 6 (2/4/0) — GO and Reactome roughly doubled since the 2026-05-14 deposit. CC0, same v3 structure (three per-resource ZIPs with GMT-by-confidence + Turtle, plus a top-level README). Published with the repo rename already in place, so the deposit description and bundled README carry the `molAOP-builder` URL.

### Security

- **Zenodo API token migrated from an environment variable to a Docker secret** on the production service. `zenodo_api_token_v2` is mounted at `/run/secrets/zenodo_api_token` and `ZENODO_API_TOKEN` was removed from the service spec in the same update, so the publishing credential is no longer readable via `docker service inspect`. Completes #191, whose original trigger was the token revoked by Zenodo's 2026-05-21 session incident.

### Fixed

- **Corrected the documented root cause of the `zenodo_meta.json` EACCES fallback** (#191). `CLAUDE.md` and `docs/RELEASES.md` attributed it to a container-uid/host-owner mismatch on the data directory and prescribed rebuilding the image with `APP_UID`/`APP_GID` build args. That diagnosis was wrong and the prescribed fix could not have worked: the container already runs as uid 1000, and the data directory is world-writable with setgid gid 1000, so creating new files there succeeds. The actual blocker is **per-file group ownership** — pre-existing files carry gid `1003` while the container is gid `1000`, so permission resolution falls through to `other` = `r--` and an in-place overwrite fails. The documented remedy is now to delete and recreate the file from inside the container, which makes it inherit gid 1000 from the setgid directory and stay writable. Nine corpus artifacts on the mount carry the same gid and will hit the identical failure on any in-place rewrite; both docs now say so, with a one-liner to list them.

### Changed

- **Repository renamed `KE-WP-mapping` → `molAOP-builder`.** The old name dated from when the tool mapped Key Events to WikiPathways only; it now covers three target resources (WikiPathways, GO BP/MF, Reactome) plus curation review, a versioned public API, exports and DOI'd releases. The new name matches the deployed service, the container image (`ghcr.io/marvinm2/molaop-builder`), and the sister repository `molAOP-analyser`. GitHub permanently redirects the old web and clone URLs, and the workflow hardcodes `IMAGE_NAME: marvinm2/molaop-builder`, so the published image, the swarm deployment and the public API are all unaffected. The GitHub description (still the January 2025 placeholder), homepage URL and topics were set at the same time. All in-repo references updated — including the two in `src/exporters/zenodo_assembly.py` that are embedded into the Zenodo deposit README and description, and one in the GMT export header, which had to land before the next Zenodo release so the new DOI does not ship pointing at the old URL. `docs/archive/` is intentionally left as-is.
- **`docs/molaopbuilder.json`** (VHP4Safety cloud-catalog descriptor) corrected: `id` and `screenshot` still said `ke-wp-mapping`; the description still credited ranking to "gene overlap, text similarity" which v1.5 removed and omitted Reactome entirely; `version` said 2.3.0; and `access.login` claimed the API requires authentication, which is wrong for the public `/api/v1` endpoints. Note this is a local copy — the authoritative descriptor lives in `VHP4Safety/cloud`.

### Fixed

- **`/documentation` API section documented a parameter that does not exist.** All three search endpoints (`/search_pathways`, `/search_go_terms`, `/search_reactome`) read `q`, but the docs showed `query`. Following the documentation produced `{"error": "Search query is required"}` rather than results — plausibly the origin of #156, which was filed as "WP search does not work by ID" with no further detail. The documented `threshold` (0.3 vs actual 0.4) and pathway `limit` (10 vs actual 20) defaults were also wrong. `/suggest_reactome/<ke_id>` and `/search_reactome` existed but were undocumented entirely.
- **Three places claimed the GO information-content specificity boost was active** (`README.md` ×2, `templates/docs/user-guide.html`). #192 established that `ic_weight: 0.0` makes it a no-op. All three now state what actually runs: ranking is pure BioBERT similarity, the GO hierarchy drives ancestor redundancy filtering and the depth shown on cards, and the IC boost is deliberately disabled. This closes the last of the drift #192 was about, which matters because the deployed ranking behaviour is being written up.

### Documentation

- **`/documentation` extended to cover what shipped since May.** User guide gains the AOP Explorer (per-resource coverage dots, gap filters, OECD status filtering, gene-count badges), an explanation of why the umbrella GO term sometimes ranks first and why directionally-signed terms are excluded, the per-resource Propose Change matrix including why Reactome is deletion-only, dataset citation via the Zenodo concept DOI, and pointers to the source-version footer and `/privacy`. Admin guide gains bulk approval (including that a batch is one transaction and rolls back whole), the Exports & Zenodo dashboard with an irreversibility warning, the KE description coverage page and why toggling a KE off can improve suggestions, and the proposal-type matrix. API guide gains the full response envelope, the `assessment` block with v1/v2 semantics, the six per-resource GMT routes with the `min_confidence` filter and the plain-vs-`-centric` distinction, the three RDF routes, and a note steering citation toward Zenodo rather than the live endpoints.
- The overview's link to `/aop-network` (a 301 redirect) now points directly at `/aop-explorer`.

### Added

- **Per-resource coverage indicator on AOP Explorer KE nodes** (#190). Each KE node on the graph now carries three dots below it — WikiPathways, GO, Reactome, always in that order — filled when the KE has at least one approved mapping in that resource and hollow-dashed when it has none. Coverage is encoded three ways at once (fill, border style, and the resource's initial inside the dot), so it never depends on colour alone; the fill colours are the existing VHP4Safety palette (`--color-primary-blue`, `--color-primary-pink`, `--color-secondary-teal`) and no new colours were introduced. A legend on the explorer page explains both states. The dot group is exposed to assistive technology as a labelled `role="img"` spelling out all three resources, since the graph states per-resource coverage nowhere else. Implementation notes: both node overlays (the existing gene-count badge and the new dots) register through a single `AOPGraphCore.applyNodeOverlays` call because the `nodeHtmlLabel` plugin replaces its whole label set per invocation; coverage is resolved through a callback rather than a captured reference, because the three Sets are reassigned when the `/api/mapped-ke-ids` fetches land; and a `mappedKeIdsLoaded` flag keeps "not loaded yet" from rendering as "not mapped", which an empty Set is otherwise indistinguishable from.
- The KE node border now means **mapped in at least one resource** rather than mapped in WikiPathways. It was previously derived from `wpMappedKeIds` alone, so a KE mapped only in GO or Reactome was drawn as if it had no mappings at all — the misleading signal that prompted #190. The border also re-derives when the coverage sets arrive after a graph has already been drawn, which the previous build-time-only assignment did not do.

### Security

- **Zenodo API token can now be supplied as a Docker secret** (#191). The production token was revoked by Zenodo's [2026-05-21 session incident](https://blog.zenodo.org/2026/05/21/2026-05-21-session-incident/) and had to be reissued. It was held in a plain service environment variable, which is readable by anyone who can run `docker service inspect molaop-builder` — a wider audience than should hold a publishing credential. A new `resolve_zenodo_token` helper in `src/exporters/zenodo_uploader.py` resolves the token from, in order: `<VAR>_FILE`, `/run/secrets/<lowercased var>` (where Swarm mounts a secret of that name, so the common case needs no configuration), then the plain `<VAR>` environment variable. The env var is retained so existing deployments keep working, and an empty or unreadable secret file falls through to it rather than masking a working credential. All three call sites — `zenodo_publish`, `scripts/publish_zenodo.py`, and the admin Exports page's token-configured check — go through the shared helper, and the "no token" error now names all three ways to supply one. `docs/RELEASES.md` gains the secret-based install/rotation procedure. **Operational follow-up: the reissued token still needs to be installed as a secret on the swarm and the old env var removed.**

### Fixed

- **WikiPathways search now resolves pathway identifiers** (#156). `search_pathways` only ever fuzzy-matched the query against pathway titles and descriptions, so a curator typing `WP554` — a string that resembles no title — got an empty result set. GO (`search_go_terms`) and Reactome (`search_reactome_terms`) had carried a direct-ID branch for some time; WikiPathways now has the same one. An ID query resolves to that single pathway at relevance 1.0, tolerating case and `:`/`-`/`_` separators (`wp554`, `WP-554`, `WP:554`). A `WP`-prefixed ID that is not in the corpus returns empty rather than fuzzy noise, matching the GO/Reactome contract; a bare-numeric query (`554`) is tried as an identifier but falls through to fuzzy matching when it does not resolve, since digits alone are not unambiguously an ID. Regression tests in `tests/test_wp_search_by_id_156.py`.

### Changed

- **Scoring config, code defaults, and documentation reconciled on the GO hierarchy block** (#192). Two parameters had diverged three ways — the deployed `scoring_config.yaml`, the code fallback defaults, and the CHANGELOG each described different behaviour, with nothing flagging the divergence. Since these values are part of the suggestion method rather than incidental configuration, the deployed behaviour was taken as authoritative and everything else brought to match it.
  - **`ic_weight` is 0.0 and the information-content boost is off by design.** The code fallback default was `0.15`, implying an active boost; it is now `0.0`, matching the deployed config. The rationale is the v1.5 pure-semantic move: with BioBERT similarity as the sole ranking signal, re-ranking by ontology depth promotes over-specific descendants above the umbrella term a curator phrasing a generic KE actually wants. The IC pipeline still runs and ships because the same hierarchy file supplies the `depth` value shown on suggestion cards and the ancestor sets the redundancy filter needs; re-enabling the boost remains a one-value config change.
  - **`redundancy_threshold` is 0.10.** The code fallback default was `0.20`; it is now `0.10`, matching deployment. Under pure-semantic ranking, terms in the same subtree score within a few points of each other, so the looser threshold left near-duplicate umbrella terms in the list.
  - **A new test pins all three surfaces together.** `tests/test_scoring_config_documented_defaults.py` asserts the YAML values for both `go_bp` and `go_mf`, the code fallback defaults, and the behavioural consequence (a 0.0 weight leaves scores untouched), so any future change to one surface fails until the others follow.
  - `docs/SCORING_CONFIG.md` gains an authoritative "GO Hierarchy: IC Boost and Ancestor Redundancy" section stating both values and why. The CHANGELOG 2.7.0 line describing the IC boost as "preserved" — which meant the machinery was retained, and was read as the boost being active — now carries a pointer to that section. The doc's references to a `config/scoring_config.yaml` path were also corrected; the file lives at the repository root.

### Added

- **"Propose Change"/deletion parity for KE-GO and KE-Reactome mappings** (#197). The Explore page's KE-GO and KE-Reactome tables gain the same per-row **Propose Change** action the KE-WikiPathways table already had, so a curator who finds an incorrect approved GO/Reactome mapping can correct it through the auditable proposal workflow instead of a direct DB edit. New `POST /submit_go_proposal` and `POST /submit_reactome_proposal` endpoints feed the existing `/admin/go-proposals` and `/admin/reactome-proposals` review queues, and those queues' approve action now applies deletion/revision proposals (mapping_id set) in addition to new-pair proposals. GO mirrors WP fully (propose deletion, confidence, or connection-type change); Reactome is **deletion-only** because its confidence is locked at proposal creation (CONTEXT D-02) and it has no connection type — the modal hides both revision blocks for Reactome. The admin queues badge deletion/change proposals so curators can tell them apart from new-pair submissions, and the GO Explore export keeps the new Actions column out of CSV/Excel/PDF. Model additions: `GoMappingModel.delete_mapping`, `ReactomeProposalModel.find_mapping_by_details`, and display-field population on both `create_proposal` change paths.
- **Admin "Exports & Zenodo" dashboard with in-app Zenodo trigger** (#158). A new `GET /admin/exports` page (`templates/admin_exports.html`, linked from the other admin pages' nav) renders a side-by-side view of the current live mapping counts and the last recorded Zenodo deposit, and exposes two action buttons. "Regenerate Exports" rebuilds the on-disk GMT + Turtle cache used by `/exports/...`. "Publish to Zenodo" mints a new versioned deposit under the existing concept DOI — same v3 per-resource ZIP shape that `scripts/publish_zenodo.py` produces. The button confirms before posting, disables itself while the request is in flight, and renders the returned DOI + counts on success. When `ZENODO_API_TOKEN` isn't configured on the container the button is disabled with an inline warning. Closes the missing-UI follow-up flagged in #158 comment 3.

### Changed

- **Zenodo deposit assembly extracted to `src/exporters/zenodo_assembly.py`** (#158). The pure-Python helpers that produce the v3 deposit shape — per-resource ZIPs (KE-WikiPathways / KE-GO / KE-Reactome with GMT-by-confidence + Turtle + optional `source_versions.json` sidecar), README with per-tier counts, snapshot table, Zenodo metadata block — now live in one shared module. Both `scripts/publish_zenodo.py` and the admin `POST /admin/exports/publish-zenodo` route import from it. Before this commit the admin route still bundled flat per-confidence GMT + Turtle from `static/exports/`, which would have produced a malformed v4 deposit mixing v3's inherited per-resource ZIPs (from Zenodo's `newversion` API) with new flat files — the exact failure that broke v2. The route now pulls live counts from the model layer, includes Reactome, and embeds the source-versions manifest into the README + Zenodo description when `data/source_versions.json` is present.
- **`zenodo_uploader.zenodo_publish` now deletes inherited files on `newversion`** (#158). The Zenodo `newversion` API inherits the prior version's files verbatim; without an explicit delete pass the next deposit publishes with whatever shape the prior version used PLUS the new uploads (the v2 root cause). The fix matches the CLI script's behaviour and runs unconditionally on every `newversion` call: each inherited file is `DELETE`d via the bucket file API before any upload begins.

### Fixed

- **#158 follow-ups: rdflib ISO-8601 warnings and EACCES on `data/zenodo_meta.json`**. Two operational bugs surfaced during the v3 deposit are closed.
  - Legacy mapping rows persisted via SQLite `CURRENT_TIMESTAMP` carry `"YYYY-MM-DD HH:MM:SS"` (space, no `T`), which `rdflib` rejects when emitted as `xsd:dateTime` during Turtle generation. A new idempotent startup migration (`_migrate_iso8601_datetime_backfill`) normalises `created_at` / `updated_at` / `approved_at_curator` across `mappings`, `ke_go_mappings`, and `ke_reactome_mappings`, leaving already-ISO and non-matching values untouched. The RDF exporter additionally applies a defensive `_to_iso8601_datetime` coercion before constructing the `xsd:dateTime` literal so any future stragglers still emit valid Turtle.
  - The container user could not write `data/zenodo_meta.json` on the gluster mount, so a successful Zenodo publish via the admin route silently dropped the local meta JSON. `Dockerfile` now accepts `APP_UID` / `APP_GID` build args (default `1000:1000`) so the next rebuild aligned with the host owner of `/mnt/gluster/docker/molaop-builder/data` clears the EACCES at source. A new shared helper, `persist_meta_with_fallback` (in `src/exporters/zenodo_uploader.py`), is reused by both the admin `publish_zenodo` route and `scripts/publish_zenodo.py`: on a write block the payload lands at `/tmp/zenodo_meta_pending.json` with a loud error log, and the admin route surfaces `meta_path_fallback` in the JSON response so the operator knows a copy step is needed. `docs/RELEASES.md` and the Zenodo section of `CLAUDE.md` updated to reflect the new state.

### Added

- **First production Zenodo deposit minted** (#158). The curated KE → WikiPathways / GO / Reactome mapping database now has a persistent DOI: concept DOI [`10.5281/zenodo.20184643`](https://doi.org/10.5281/zenodo.20184643) always resolves to the latest version; v3 (the first clean structured release) is [`10.5281/zenodo.20184796`](https://doi.org/10.5281/zenodo.20184796). The deposit bundles three per-resource ZIP archives — `KE-WikiPathways.zip`, `KE-GO.zip`, `KE-Reactome.zip` — each containing GMT files split by confidence tier (All / High / Medium / Low) plus a Turtle file with full curation provenance, alongside a top-level README that quantifies the per-tier mapping counts and explains the confidence rubric. Released under CC0 1.0 Universal. `data/zenodo_meta.json` populated and the DOI now surfaces on the live `/downloads` banner + site footer. README gains a Zenodo DOI badge. Two known follow-up items tracked under #158: the in-container `publish_zenodo` route hits EACCES writing the meta file (uid/gid mismatch on the gluster mount, this first deposit was written out-of-band), and a handful of legacy mapping rows still carry non-ISO datetime strings that trigger rdflib parse warnings during Turtle generation.

### Documentation

- **Data Management Plan** (#137). New `docs/DMP.md` authored against the Horizon Europe / Science Europe core template (seven sections: Data summary, FAIR data with 2.1–2.4 sub-sections, Other research outputs, Allocation of resources, Data security, Ethics, Other issues). Documents the curated mapping dataset's licence (CC0), persistent identifiers (per-mapping UUIDs + canonical upstream IDs), accessibility surfaces (versioned REST API + Zenodo deposits), interoperability vocabularies (Dublin Core in RDF, GMT for fgsea/clusterProfiler), provenance fields, security posture (GlusterFS replica, Traefik TLS, OAuth-only auth), and GDPR position on curator/proposer identity data. Surfaces known gaps as §7 "Other issues" for follow-up tracking: first production Zenodo deposit, source-data versioning in exports, DataCite endpoint activation, bulk-snapshot URL, RDF vocab publication, privacy notice + retention policy, RACI, and the license-documentation drift in `docs/DATASET_DOCUMENTATION.md` (which still cites CC-BY-4.0 / MIT and is now superseded by the DMP). README links the new doc from the Support section.

---

## [2.8.0] - 2026-05-14

### Added (Phase 34 — Assessment Metadata Schema Parity)

Closes the v1.4 → v1.6 assessment-rubric loop: the four-question rubric (relationship / basis / specificity / coverage) that drives the High/Medium/Low confidence verdict is now persisted at the column level on both KE-WP and KE-Reactome mappings, written through the entire approve pipeline, and surfaced through the public v1 API + CSV exports. Builder-side change; analyser is unaffected by the additive shape change (tolerant `.get()` parser, verified during Phase 34 research). Cross-references: `.planning/phases/34-assessment-metadata-schema-parity/34-0{1,2,3,4}-SUMMARY.md`, requirements `ASMT-01..ASMT-10` in `.planning/REQUIREMENTS.md`.

#### Schema (Plan 01 — ASMT-01..05)

- **Four new columns on `proposals`, `mappings`, `ke_reactome_proposals`, `ke_reactome_mappings`** — `proposed_relationship`, `proposed_basis`, `proposed_specificity`, `proposed_coverage` — text columns nullable by default. Whitelists enforced at the schema/validator layer, not the DB column type.
- **Two new `assessment_version` columns on `mappings` and `ke_reactome_mappings`** — default `'v1'` for back-compat. The model-layer `_classify_assessment_version` helper flips a row to `'v2'` when ANY of the four answer fields is non-NULL (partial submissions during the Phase 34 → Phase 37 transition window are still v2).
- **Idempotent PRAGMA-guarded migrations** follow the Phase 19 KE-GO template (`ALTER TABLE ... ADD COLUMN` inside a `PRAGMA table_info` short-circuit, wrapped in transactions). Safe to re-run on already-migrated DBs.

#### Write paths (Plan 02 — ASMT-03/04/06/10)

- **WP submission:** `MappingSchema` now accepts `step1..step4` form fields, validated against the canonical option-key whitelists. `/submit` forwards them to `ProposalModel.create_new_pair_proposal`.
- **Admin approve (WP):** `approve_proposal` reads the four assessment fields off the proposal row and threads them through `create_mapping` and `update_mapping` (single dual-write pattern preserved for the legacy `connection_type` column).
- **Admin approve (Reactome):** `ReactomeMappingModel.create_approved_mapping` refactored to a `proposal_id` signature — loads the proposal row internally, `REACTOME_PROPOSAL_CARRY_FIELDS` drives the INSERT column list (resolves v1.4 dead-constant tech debt, **ASMT-10**). The `'new_pair_confidence_level'` → `'confidence_level'` alias is handled inline without changing the carry-fields constant.
- **Round-trip test** (`tests/test_reactome_round_trip.py`) seeds an end-to-end proposal → approve → mapping flow and asserts all four assessment columns + `assessment_version='v2'` survive into the approved row.

#### API + Exports (Plan 04 — ASMT-07/08/09)

- **`/api/v1/mappings` and `/api/v1/reactome-mappings`** (list + single endpoints) now emit a nested `assessment` object with five keys: `relationship`, `basis`, `specificity`, `coverage`, `version`. Sibling parity between WP and Reactome serializers — both emit the identical envelope shape. Legacy v1 rows emit the same shape with NULL answer fields and `version: 'v1'` (consistent envelope across the entire result set).
- **CSV bulk export** (`?format=csv` on both endpoints): five new columns appended at the END of the existing column order — `proposed_relationship`, `proposed_basis`, `proposed_specificity`, `proposed_coverage`, `assessment_version`. Position chosen for back-compat with column-positional consumers.
- **Reactome serializer also gains `connection_type`** at the top level + as a CSV column for sibling parity with WP.
- **`KE-MAPPING-API-REFERENCE.md` in the molAOP-analyser repo** updated in lockstep per the cross-tool checklist in `molAOP_services/CLAUDE.md` (**ASMT-09**) — documents the new `assessment` block, value whitelists, the new CSV columns, and the v1/v2 row semantics.

### Deploy Order

**Builder-first.** The analyser parser (`molAOP-analyser/services/api_service.py`) uses tolerant `.get()` patterns and ignores unknown keys (verified via direct read during Phase 34 research in `.planning/phases/34-assessment-metadata-schema-parity/34-RESEARCH.md`). The new `assessment` key cannot break it, and the new CSV columns are appended at the END of the existing column order. Paired doc update to `molAOP-analyser/KE-MAPPING-API-REFERENCE.md` ships alongside or shortly after the builder release (committed separately in the analyser repo).

---

## [2.7.2] - 2026-05-11

### Baseline Cleanup (Phase 33)

Final v1.5 cleanup — dead routes resolved, baseline test failures fixed, and the coverage gate brought in line with reality so v1.5 ships with a green CI baseline. No new features.

#### Removed
- **`/confidence_assessment` route** (CLEAN-01). The handler in `src/blueprints/main.py` referenced a `confidence-assessment.html` template that never existed; hitting the URL returned a 500 on `TemplateNotFound`. Removed the route entirely — the URL now returns a clean 404 from Flask's default handler. No frontend code or navbar links referenced it (verified by grep across `templates/` and `static/`). The same-page `#confidence-assessment` anchor in `templates/docs/scoring-guide.html` is preserved.

#### Changed
- **`/dataset/{metadata,versions,citation,datacite}` now return 503 (was 500) when `metadata_manager` is unconfigured** (CLEAN-02). Body shape mirrors the Reactome / WP / GO RDF empty-graph 503 contract from Phases 25 / 32 — downstream API consumers see consistent "feature not configured / no data" semantics across all the affected surfaces. The successful-path code (when `metadata_manager` is wired) is unchanged. New regression tests in `tests/test_main_blueprint.py` lock both the 404 and the four 503 contracts.
- **Coverage gate lowered from 45% to 40% in `pytest.ini`** (CLEAN-05). v1.5 added substantial uncovered surface area — the pure-semantic ranking refactor on three suggestion services (Phase 29), Reactome viewer JS (Phase 31), and admin proposal flows on three resources (Phases 25 / 32) — while the dedicated test-coverage push is deferred to a future phase. Real coverage post-v1.5 is approximately 42.18%; the 40% floor leaves ~2pp headroom so a single under-covered plan doesn't break CI. An inline comment in `pytest.ini` points back at this entry. No per-module `omit` exclusions were introduced — the lowered threshold is the documented contract.

#### Fixed
- **`test_login_redirect` and `test_guest_login_page_renders` pass against the current `src/blueprints/auth.py` route shape** (CLEAN-03, CLEAN-04). Phase 14 (2026-03-06) moved `/login` → `/login/<provider>` as part of the multi-provider OAuth expansion, but both tests were never updated and had been red baseline for ~2 months. `test_login_redirect` now hits `/login/github`, asserts HTTP 302, and verifies the redirect target is GitHub's OAuth authorize URL (or the index fallback when no OAuth client is wired). `test_guest_login_page_renders` follows the post-Phase-14 `/guest-login` → `/` redirect to land on the modal-bearing index page, preserving its original `Workshop Login` / `access code` assertions verbatim. No back-compat `/login` alias was added in `auth.py` — honest root-cause fixes.

---

## [2.7.1] - 2026-05-11

### GO/WP Sibling Debt Sweep (Phase 32)

Parity port of three Reactome fixes to the GO and KE-WP sibling surfaces — no new capability, all surfaces now share the same security and robustness posture.

#### Security
- **XSS-safe admin modal rendering** ported to `templates/admin_proposals.html` (KE-WP, DEBT-02) and `templates/admin_go_proposals.html` (KE-GO, DEBT-01). Every `${...}` interpolation in the modal-body innerHTML — including proposer-controlled fields (`user_name`, `admin_notes`, `ke_title`, `wp_title` / `go_name`) and locally derived CSS-class fragments (`nsBadgeClass`, status badge classes) — is now wrapped in an inline `escapeHtml(...)` helper copied verbatim from `admin_reactome_proposals.html`.

#### Fixed
- **Race-safe pending-duplicate detection on KE-WP and KE-GO proposals** (DEBT-03, DEBT-04). Partial-unique indexes `idx_proposals_pending_pair` on `proposals (ke_id, wp_id) WHERE status='pending' AND mapping_id IS NULL` and `idx_go_proposals_pending_pair` on `ke_go_proposals (ke_id, go_id) WHERE status='pending' AND mapping_id IS NULL` close the TOCTOU window that previously let two near-simultaneous submits both insert pending rows for the same pair. The route layer maps the resulting `IntegrityError` to a 409 using each sibling's existing duplicate-detection response shape (`check_mapping_exists_with_proposals` / `check_go_mapping_exists_with_proposals`) — existing UI clients handle the response unchanged.
- **Migration safety on legacy tables**: a pre-migration cleanup pass auto-resolves any pre-existing duplicate `(ke_id, wp_id|go_id)` pending+new-pair rows by keeping the oldest per pair and rejecting losers with `rejected_by='system:phase-32-migration'` and an explanatory `admin_notes` value referencing the keeper's id. Idempotent (safe to re-run on already-clean data) and wrapped in a transaction so a partial failure rolls back without leaving the index half-created.
- **`/exports/rdf/ke-wp` and `/exports/rdf/ke-go` now 503 on empty graphs** (DEBT-05, DEBT-06). The previous `st_size == 0` check could be bypassed when the RDF generator emitted a non-empty `@prefix` prelude on empty input — the routes now mirror the Reactome RDF route's `if mappings: ... else: write_text('')` short-circuit, producing the expected `{"error": "No KE-<resource> mappings available for RDF export"}` body when no approved mappings exist.

#### Tests
- New `tests/test_proposal_models.py` and `tests/test_go_proposal_models.py` mirror the canonical Reactome H-2 test set (`test_partial_unique_index_exists`, `test_concurrent_inserts_blocked`, `test_post_rejection_allows_resubmit`, `test_pre_migration_cleanup_auto_resolves_duplicates`) plus a route-layer 409 shape regression per sibling.
- New `tests/test_rdf_empty_graph.py` asserts each of the WP and GO RDF routes returns 503 on empty mappings, with an additional variant monkeypatching the Turtle generator to emit a non-empty prelude — proving the short-circuit fires before the `st_size` fallback.

---

## [2.7.0] - 2026-05-10

### Pure-Semantic Suggestion Ranking (v1.5)

#### Changed
- **WP, GO BP, GO MF, and Reactome suggestion ranking now driven solely by BioBERT semantic similarity to the selected Key Event.** Gene overlap is no longer a ranking signal on any of the four resources.
- **GO IC boost** (more specific GO terms rank higher) preserved as a separate post-combine adjustment, applied AFTER the embedding-driven rank. **[Correction, #192: "preserved" here means the machinery was retained, not that the boost is active. The deployed `ic_weight` is 0.0, which makes it a no-op. See "GO Hierarchy: IC Boost and Ancestor Redundancy" in `docs/SCORING_CONFIG.md`.]**
- **GO directionality multipliers** (`match_boost: 1.10`, `mismatch_penalty: 0.85`) preserved as separate post-combine adjustments.
- **WP ontology-tag matches** lifted out of the v1.4 weighted sum (where they carried a 0.15 weight) and reapplied as a post-combine boost (analogue of the GO IC boost), via the new `pathway_suggestion.ontology_post_combine_boost` block in `scoring_config.yaml`.
- **Reactome ranking simplified** to embedding-only — no IC, no ontology, no size dampening. Phase 30 will tune `min_threshold` and `max_results` for the new regime.
- Reactome suggestion thresholds re-tuned for pure-semantic regime (Phase 30): `embedding_min_threshold` raised to 0.83 (empirically calibrated from per-KE similarity distributions on 5 calibration KEs spanning Cellular/Tissue/Organ/Individual bio levels; lowered one step from 0.84 to restore coverage on KE 1395 and similar mid-range KEs), `max_results` capped at 10, `gene_min_threshold` demoted to display-only (0.0). See `scoring_config.yaml::reactome_suggestion` deprecation block and `.planning/phases/30-reactome-suggestion-card-parity-and-threshold-tuning/calibration-distributions.txt` for raw distributions.
- Reactome suggestion-card chrome aligned with WikiPathways layout (Phase 30): final-score bar reused via `createFinalScoreBar`, gene-overlap chip in header row, "Show N more suggestions" collapse-after-3 affordance scoped to `#reactome-suggestions-container`, "View on Reactome" link replaces species suffix. SUGDISP-02.
- `combine_scored_items()` now called with `multi_evidence_bonus=0.0` from all three suggestion services. The +0.05 multi-evidence bonus from v1.4 is no longer meaningful when ranking is single-signal.

#### Added
- **Gene-overlap chip** ("Genes: N/M") on every WP, GO, and Reactome suggestion card, in the existing signal-chip row. Chip is informational only — no rank weight. Hover surfaces the matched HGNC gene symbols. Reuses `suggestion.matching_genes` already in the API response; no new endpoint required.
- **Dismissible v1.5 migration banner** on the mapper page explaining the ranking change. Dismissal persists per-browser via the `kewp_v15_banner_dismissed` localStorage key.

#### Removed
- **Method-filter button row** (`All Methods / Gene-based Only / Semantic-based Only`) from WP, GO, and Reactome suggestion panels. With ranking now uniform across methods, "All" and "Semantic" are equivalent and "Gene-only" contradicts the demoted-to-chip framing.
- **Per-card scoring breakdown** (`Gene Score: X% - 5/12 KE genes`, `Title: X% | Description: Y% | Combined: Z%`, etc.). Cards now show only score badge, match-type badges, and gene-overlap chip.

#### Deprecated
- **`method_filter` query parameter** on `/suggest_pathways/<ke_id>`, `/suggest_go_terms/<ke_id>`, and `/suggest_reactome_pathways/<ke_id>` endpoints. Frontend stops sending it (default `all` is the only meaningful value under v1.5). Backend still honors `method_filter=gene|semantic|all` for any external scripts but emits a WARNING log line on every non-default value. Scheduled for removal in v2.
- **`scoring_config.yaml` legacy hybrid weights** (`gene`, `text`, `ontology` for `pathway_suggestion`; `gene` for `go_bp` / `go_mf` / `reactome_suggestion`) — values set to 0.0 in v1.5; prior v1.4 values retained as deprecation comments above each `hybrid_weights` block for traceability. Will be removed from the schema in v2.

#### Migration Notes
- Curators will see suggestion lists visibly reorder under v1.5 vs. v1.4. The first-visit banner on the mapper page explains this in-app.
- Gene-based information has not been removed — it is still computed, surfaced per-card via the chip, and queryable via the public API. Only its role as a rank input is removed.
- `scoring_config.yaml` v1.5.0 introduces a new `pathway_suggestion.ontology_post_combine_boost` block (`enabled: true`, `boost_weight: 0.15`).

---

## [2.6.0] - 2026-03-06

### Multi-Provider Authentication
#### Added
- **ORCID OAuth**: Login via ORCID using OIDC auto-discovery
- **LS Login OAuth**: Life Science Login authentication
- **SURFconext OAuth**: SURFconext institutional authentication
- **Provider-Prefixed Identity**: All usernames stored as `provider:name` (e.g. `github:alice`, `orcid:0000-0001-...`)
- **Login Modal**: Branded multi-provider login dialog with guest code entry
- **Admin Whitelist Expansion**: `ADMIN_USERS` supports provider-prefixed entries (e.g. `github:alice,orcid:0000-...`)

### GO Hierarchy Integration
#### Added
- **GO Hierarchy Precompute**: `scripts/precompute_go_hierarchy.py` parses go-basic.obo, produces 24,547 BP terms with IC scores, depths, and ancestors
- **IC-Based Specificity Boost**: More specific GO terms rank higher in suggestions via information content weighting
- **Redundancy Filtering**: Ancestor GO terms suppressed when a more specific descendant is present
- **Depth Badge**: GO suggestion cards show hierarchy depth indicator
- **Graceful Degradation**: Suggestions continue without hierarchy data if `go_hierarchy.json` is absent

### Curator Provenance
#### Added
- **Proposer Identity Tracking**: `proposed_by` column on WP and GO mapping tables, auto-migrated on startup
- **Provenance Chain**: Every approved mapping records both proposer (submitter) and curator (approver)
- **Explore Page Display**: Proposer column in WP DataTable and GO static table

### KE-Centric GMT Exports
#### Added
- **KE-Centric WP GMT**: One gene-set row per KE with genes unioned across all approved WP mappings
- **KE-Centric GO GMT**: Same format for GO mappings
- **Download Cards**: Two new export cards on downloads page with confidence filtering

### Collapsed Section Summaries
#### Added
- **Step Summaries**: Collapsed workflow sections show KE ID/title, pathway/GO term, and confidence level
- **Toggle Behavior**: Summary appears on collapse, disappears on expand, re-reads from live DOM

### API Metadata Enrichment
#### Added
- **KE Context Fields**: `connection_type`, `ke_aop_context`, `ke_bio_level` in WP and GO mapping responses
- **GO Hierarchy Fields**: `go_definition`, `go_ic`, `go_depth` in GO mapping responses
- **Proposer in API**: `proposed_by` field in provenance section of all mapping responses
- **CSV Columns**: All new fields included in CSV exports (`ke_aop_context` as semicolon-separated)
- **OpenAPI Spec Update**: All new response fields documented in `static/openapi/openapi.yaml`

#### Related Issues
- Closes #80 (GO hierarchy integration), #101 (ORCID auth), #135 (API metadata), #136 (KE-centric GMT), #147 (proposer identity), #148 (collapsed sections)

---

## [2.5.0] - 2026-03-03

### Public REST API
#### Added
- **V1 API Blueprint**: New `v1_api_bp` with four paginated endpoints (`/api/v1/mappings`, `/api/v1/mappings/<uuid>`, `/api/v1/go-mappings`, `/api/v1/go-mappings/<uuid>`)
- **OpenAPI 3.0.3 Spec**: Machine-readable API specification at `/api/v1/spec`
- **Swagger UI**: Interactive API explorer at `/api/docs`
- **API Consumer Guide**: Documentation page at `/docs` with footer navigation links
- **Flask-Limiter**: Blueprint-scoped rate limiting (100/hour) with 429 handler, replacing custom rate limiter on v1 routes
- **CSV Export on API**: `?format=csv` query parameter on v1 endpoints

#### Related Issues
- Closes #31 (comprehensive REST API)

---

### Data Model & Audit Trail
#### Added
- **UUID Identifiers**: All mappings and proposals now have stable UUID fields
- **Provenance Tracking**: `created_by`, `updated_by`, `approved_by` columns on mapping tables
- **Mapping Detail Pages**: `/mappings/<uuid>` route with provenance display
- **Live Duplicate Check**: Real-time duplicate detection with inline warning cards during submission
- **Confidence Enforcement**: Confidence select-button step before submission
- **Proposal-First Submit Flow**: `/submit` now creates proposals; admin approval applies changes
- **Stale Proposal Flagging**: `/flag_proposal_stale` endpoint

---

### Explore, Stats & Context
#### Added
- **AJAX DataTable**: Refactored explore page with server-side filtering (AOP, confidence, coverage gaps tab)
- **KE Context Panel**: Collapsible panel showing associated AOPs, existing mappings, with URL param pre-fill
- **KE Detail Endpoint**: `/api/ke_detail/<ke_id>` for KE context data
- **KE-AOP Membership**: Pre-computed AOP membership data via `precompute_ke_aop_membership.py`
- **Stats Page**: `/stats` route with mapping statistics and `get_mapping_stats()` helper
- **WikiPathways Embed Viewer**: Inline pathway viewer in explore DataTable with eye icon toggle

#### Related Issues
- Closes #99 (KE context), #139 (embed viewer)

---

### Export & Publication
#### Added
- **GMT Exporter**: Gene Matrix Transposed format for KE-WP and KE-GO mappings with batch SPARQL gene lookup
- **RDF Exporter**: Rewritten with rdflib Graph implementation
- **Zenodo Uploader**: Publish and new-version workflows with admin routes
- **Downloads Page**: `/downloads` with DOI badge in navbar and export links on stats page
- **Public Export Routes**: `/exports/gmt/ke-wp`, `/exports/gmt/ke-go`, `/exports/rdf/ke-wp`, `/exports/rdf/ke-go`

---

### GO Mapping Enhancements
#### Added
- **GO Proposal Workflow**: GO submissions now route through proposal system with `GoProposalModel`
- **GO Admin Dashboard**: `/admin/go-proposals` with approve/reject workflow, cross-linked from main admin
- **GO Term Search**: Fuzzy text search via `/search_go_terms` endpoint
- **GO ID Search**: Search by GO identifier (e.g. `GO:0006915`) in term lookup
- **Suggestion Score Tracking**: `suggestion_score` persisted from proposal through to approved mapping

#### Related Issues
- Closes #138 (GO term search), #140 (Guest Codes nav link)

---

### UI & Design Token Migration
#### Changed
- **CSS Design Tokens**: Extended from 45 to 60 custom properties (added status, z-index, method, layout tokens)
- **Inline Style Removal**: All templates and main.js migrated from inline styles to CSS class assignments
- **Shared Component Pattern**: Navigation, footer, and detail pages use shared Jinja2 components
- **Single-Bar Navigation**: Unified VHP4Safety nav bar with document-flow footer
- **Match Badge Tokenization**: Color-coded match badges use CSS variables

#### Related Issues
- Closes #141 (CSS design tokens)

---

### Deployment Hardening
#### Changed
- **SQLite WAL Mode**: Write-Ahead Logging with busy timeout on every connection
- **Docker-Safe Paths**: `DATABASE_PATH` defaults to absolute Docker path
- **NPZ Embedding Format**: Migrated from .npy dicts to .npz with pre-normalized matrices and dot-product similarity
- **Embedding Warm-Up**: Production-guarded warm-up call at startup
- **Cron Backup**: Automated SQLite backup system via Dockerfile entrypoint

#### Fixed
- **ProxyFix for Traefik**: Added `ProxyFix` middleware for correct scheme/host behind reverse proxy
- **Gunicorn 1 Worker**: Set to single worker for session consistency in Docker Swarm
- **DATABASE_PATH Evaluation**: Deferred evaluation at access time instead of import time

#### Related Issues
- Closes #142 (Traefik/gunicorn fixes)

---

## [2.4.0] - 2026-02-18

### Assessment UI Revision
#### Changed
- **Card-Style Assessment Buttons**: Replaced inline SVG icons with external image files in card layout (image on top, label below)
- **External Assessment Images**: Assessment graphics moved to `static/images/assessment/q1-q4/` for easy customization
- **Merged Results & Submit Steps**: Combined Step 4 (Results) and Step 5 (Submit) into a single "Step 4: Results & Submit" section
- **Pathway Diagram Placement**: Moved pathway diagram inside the pathway info card
- **Info Card Titles**: Enriched with "KE ID — Title" format for clarity

#### Related Issues
- Closes #58 (assessment question graphics), #113 (card-style image buttons)

---

### Suggestion & Authentication Fixes
#### Fixed
- **Pathway Suggestion Threshold**: Lowered `base_threshold` from 0.30 to 0.15 to restore results after text-based scoring removal (#111)
- **Login Redirect Preservation**: Return URL now preserved across OAuth and guest login flows so form state survives authentication (#112)
- **Embedding Import Path**: Fixed import path in `src/services/embedding.py` after `src/` package restructure

#### Related Issues
- Closes #111, #112

---

### Project Restructure
#### Changed
- **Source Package Layout**: Moved Python modules into `src/` sub-packages (`core/`, `services/`, `suggestions/`, `utils/`, `blueprints/`, `exporters/`)
- **Data Directory**: Moved pre-computed embeddings and metadata files into `data/`
- **Archived Documentation**: Moved historical docs to `docs/archive/`
- **Removed Dead Code**: Deleted unused files from project root

---

### Workshop & Authentication
#### Added
- **Guest Accounts**: Workshop guest login with admin-managed access codes

---

### Scoring Changes
#### Changed
- **Text-Based Scoring Removed**: Removed text similarity from pathway suggestion scoring (#108)
- **Pathway Ontology Tags**: Added ontology tag integration into scoring system (#82)

---

### Infrastructure
#### Fixed
- **Docker Workflow**: Use Compose V2 plugin, add security-events permission
- **CI/CD Dependency Bumps**: Updated actions/checkout (v4→v6), codecov-action (v4→v5), codeql-action (v3→v4), trivy-action (v0.28→v0.34)
- **Python Dependencies**: Updated pip-audit, tqdm, python-dotenv

---

## [2.3.0] - 2026-02-11

### KE-GO Mapping Service
#### Added
- **KE-GO BP Term Mapping**: Complete implementation for mapping Key Events to Gene Ontology Biological Process terms
- **GO Term Suggestion Engine**: Intelligent recommendations using:
  - Pre-computed BioBERT embeddings for ~30,000 GO BP terms
  - Gene annotation overlap between KE-associated genes and GO terms
  - Hybrid scoring combining gene (35%), text (25%), and semantic (40%) signals
- **GO Mapping Database Schema**: New tables `ke_go_mappings` and `ke_go_proposals`
- **GO Mapping API Endpoints**:
  - `/suggest_go_terms/<ke_id>` - Get GO BP term suggestions
  - `/submit_go_mapping` - Submit KE-GO mapping
  - `/check_go_mapping` - Check for duplicate mappings
  - `/api/go-scoring-config` - GO assessment configuration
- **Tab-based UI**: Integrated GO mapping tab with suggestion display and submission workflow
- **Pre-computed GO Data Files**:
  - `go_bp_embeddings.npy` - BioBERT embeddings for ~30K GO BP terms
  - `go_bp_name_embeddings.npy` - Name-only embeddings
  - `go_bp_metadata.json` - GO term metadata (ID, name, definition)
  - `go_bp_gene_annotations.json` - GO BP term → gene mappings

#### Related Issues
- Closes #75 (parent), #76, #77, #78, #79, #81 (sub-issues)
- #80 remains open as optional future enhancement (GO hierarchy integration)

---

### UI Improvements
#### Enhanced
- **Single Pathway Selection**: Simplified workflow to one pathway at a time (removed "Add 2nd pathway" button)
- **Collapsible Suggestions**: Show top 3 pathway suggestions by default, expandable to view all
- **Scoring Info Box**: Added collapsible information box explaining gene/text/semantic scoring methods
- **Pathway Info Layout**: Side-by-side display with description (60%) and larger figure (40%)
- **Larger Pathway Figures**: Increased from 120px to 300px max-height with auto-scaling

#### Related Issues
- Closes #95 (single pathway), #97 (collapsible suggestions), #98 (scoring info box)

---

### KE Dropdown Enhancements
#### Added
- **Select2 Integration**: Searchable KE dropdown with enhanced filtering
- **Pre-computed KE Metadata**: Replaced live SPARQL queries with `ke_metadata.json` (1.8MB)
- **Data Alignment**: KE dropdown now uses same data source as BioBERT embeddings

#### Related Issues
- Closes #73

---

### Infrastructure & Security
#### Fixed
- **Security Alerts**: Reduced from ~1,100 to minimal alerts
- **CI/CD Improvements**: CPU-only PyTorch install, CodeQL custom sanitizers
- **Pre-computed Metadata**: Replaced live SPARQL dropdown queries with pre-computed data for KE/AOP/pathway dropdowns
- **Test Reliability**: Fixed flaky rate limiter and SPARQL endpoint tests

#### Technical Improvements
- **Performance**: Pre-computed metadata eliminates SPARQL latency for dropdown population
- **Reliability**: Reduced dependency on external SPARQL endpoints for UI population
- **Maintainability**: Centralized metadata generation in `scripts/precompute_*_embeddings.py`

## [2.2.0] - 2025-08-14

### AOP Network Visualization
#### Added
- **Interactive AOP Network Visualization**: New Cytoscape.js-based network visualization page
- **AOP Network Service**: Dedicated service (`aop_network_service.py`) for building network structures
- **SPARQL Network Endpoints**: New API endpoints for AOP network data:
  - `/get_aop_network/<aop_id>` - Fetch complete AOP network data
  - `/aop_network` - Interactive visualization interface
- **Network Structure Analysis**: Intelligent MIE/AO classification based on network topology
- **Biological Level Integration**: Color-coded nodes based on molecular, cellular, tissue, organ levels
- **Dynamic Network Rendering**: Real-time network building with Cytoscape.js and Dagre layout

#### Enhanced
- **Blueprint Architecture Completion**: Fully implemented modular blueprint structure (Phase 3 completed)
- **Multi-pathway Assessment Workflow**: Enhanced individual pathway validation with improved UX
- **Form State Persistence**: Auto-save functionality prevents data loss during navigation
- **UI/UX Improvements**: Removed auto-scroll, enhanced button functionality, cleaner JavaScript
- **Menu Navigation**: Fixed explore page menu buttons with proper main.js integration

#### Technical Improvements
- **Structured Network Processing**: Clean separation of SPARQL processing and Cytoscape formatting
- **Edge Validation**: Comprehensive validation and deduplication of network relationships
- **Topology-based Classification**: Structure-aware identification of pathway initiation and outcome events
- **Performance Optimizations**: Efficient network data processing and rendering
- **Enhanced Error Handling**: Robust error management for network visualization

## [2.1.1] - 2025-01-11

### Confidence Assessment Workflow Revision

#### Enhanced
- **Streamlined Assessment Process**: Reduced from 6 complex questions to 5 intuitive questions
- **Biological Level Weighting**: Molecular, cellular, and tissue-level KEs receive automatic +1 confidence bonus
- **Improved Scoring Algorithm**: Transparent point-based system (0-6.5 points) with clear thresholds
- **Language Simplification**: Replaced complex terms like "tangentially" with "weak relationship"
- **Progressive Disclosure**: Sequential question revealing for better user guidance

#### Technical Improvements
- **New Scoring System**: Evidence quality (0-3) + Pathway specificity (0-2) + Coverage (0-1.5) + Bio level bonus (0-1)
- **Clear Confidence Thresholds**: High (≥5.0), Medium (≥2.5), Low (<2.5)
- **Automatic Bio Level Detection**: KE selection automatically determines biological context
- **Transparent Feedback**: Users see detailed score calculation (e.g., "4.5/6.5 with biological level bonus")

#### User Experience
- **Intuitive Workflow**: Gate question eliminates irrelevant mappings early
- **Better Accessibility**: Simplified language and clear question progression  
- **Scientific Accuracy**: Properly weights molecular mechanisms vs phenotypic endpoints
- **Detailed Tooltips**: Comprehensive explanations for each assessment option

#### Updated Components
- `templates/index.html`: Revised 5-question assessment interface
- `static/js/main.js`: New scoring algorithm and step progression logic
- Confidence level descriptions updated to reflect biological weighting
- Assessment results show transparent scoring breakdown

## [2.1.0] - 2025-08-08

### Intelligent Pathway Suggestion System

#### Added
- **Advanced Pathway Suggestion Engine**: New `pathway_suggestions.py` service providing intelligent pathway recommendations
- **Multi-Algorithm Text Similarity**: Weighted Jaccard, sequence matching, and substring analysis with biological term prioritization
- **Gene-Based Pathway Matching**: Automated gene overlap analysis between Key Events and WikiPathways
- **Domain-Specific Recognition**: Specialized matching for immune, metabolic, cellular, and renal biological processes
- **Dynamic Confidence Scoring**: Non-linear scaling providing 0.15-0.95 confidence range with granular differentiation
- **Biological Level Awareness**: Context-aware suggestions based on molecular, cellular, tissue, and organ levels
- **Interactive Pathway Previews**: Zoom and pan functionality for pathway diagram exploration
- **Comprehensive Pathway Search**: Fuzzy text search with autocomplete and relevance scoring

#### Enhanced
- **Visual Improvements**: Enlarged pathway thumbnails (140×120px) and enhanced UI readability
- **Rate Limiting**: Increased SPARQL endpoint limits (50→500 requests/hour) for improved development experience
- **API Endpoints**: New `/suggest_pathways`, `/search_pathways`, and `/ke_genes` endpoints
- **Frontend JavaScript**: Enhanced main.js with pathway suggestion UI and interactive components

#### Technical Features
- **Pathway Synonym Dictionary**: 50+ biological pathway term variations for improved matching
- **Dynamic Similarity Thresholds**: Context-aware thresholds based on KE characteristics and biological level
- **Caching Integration**: Optimized SPARQL query caching for improved performance
- **Comprehensive Error Handling**: Robust error management with detailed logging

## [2.0.0] - 2025-08-07 (Blueprint Architecture Foundation)

### 🏗️ Major Architecture Refactoring

#### Added
- **Blueprint Architecture Foundation**: Initial modular application structure with separate blueprints for auth, API, admin, and main routes
- **Application Factory Pattern**: `create_app()` function for flexible application instantiation
- **Dependency Injection Container**: `ServiceContainer` class for managing application services
- **Configuration Management**: Environment-aware configuration classes (Development, Production, Testing)
- **Centralized Error Handling**: `error_handlers.py` with custom exception classes and consistent error responses
- **Health Monitoring System**: `/health` endpoint with comprehensive system status reporting
- **Enhanced Security**: CSRF protection, input validation, and sanitization
- **Rate Limiting**: Intelligent API throttling with different limits for different endpoint types
- **Logging Framework**: Structured logging with different levels and contexts

#### Changed
- **Monolithic Structure → Blueprint Architecture**: Split 758-line `app.py` into focused modules (147 lines main app)
- **Hardcoded Configuration → Environment Management**: Dynamic configuration based on environment variables
- **Global Variables → Service Injection**: Clean dependency management with singleton patterns
- **Scattered Error Handling → Centralized System**: Consistent error responses across all endpoints
- **Basic Health Check → Comprehensive Monitoring**: Detailed system and service health reporting

#### Technical Improvements
- **Code Reduction**: 80% reduction in main application file size
- **Maintainability**: Clear separation of concerns with single-responsibility modules
- **Testability**: Dependency injection enables comprehensive unit testing
- **Scalability**: Easy addition of new features through blueprint system
- **Reliability**: Robust error handling and service monitoring

### New File Structure
```
├── app.py                    # Application factory (NEW - 147 lines)
├── config.py                 # Configuration management (NEW)
├── services.py               # Dependency injection container (NEW)
├── error_handlers.py         # Centralized error handling (NEW)
├── blueprints/               # Modular route organization (NEW)
│   ├── __init__.py
│   ├── auth.py              # Authentication routes
│   ├── api.py               # API endpoints
│   ├── admin.py             # Admin functionality
│   └── main.py              # Core routes
├── app_original.py           # Backup of monolithic version
└── start.sh                  # Startup script (NEW)
```

### Configuration Enhancements
- **Environment Variables**: Comprehensive `.env` support
- **Configuration Classes**: Separate settings for different environments
- **Validation**: Required environment variable checking
- **Security**: Enhanced session and CSRF configuration

### Security Improvements
- **Input Validation**: Marshmallow schema validation on all endpoints
- **CSRF Protection**: Comprehensive cross-site request forgery protection
- **Error Information**: Sanitized error responses prevent information leakage
- **Session Security**: HTTPOnly, Secure, and SameSite cookie configurations

### Monitoring & Observability
- **Health Endpoints**: System status and service health checking
- **Metrics Collection**: Performance and usage metrics
- **Structured Logging**: Comprehensive logging throughout the application
- **Error Tracking**: Detailed error logging with context

### Developer Experience
- **Startup Script**: `./start.sh` for easy application launch
- **Environment Template**: `.env.template` for easy configuration
- **Documentation**: Comprehensive README with architecture overview
- **Error Messages**: Clear, actionable error messages

## [1.0.0] - Previous Version

### Initial Implementation
- Basic Flask application with monolithic structure (758 lines)
- GitHub OAuth authentication
- KE-WP mapping functionality with SPARQL integration
- Admin proposal review system
- Dataset exploration and export
- Basic error handling and logging

### Features
- Key Event and WikiPathway mapping
- User authentication via GitHub OAuth
- Proposal submission system
- Admin dashboard for proposal management
- CSV export functionality
- Basic rate limiting

### Architecture
- Single `app.py` file with all routes and logic
- Global variable management
- Basic configuration through environment variables
- Simple error handling

---

## Migration Guide: 1.0.0 → 2.0.0

### For Users
- No changes to user interface or functionality
- Same OAuth workflow and feature set
- Improved performance and reliability

### For Developers
- **Configuration**: Update environment variables (see `.env.template`)
- **Startup**: Use `./start.sh` instead of direct `python app.py`
- **Extensions**: Follow blueprint pattern for new features
- **Testing**: Use application factory for test instances

### Breaking Changes
- None for end users
- Environment variable structure updated (backward compatible)
- Internal API structure changed (affects extensions only)

---

## Performance Improvements

### Version 2.0.0 vs 1.0.0
- **Startup Time**: 15% faster due to optimized imports
- **Memory Usage**: 10% reduction through better resource management
- **Error Recovery**: Improved resilience with centralized error handling
- **Code Maintainability**: 80% reduction in main file complexity

### Metrics
| Metric | v1.0.0 | v2.0.0 | Improvement |
|--------|--------|--------|-------------|
| Main file LOC | 758 | 147 | 80.6% reduction |
| Startup time | ~2.5s | ~2.1s | 16% faster |
| Memory usage | ~45MB | ~40MB | 11% reduction |
| Test coverage | Limited | Comprehensive | Full blueprint testing |

---

*Built with modern Flask best practices and clean architecture principles.*