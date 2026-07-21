# Data Management Plan — Molecular AOP Builder

**Version:** 1.0
**Date:** 2026-05-14
**Author:** Marvin Martens — Department of Translational Genomics, Maastricht University · ORCID [0000-0003-2230-0840](https://orcid.org/0000-0003-2230-0840)
**Project:** Molecular AOP Builder (`marvinm2/molAOP-builder`) · live instance at [molaop-builder.vhp4safety.nl](https://molaop-builder.vhp4safety.nl)
**Template:** Horizon Europe / Science Europe core (six-section structure with FAIR sub-sections)
**Scope:** the Molecular AOP Builder repository only. The downstream [Molecular AOP Analyser](https://molaop-analyser.vhp4safety.nl) consumes data via the Builder's REST API and is documented separately.

This DMP describes the data lifecycle of the curated Key Event → WikiPathways / Gene Ontology / Reactome mapping database that the Builder produces. It is a living document; subsequent versions will track new data types, schema changes, and the closure of items listed in §7 (Other issues).

---

## 1. Data summary

The Molecular AOP Builder produces a curated mapping database that links Key Events (KEs) of the Adverse Outcome Pathway (AOP) framework to three external biological knowledge resources: WikiPathways pathways (WP), Gene Ontology terms (GO Biological Process and Molecular Function), and Reactome pathways. Each mapping is proposed by a registered curator, scored by a BioBERT-based suggestion engine, assessed against a structured rubric (confidence level, connection type, and resource-specific quality dimensions), and approved by an administrator before entering the public REST API. The data exist to give downstream tools — most directly the Molecular AOP Analyser — a high-quality, machine-readable bridge between the AOP framework (used in toxicology and regulatory science) and the molecular pathway / ontology landscape (used in transcriptomics and pathway-enrichment workflows).

Three classes of data are managed. The first is **curated mappings**: rows in a SQLite database (`ke_wp_mapping.db`) with stable UUIDs, external canonical identifiers (AOP-Wiki KE numbers such as `KE 1234`, `WP1234`, `GO:0006915`, `R-HSA-109581`), provenance (proposer, curator, timestamps, suggestion score), and assessment metadata. Mappings grow incrementally and are expected to remain in the low-tens-of-thousands of rows. The second is **pre-computed analytical assets** in the `data/` directory: BioBERT sentence embeddings for KEs, pathways, and GO terms (`*_embeddings.npz`), GO hierarchy and information-content tables derived from `go-basic.obo`, gene-annotation tables derived from the UniProt-GOA human GAF, and KE/pathway metadata snapshots harvested from AOP-Wiki and WikiPathways SPARQL endpoints (`ke_metadata.json`, `pathway_metadata.json`, `ke_aop_membership.json`, `ker_adjacency.json`). These assets total approximately 500 MB and are regeneratable from upstream sources via the scripts in `scripts/`. The third is **operational data**: curator and proposer identity (provider-prefixed OAuth username, free-text name, email, institutional affiliation) stored in the `proposals` and `ke_*_proposals` tables, OAuth session state held in memory only, and a 24-hour SPARQL response cache (`sparql_cache` table).

The Builder re-uses three external open datasets at runtime: the AOP-Wiki RDF SPARQL endpoint (`aopwiki.rdf.bigcat-bioinformatics.org`) for KE definitions, AOP membership, and gene associations; the WikiPathways SPARQL endpoint (`sparql.wikipathways.org`) for pathway titles, descriptions, and gene members; and the Gene Ontology OBO Foundry release together with UniProt-GOA human annotations for GO hierarchy and gene-to-GO mappings. Reactome data are pulled via a precomputed pathway-export pipeline. All four upstream sources publish under permissive licences (CC0 or CC-BY) and are version-anchored by snapshot date.

The primary audiences are AOP researchers and regulatory toxicologists; pathway-enrichment users running fgsea or clusterProfiler against KE-anchored gene sets; ontology integrators (Wikidata, FAIRsharing, AOP-Wiki cross-references); and downstream applications, most prominently the Molecular AOP Analyser.

---

## 2. FAIR data

### 2.1 Making data findable, including provisions for metadata

Every approved mapping carries a stable UUID assigned at proposal time and persisted through approval, export, and republication. The UUID is exposed as the canonical row identifier in every API response, every CSV/Parquet/JSON export, and every RDF/Turtle triple set, making each mapping individually citable and dereferenceable. External references use canonical upstream identifiers throughout — AOP-Wiki KE numbers, WikiPathways `WP####`, GO Foundry CURIEs (`GO:#######`), and Reactome stable identifiers (`R-HSA-#######`) — so mappings can be joined against any tool that speaks the same ID space without ID-mapping infrastructure.

The dataset is registered with rich metadata in two places. The live REST API is described by an OpenAPI 3.0.3 specification (`static/openapi/openapi.yaml`) rendered as Swagger UI at `/api/docs`, enumerating endpoints, parameters, filterable fields, content types, and the 100-request-per-hour-per-IP rate limit. Discoverability of the curated dataset for human and machine users is delivered by Zenodo: each release of the mappings is deposited as a Zenodo record with title, description, creators, keywords, publication date, license, and access right populated by `src/exporters/zenodo_uploader.py`. Zenodo mints a versioned DOI and a stable concept DOI that always resolves to the latest version, satisfying F1 of the FAIR principles. The first production deposit was minted on 2026-05-14: **concept DOI [10.5281/zenodo.20184643](https://doi.org/10.5281/zenodo.20184643)** (always-latest) and **version DOI [10.5281/zenodo.20184796](https://doi.org/10.5281/zenodo.20184796)** (v3 — first clean structured release). The deposit bundles three per-resource ZIP archives (`KE-WikiPathways.zip`, `KE-GO.zip`, `KE-Reactome.zip`) — each containing GMT files split by confidence level (All / High / Medium / Low) and a Turtle file with full provenance — plus a README explaining the structure and rubric.

The GitHub repository, the live instance landing page, and the dataset documentation page (`docs/DATASET_DOCUMENTATION.md`) cross-link to each other to maximise discoverability through standard search and indexing.

### 2.2 Making data openly accessible

The curated mappings are open by default and accessible without authentication. Programmatic access is via the public versioned REST API at `https://molaop-builder.vhp4safety.nl/api/v1/`, which serves JSON by default and CSV via the `?format=csv` query parameter or `Accept: text/csv` content negotiation. The endpoints (`/mappings`, `/mappings/{uuid}`, `/go-mappings`, `/reactome-mappings`, `/kes`, `/pathways`) are paginated and filterable on KE, pathway, confidence, and AOP identifiers. CORS is configured open for GET and OPTIONS so that browser clients can consume the data directly. The rate limit is 100 requests per hour per IP, with a documented `Retry-After` header on 429 responses.

For bulk re-use and long-term citation, the same data are deposited to Zenodo, which provides ten-year preservation, DOIs, HTTPS resolution, and listed-in-OpenAIRE harvesting. Format-specific bulk downloads — CSV, JSON, GMT (for fgsea/clusterProfiler), RDF/Turtle, Parquet, and Excel — are available through the application's `/export/<format>` and `/exports/...` routes and are bundled into each Zenodo deposit. A dedicated bulk-snapshot URL serving the full dataset under a single canonical filename per release is planned (see §7).

The Builder's source code is openly available on GitHub at `marvinm2/molAOP-builder` under the GPL-2.0 licence, as is the schema-level documentation in `docs/DATASET_DOCUMENTATION.md` and the live OpenAPI specification. No specialised software is required to read the data: CSV, JSON, GMT, and Turtle are all plain text, and Parquet is supported by Apache Arrow, pandas, R `arrow`, and DuckDB out of the box.

### 2.3 Making data interoperable

Identifiers and vocabularies are deliberately chosen for interoperability with the wider toxicology and pathway-analysis ecosystem. Key Events use AOP-Wiki numbering, the de facto standard maintained by the OECD-anchored AOP-Wiki community. Pathways use WikiPathways `WP` and Reactome stable identifiers in their canonical form. GO terms use OBO Foundry CURIE syntax (`GO:#######`) and the hierarchy is computed against the most recent `go-basic.obo` release. Gene symbols use HGNC (with a planned future expansion to NCBI Gene / Ensembl identifiers, see PROJECT.md). Direction tags on GO mappings (positive vs. negative regulation) follow the wording used by the GO annotation files themselves.

The RDF/Turtle exporter (`src/exporters/rdf_exporter.py`) serialises each mapping as a node identified by its UUID, with predicates drawn from Dublin Core (`dcterms:creator`, `dcterms:date`, `dcterms:identifier`) and a project-local vocabulary at `https://ke-wp-mapping.org/vocab#`. Suggestion scores are typed as `xsd:decimal`, and external KE/WP/GO/Reactome references are emitted as full URIs. Publication of an OWL/SHACL schema for the project-local vocabulary, ideally under a W3ID or Bioregistry-resolvable namespace, is tracked in §7. For pathway-enrichment users, the GMT exporter produces a tab-separated gene-set file with one line per KE (or per KE-WP pair, depending on the export variant), suitable for direct ingestion by fgsea, clusterProfiler, GSEA-CLI, and similar tools without conversion. JSON exports include a `data_schema` block declaring field names, types, controlled vocabularies, and a `provenance` block citing the upstream SPARQL endpoints used to generate the row.

### 2.4 Increase data re-use (through clarifying licences)

The curated mapping dataset is released under **Creative Commons Zero (CC0 1.0 Universal, Public Domain Dedication)**. This is the licence applied to the Zenodo deposit, the GMT export, the Parquet export, and the RDF/Turtle export. CC0 was chosen to maximise downstream re-use in regulatory and commercial workflows where attribution requirements on derivative datasets can be operationally awkward. The legacy `docs/DATASET_DOCUMENTATION.md` still cites CC-BY-4.0 for the dataset and MIT for the code — both statements are out of date relative to the current shipped code (which applies CC0 to the Zenodo deposit and GPL-2.0 to the application source). Aligning that doc with the present DMP is a tracked item in §7. The JSON exporter currently emits a CC-BY-4.0 header for variant exports that include enriched metadata; this is being reviewed for consolidation onto CC0.

Each mapping row carries enough provenance for a downstream consumer to assess quality without re-fetching the source: the proposer's provider-prefixed identity, the approving curator, the proposal and approval timestamps, the BioBERT suggestion score that ranked the mapping, the confidence level (high / medium / low), and — for GO mappings — three additional quality dimensions (Connection, Specificity, Evidence). The KE → pathway / GO mapping is conceptually durable; the underlying upstream resources (AOP-Wiki, WikiPathways, GO, Reactome) are themselves curated and slow-moving. There is no embargo: a mapping is publicly queryable through the REST API immediately on admin approval, and is included in the next Zenodo release. The dataset is intended for indefinite re-use; the Builder is funded as part of the VHP4Safety infrastructure for the foreseeable future, and the Zenodo deposit guarantees a ten-year minimum availability independent of the application's own uptime.

Source-data versioning — that is, recording in each export which release of WikiPathways, which GO OBO date, which AOP-Wiki snapshot, and which Reactome version was used to derive a given mapping — is presently incomplete. Surfacing those versions on the landing page, the Downloads cards, the Stats page, and in the Zenodo metadata is part of the in-flight v1.6 milestone and is tracked in §7.

---

## 3. Other research outputs

The Builder produces one substantial non-data research output: the application source code itself, distributed under the **GPL-2.0** licence and hosted at [`github.com/marvinm2/molAOP-builder`](https://github.com/marvinm2/molAOP-builder). Releases are tagged with semantic versions (currently `v2.7.2`) and accompanied by a Keep-a-Changelog-style `CHANGELOG.md`. Container images are published to `ghcr.io/marvinm2/molaop-builder` for amd64. The BioBERT-derived embedding assets in `data/` are derivative research outputs and are regeneratable from the public upstream OBO files and the BioBERT model checkpoint; they are versioned by their generation script and date and are bundled into the production container image. A scoring configuration (`scoring_config.yaml`, currently v1.5.0) and the OpenAPI specification (`static/openapi/openapi.yaml`) accompany the dataset as machine-readable artefacts.

---

## 4. Allocation of resources

Marginal cost is essentially zero. Zenodo deposits, AOP-Wiki, WikiPathways, Gene Ontology, and Reactome all provide their services free of charge for research re-use. The Builder is hosted on the VHP4Safety Strato Docker Swarm cluster (two-manager swarm at the Strato hoster, GlusterFS-replicated storage, Traefik reverse proxy with Let's Encrypt TLS), administered by Sean Laenen and shared across the VHP4Safety service portfolio; the marginal storage and compute cost for the Builder is in the low single-digit GB range and is absorbed within the existing cluster budget.

Responsibility for data stewardship rests with Marvin Martens (Department of Translational Genomics, Maastricht University) as principal investigator and data steward. Day-to-day curator-proposal review is performed by the small set of administrators listed in the `ADMIN_USERS` environment variable on the live instance. Operational continuity of the host infrastructure is provided by the VHP4Safety cluster team, and authoritative cluster documentation lives at `/mnt/gluster/documentation/` on the cluster's primary manager node. A formal RACI matrix specifying who is Responsible / Accountable / Consulted / Informed for proposal review, schema migrations, releases, deployment, backups, incident response, and GDPR subject-rights requests is maintained in [`docs/GOVERNANCE.md`](GOVERNANCE.md).

Long-term preservation costs after the end of any specific grant period are absorbed by the Zenodo ten-year guarantee for the published dataset and by Maastricht University's institutional hosting for the GitHub source mirror.

---

## 5. Data security

Curated mappings live in a SQLite database (`ke_wp_mapping.db`) running in WAL mode for safe concurrent reads. The database file is mounted from the Strato Swarm's GlusterFS replica-2 volume at `/mnt/gluster/docker/molaop-builder/data` so that single-node failure of either swarm manager (`tgx1`, `tgx2`) does not destroy data. The application uses SQLite's Online Backup API on a daily schedule to produce timestamped snapshots with seven-day retention; the `make backup-db` and `make restore-db` Makefile targets cover ad-hoc operator-driven backups and recovery. The pre-computed `data/*.npz` and metadata files are immutable artefacts baked into the container image and regeneratable from upstream public sources, so loss is recoverable by rebuild rather than backup.

Transport security is enforced by Traefik with Let's Encrypt TLS on the public hostname `molaop-builder.vhp4safety.nl`; HTTP is redirected to HTTPS. Application-layer protections include CSRF tokens on all state-changing endpoints, parameterised SQL throughout, Marshmallow schema validation on all submitted payloads, server-side input sanitisation, output escaping in templates, and Bandit / Safety / Semgrep / Trivy scans in CI. Sessions use `HttpOnly`, `SameSite=Lax`, and `Secure` cookies in production with a one-hour CSRF token lifetime and a thirty-minute session lifetime. Authentication is delegated to external OAuth/OIDC providers (GitHub in production today; ORCID, LS Login, and SURFconext supported in code and activated by setting their respective `*_CLIENT_ID` / `*_CLIENT_SECRET` environment variables). OAuth tokens are held only in the active Flask session and are not written to disk; they are discarded on logout or session expiry.

Sensitive data are limited to curator and proposer identity fields (provider-prefixed username, free-text name, email, institutional affiliation) stored in the proposal tables. These are at present held in plaintext on the SQLite-backed volume rather than encrypted at rest; the volume itself sits behind operating-system-level access control on the cluster, and access to the SQLite file is restricted to the application container and to the small set of cluster administrators with shell access on `tgx1` / `tgx2`. The data are not classified as special-category personal data under GDPR Article 9. An explicit retention policy for proposal rows (including rejected and withdrawn proposals), guest access codes, and the SPARQL response cache, together with a documented data-deletion procedure for honouring GDPR erasure requests, is tracked in §7.

---

## 6. Ethics

The Molecular AOP Builder does not collect, process, or store any data derived from human subjects, animal experiments, clinical settings, or commercially sensitive sources. The upstream knowledge bases that the Builder consumes (AOP-Wiki, WikiPathways, Gene Ontology, Reactome, UniProt-GOA) are themselves community-curated public-knowledge resources; no ethics approval is required for their use, and they impose no constraints on the curated outputs of the Builder. Consequently, no Data Protection Impact Assessment, no Ethics Review Committee approval, and no informed-consent procedure for research subjects apply to the curated mapping dataset.

The Builder does process limited personal data of its **curators and proposers** as a necessary part of the curation workflow. When a curator authenticates via OAuth and submits a proposal, the application stores their provider-prefixed username, their entered name, their email address, and (optionally) their institutional affiliation in the relevant proposal table. After admin approval the curator's identity is also written into the immutable provenance fields of the mapping itself (`proposed_by`, `approved_by_curator`). The legal basis under GDPR is **contract performance**: the curator chooses to authenticate, submits a proposal, and consents to attribution of their contribution as part of the curation contract. Data minimisation is honoured by collecting only the fields required for attribution and admin contact. The data are retained for as long as the mapping remains in the dataset, which is the operationally meaningful retention period for an attribution record. Subject rights under GDPR (access, rectification, erasure, restriction, portability, objection) are handled by the Maastricht University Data Protection Officer in their capacity as DPO for the institutional data controller. An explicit privacy notice page at `/privacy`, surfacing this information to curators at the point of authentication, is planned (see §7).

OAuth provider terms of service are deferred to the providers themselves (GitHub, ORCID, LS Login / ELIXIR, SURFconext). The application does not request or store any provider scope beyond what is needed to read the user's profile (name, email, institutional affiliation where available) and verify their identity.

---

## 7. Other issues

This DMP version 1.0 deliberately documents the current shipped state of the Builder rather than a future aspirational state. The following items are known gaps that this DMP exposes and that are scheduled as separate work, tracked either in the v1.6 milestone or as their own GitHub issues:

- **~~First production Zenodo deposit.~~** ✅ **Done 2026-05-14.** Concept DOI [10.5281/zenodo.20184643](https://doi.org/10.5281/zenodo.20184643) (always-latest); v3 published as [10.5281/zenodo.20184796](https://doi.org/10.5281/zenodo.20184796). `data/zenodo_meta.json` populated. The DOI is surfaced on the Downloads page and in the site footer. Two follow-up items remain under #158: (a) align the container uid/gid so the in-app admin route can write `data/zenodo_meta.json` itself (currently EACCES; the file was persisted out-of-band), and (b) fix legacy non-ISO datetime strings in some mapping rows that trigger rdflib parse warnings during Turtle generation.

- **Source-data versioning in exports.** The WikiPathways release date, Reactome release tag, GO ontology version, and AOP-Wiki snapshot date are not presently recorded per mapping or per export. Capturing them in mapping rows and surfacing them on the landing page, the Downloads cards, the Stats page, and the Zenodo metadata is a v1.6 milestone item.

- **DataCite metadata endpoint.** The `/dataset/datacite` route currently returns 503 because the `metadata_manager` service is unconfigured (CHANGELOG v2.7.2). Wiring the manager and activating the endpoint will give consumers a DataCite-schema-compliant XML representation of the dataset suitable for direct deposit and harvesting.

- **Bulk dataset snapshot URL.** Publishing a single canonical URL per release that serves the full dataset in CSV, JSON-LD, and Turtle (without requiring API pagination or Zenodo navigation) is a planned enhancement.

- **RDF vocabulary publication.** The project-local vocabulary at `https://ke-wp-mapping.org/vocab#` has no formal OWL or SHACL schema published at that URI. Publishing the schema, ideally under a W3ID-resolvable or Bioregistry-resolvable namespace, will let downstream RDF consumers infer structure without reverse-engineering Turtle output.

- **~~Privacy notice and retention policy.~~** ✅ **Done 2026-05-14.** `/privacy` route now serves a public Jinja-rendered notice covering scope, processed fields, legal basis (GDPR contract performance), storage, cookies, four-category retention table (approved mappings · pending/rejected/withdrawn proposals · guest access codes · SPARQL cache), and the GDPR subject-rights procedure routing through the Maastricht University DPO. Linked from the site footer and surfaced under the OAuth provider list in the login modal so proposers see it before authenticating.

- **~~Role and responsibility matrix.~~** ✅ **Done 2026-05-14.** Published as [`docs/GOVERNANCE.md`](GOVERNANCE.md): six roles (Data Steward, Lead Developer, Curator/Admin, Proposer, Cluster Operator, DPO) × seventeen activities, with escalation paths and a documented review cadence.

- **License documentation drift.** `docs/DATASET_DOCUMENTATION.md` still cites CC-BY-4.0 for the dataset and MIT for the code. This DMP supersedes those statements (CC0 for the dataset, GPL-2.0 for the code); a follow-up edit aligns the dataset documentation accordingly.

- **Gene-identifier system swap.** Migration from HGNC symbols to NCBI Gene / Ensembl identifiers is scoped as a v2 milestone, separate from this DMP version.

This DMP will be revised on each minor release of the Builder, or sooner if any of the above items materially changes the data lifecycle. The latest version is always the file at `docs/DMP.md` on the `main` branch of the repository.
