# Keeping the corpus current

The suggestion corpora are precomputed snapshots of four upstream sources. They
do not expire on their own, and until 2026-08-03 nothing noticed when they fell
behind. Measured that day, every source had moved:

| Source | Deployed | Upstream | Behind |
|---|---|---|---|
| WikiPathways | 2026-05-10 | 2026-07-10 | 2 months |
| Reactome | v96 | v97 | 1 release |
| AOP-Wiki | 2026-05-06 | 2026-07-28 | ~3 months |
| Gene Ontology | 2026-01-23 | (needs `data/go-basic.obo`) | — |

That AOP-Wiki row is not cosmetic: it is the same drift that left 34 Key Events
missing from the curation dropdown, nine of them holding approved mappings
(#239). A stale corpus is not a slightly worse corpus, it is one a curator
cannot do the work in.

## The loop

`scripts/check_source_releases.py` compares the live release identifiers from
`SourceVersionService` against `data/source_versions.json` and rebuilds what
has moved.

```bash
make check-releases          # report only
make refresh-stale-corpora   # rebuild every drifted source
```

Exit codes are chosen so a cron can alert on them:

| Code | Meaning |
|---|---|
| `0` | everything current, or a rebuild completed |
| `1` | an error prevented the check |
| `2` | drift detected and `--rebuild` was not passed |
| `3` | a rebuild was attempted and failed |

### What each source rebuilds

| Source | Commands | Notes |
|---|---|---|
| `wikipathways` | annotations, then title embeddings | also refreshes the gene-count snapshot |
| `gene_ontology` | hierarchy, annotations, embeddings | the slowest of the four |
| `reactome` | annotations, then embeddings | |
| `aopwiki` | `precompute_ke_embeddings.py --metadata-only` | **deliberately metadata only** |

AOP-Wiki is the deliberate asymmetry. A stale Key Event dropdown blocks curation
outright, because `#ke_id` is a fixed Select2 option list with no search
fallback. Stale KE *embeddings* only cost a live encode on a cache miss. Pulling
BioBERT into the routine refresh would make it slow enough that it stops being
run — which is exactly how the snapshot got three months stale in the first
place. Rebuild those on their own cadence with `make ke-corpus`.

## Two things that will bite an unattended rebuild

**The gid trap.** Artifacts on the GlusterFS mount carry gid 1003 while the
container runs as gid 1000, so an in-place rewrite fails with `EACCES` — and it
fails *after* the expensive work is done. The script therefore preflights every
artifact for writability and refuses before starting. The remedy is the one in
`CLAUDE.md`: remove the file and recreate it from inside the container so it
inherits gid 1000 from the setgid data dir.

**Half-written artifacts.** `save_embeddings()` and `save_metadata()` now write
to a sibling and `os.replace()` into position. `open(path, 'w')` truncates
immediately, so an interrupted write used to leave a partial file — and for
`ke_metadata.json`, a truncated file silently removes Key Events from curation.
A human running a rebuild sees the traceback; a cron does not.

## The manifest, and why scores carry a corpus

Every rebuild records itself in `data/corpus_manifest.json`:

```json
{
  "pathway_title_embeddings.npz": { "built_on": "2026-08-03", "rows": 803 }
}
```

`suggestion_score` is stored on every mapping and is treated as review evidence
— #236 argues a reviewer who cannot reproduce a curator's ranking cannot use the
score to judge the mapping. But a score only means anything against the corpus
that produced it, and a rebuild moves every score at once. So a submitted score
is now stamped with its corpus (`wp:2026-08-03`) on the proposal, and carried to
the mapping at approval.

Scheduling the rebuild is what made this urgent. Before, the corpus moved when
somebody chose to move it; now it moves on its own, and without the stamp a
reviewer could not tell a genuine disagreement from a rebuild that happened in
between.

The stamp is a **date**, not a timestamp: it identifies a corpus, and two
mappings scored against the same corpus must carry the same value. It is `NULL`
on every pre-existing row, which is the honest value — those scores came from a
corpus nobody recorded, and backfilling a guess would assert provenance that was
never captured.

## Deploying the cron

Follows `operations/scheduling-cronjobs.md` in the cluster docs, using the same
shape as `molaop-builder-backup`: a `replicas: 0` service running **this
service's own image**, calling a script already inside it. No shared-image
change and no GHCR auth needed, because `ghcr.io/marvinm2/molaop-builder` is
public.

Add to `/mnt/gluster/docker/cronjobs/stack.yml` on tgx1 (canonical source: the
private `TGX-UM/cronjobs` repo — keep both in sync):

```yaml
  molaop-builder-refresh-corpora:
    image: ghcr.io/marvinm2/molaop-builder:latest
    entrypoint: ["python", "/app/scripts/check_source_releases.py", "--rebuild"]
    environment:
      - "TZ=UTC"
    volumes:
      # The same bind mount the service uses — the artifacts being rebuilt and
      # the source_versions.json being refreshed both live here.
      - /mnt/gluster/docker/molaop-builder/data:/app/data
    networks: [core]
    deploy:
      replicas: 0
      restart_policy:
        condition: none
      labels:
        - "swarm.cronjob.enable=true"
        - "swarm.cronjob.schedule=0 4 * * 1"   # Mondays 04:00 UTC
        - "swarm.cronjob.skip-running=true"
```

Then `docker stack deploy -c stack.yml cronjobs`.

**Why weekly and not daily.** None of these sources releases more than monthly,
so a daily job would spend most runs confirming nothing changed while holding
BioBERT in memory. Monday 04:00 UTC puts a rebuild before the working week and
well clear of the 02:00 backup.

**`skip-running=true` matters here.** A GO rebuild can outlast the interval; two
concurrent rebuilds would race on the same artifacts.

### Run it now, off-schedule

```bash
ssh tgx1 "docker service scale cronjobs_molaop-builder-refresh-corpora=1"
ssh tgx1 "docker service logs -f cronjobs_molaop-builder-refresh-corpora"
```

### Check without rebuilding

```bash
ssh tgx1 'docker exec $(docker ps -qf name=molaop-builder) \
    python /app/scripts/check_source_releases.py'
```

## What this does not do

- **It does not restart the service.** The app reads the artifacts at startup,
  so a rebuilt corpus is not live until `docker service update --force
  molaop-builder`. Worth deciding whether the cron should do that itself; it
  currently does not, because a forced restart mid-curation is more disruptive
  than a corpus that is a few hours stale.
- **It does not notify.** The exit codes are there for it, but nothing consumes
  them yet. Icinga is on the cluster and is the natural home.
- **It does not re-embed Key Events.** See the AOP-Wiki note above.
