# Releasing the curated dataset to Zenodo

Runbook for cutting a new version of the curated KE → WikiPathways / GO / Reactome
mapping database. For the data-management policy that sits behind these mechanics
see [`docs/DMP.md`](DMP.md); for the dataset's schema and re-use guidance see
[`docs/DATASET_DOCUMENTATION.md`](DATASET_DOCUMENTATION.md).

## What gets released

- **Concept DOI** [`10.5281/zenodo.20184643`](https://doi.org/10.5281/zenodo.20184643)
  — stable, always resolves to the latest version. This is the citation target.
- **Version DOI** — a fresh DOI per release, recorded in `data/zenodo_meta.json`.
- **License**: CC0 1.0 Universal · **Access**: open.
- **Structure**: three per-resource ZIP archives plus a top-level README:
  - `KE-WikiPathways.zip` — GMT × {All, High, Medium, Low} + Turtle for WikiPathways
  - `KE-GO.zip` — same shape for Gene Ontology (BP / MF)
  - `KE-Reactome.zip` — same shape for Reactome
- **Version label**: ISO date of release (`YYYY-MM-DD`) — set automatically by the
  release script.

## How to trigger a release

All releases go through [`scripts/publish_zenodo.py`](../scripts/publish_zenodo.py).
The script runs inside the production container so it can talk to the database
directly; auth is via a Zenodo personal access token stored in the
`ZENODO_API_TOKEN` swarm-service environment variable.

```bash
# Preview only — assembles ZIPs + README in memory, computes mapping counts,
# reports what would be published; no Zenodo calls, no file writes.
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) python /app/scripts/publish_zenodo.py --dry-run'

# Real release — skips silently if mapping counts haven't changed since last deposit.
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) python /app/scripts/publish_zenodo.py'

# Force a release even when counts are unchanged (e.g. when re-issuing for metadata fixes).
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) python /app/scripts/publish_zenodo.py --force'

# Threshold the skip gate (e.g. only release when ≥5 new mappings accumulated).
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) python /app/scripts/publish_zenodo.py --min-delta 5'

# Rehearse against sandbox.zenodo.org (requires ZENODO_SANDBOX_API_TOKEN).
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) python /app/scripts/publish_zenodo.py --sandbox'
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Published successfully OR skipped because counts are unchanged |
| `1`  | Configuration error (missing token, app context failed) |
| `2`  | Zenodo API HTTP error during publish |
| `3`  | Lock held by another invocation |

## Post-release checklist

After a successful release:

1. **Verify on Zenodo** — open https://doi.org/10.5281/zenodo.20184643 and confirm
   the new version shows up with the expected file list and counts.
2. **Sync `data/zenodo_meta.json` back into git**. The script writes it inside the
   container (or falls back to `/tmp/zenodo_meta_pending.json` if the gluster mount
   blocks the write — see "Known limitations" below). Either way:
   ```bash
   scp tgx1:/mnt/gluster/docker/molaop-builder/data/zenodo_meta.json data/zenodo_meta.json
   # or, if the EACCES fallback fired:
   scp tgx1:/tmp/zenodo_meta_pending.json data/zenodo_meta.json
   ```
   Then `git add data/zenodo_meta.json && git commit -m "feat(zenodo): record release YYYY-MM-DD"`.
3. **Bump the DOI badge if the concept DOI is being referenced elsewhere** — README,
   landing page, etc. (The concept DOI is stable, so usually nothing to change.)
4. **Optional: changelog entry** under `[Unreleased]` summarising what's new in the
   dataset.

## Cadence

Currently **manual / event-driven**. The right time to release is after a substantive
batch of curation activity has landed and you want it citable. The skip gate means
running the script when nothing has changed is harmless (silent no-op).

Monthly cron is supported but not turned on. The relevant snippet, when you want to
flip it on:

```cron
# First Monday of each month at 09:00 — change-gated; silent skip on quiet months
0 9 1-7 * 1 /usr/bin/docker exec $(/usr/bin/docker ps -qf name=molaop-builder) python /app/scripts/publish_zenodo.py >> /var/log/molaop-zenodo.log 2>&1
```

## Known limitations

These are tracked under GitHub [issue #158](https://github.com/marvinm2/molAOP-builder/issues/158):

- **`data/zenodo_meta.json` write may fall back to `/tmp/`** if the container uid
  doesn't match the host owner of the gluster mount. The `Dockerfile` now accepts
  `APP_UID` / `APP_GID` build args (default `1000`) so a rebuild aligned with the
  host owner clears the original EACCES. Both the release script and the admin
  `publish_zenodo` route share `persist_meta_with_fallback`: a successful Zenodo
  deposit never appears to fail; on a write block the payload lands at
  `/tmp/zenodo_meta_pending.json` (loud log, response includes `meta_path_fallback`).
  Operator then `scp`s it back into git. Confirm the host owner with
  `ssh tgx1 stat -c '%u %g' /mnt/gluster/docker/molaop-builder/data` and rebuild
  with `docker build --build-arg APP_UID=<uid> --build-arg APP_GID=<gid> ...` if
  it isn't 1000:1000.
- **`rdflib` ISO-8601 warnings** are resolved. The DB startup migration
  (`_migrate_iso8601_datetime_backfill`) normalises legacy `"YYYY-MM-DD HH:MM:SS"`
  rows to ISO-8601 across the three mapping tables, and the RDF exporter applies
  a defensive coercion so any future stragglers still emit valid `xsd:dateTime`.
- **No failure alerting on cron path.** Until that's added, monthly cron must be
  paired with a periodic eyeball of `/var/log/molaop-zenodo.log` or a small wrapper
  that emails / Slack-pings on non-zero exit.

## Token management

The token is resolved by `resolve_zenodo_token` in
`src/exporters/zenodo_uploader.py`, which checks three sources — first hit wins:

1. `ZENODO_API_TOKEN_FILE` — path to a file containing the token.
2. `/run/secrets/zenodo_api_token` — where Swarm mounts a secret of that name.
   Nothing to configure; this is the intended production path.
3. `ZENODO_API_TOKEN` — the plain environment variable. Still supported so an
   existing deployment keeps working, but **not** how a new one should be set up.

**Prefer the Docker secret.** An environment variable is readable by anyone who
can run `docker service inspect molaop-builder` on the cluster, which is a wider
audience than the people who should hold a publishing credential (#191).

### Installing or rotating the production token

Mint the token first: zenodo.org → Account → Applications → Personal access
tokens → new token with scopes **`deposit:write`** and **`deposit:actions`**.

```bash
# 1. Create the secret (reads from stdin, so the token never lands in shell history).
#    Use a versioned name — Swarm secrets are immutable and cannot be updated in place.
ssh tgx1 'printf %s "<TOKEN>" | docker secret create zenodo_api_token_v2 -'

# 2. Attach it to the service under the name the code looks for, and drop the
#    old env var in the same update so there is no window where both exist.
ssh tgx1 'docker service update \
    --secret-add source=zenodo_api_token_v2,target=zenodo_api_token \
    --env-rm ZENODO_API_TOKEN \
    molaop-builder'

# 3. Verify — the admin Exports page should show the token as configured, and a
#    dry run exercises the resolution path without touching Zenodo.
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) \
    python /app/scripts/publish_zenodo.py --dry-run'

# 4. Confirm the token is no longer exposed in the service definition.
ssh tgx1 'docker service inspect molaop-builder --format "{{json .Spec.TaskTemplate.ContainerSpec.Env}}"'

# 5. Revoke the superseded token on zenodo.org, then remove the old secret:
ssh tgx1 'docker secret rm zenodo_api_token_v1'   # only after the new one is verified
```

To rotate later, repeat with `_v3`, `--secret-rm zenodo_api_token_v2` alongside
the `--secret-add`. Existing published deposits are unaffected by rotation.

- **Rotate after**: a major release campaign, a suspected leak, an upstream
  incident that invalidates tokens (as in Zenodo's 2026-05-21 session incident,
  which revoked the original production token — #191), or whenever the
  responsible admin changes.
- **Sandbox token**: optional, only needed for `--sandbox` rehearsals. Get from
  https://sandbox.zenodo.org/account/settings/applications/tokens/new/ and supply
  it the same three ways, as `ZENODO_SANDBOX_API_TOKEN` /
  `/run/secrets/zenodo_sandbox_api_token`.

## Version timeline

For historical context:

| Version | DOI | Date | Notes |
|---------|-----|------|-------|
| v1 | `10.5281/zenodo.20184644` | 2026-05-14 | First deposit. Flat filenames, placeholder creator. Superseded. |
| v2 | `10.5281/zenodo.20184759` | 2026-05-14 | Added per-resource ZIPs but inherited v1's flat files; transitional. Superseded. |
| v3 (`2026-05-14`) | `10.5281/zenodo.20184796` | 2026-05-14 | First canonical release — clean per-resource ZIP layout, correct creator, date-based version label, README with counts. |

The concept DOI `10.5281/zenodo.20184643` resolves to whichever of these is latest.
