# KE-WP Mapping Dataset Documentation

## Table of Contents
1. [Dataset Overview](#dataset-overview)
2. [Data Schema](#data-schema)
3. [Data Collection Methodology](#data-collection-methodology)
4. [Quality Control](#quality-control)
5. [Access Methods](#access-methods)
6. [Export Formats](#export-formats)
7. [API Documentation](#api-documentation)
8. [Usage Examples](#usage-examples)
9. [Citation](#citation)
10. [Licensing](#licensing)

## Dataset Overview

### Description
The KE-WP Mapping Dataset contains curated mappings between Key Events from the Adverse Outcome Pathway (AOP) framework and biological pathways from WikiPathways. Each mapping includes expert-assessed confidence levels and connection type classifications to support toxicological research and systems biology applications.

### Key Statistics
- **Total Mappings**: Variable (updated continuously)
- **Data Sources**: AOP-Wiki SPARQL endpoint, WikiPathways SPARQL endpoint
- **Update Frequency**: Real-time via community contributions
- **Quality Control**: Expert curation with guided assessment workflow

### Biological Context
- **Key Events (KE)**: Measurable biological changes that are essential steps in adverse outcome pathways
- **WikiPathways**: Community-curated biological pathway database with visual representations
- **Mapping Purpose**: Links molecular-level Key Events to well-characterized biological pathways for mechanistic understanding

## Data Schema

### Core Fields

| Field Name | Data Type | Required | Description | Example |
|------------|-----------|----------|-------------|---------|
| `id` | Integer | Yes | Unique mapping identifier | `1234` |
| `ke_id` | String | Yes | Key Event identifier from AOP-Wiki | `KE 1234` |
| `ke_title` | String | Yes | Full title of the Key Event | `Oxidative stress in liver` |
| `wp_id` | String | Yes | WikiPathways pathway identifier | `WP4949` |
| `wp_title` | String | Yes | Full title of the WikiPathways pathway | `Apoptosis signaling pathway` |
| `connection_type` | String | Yes | Biological relationship type | `causative` |
| `confidence_level` | String | Yes | Evidence-based confidence assessment | `high` |
| `created_by` | String | No | GitHub username of contributor | `researcher123` |
| `created_at` | DateTime | Yes | Timestamp of mapping creation | `2025-01-15T10:30:00Z` |
| `updated_at` | DateTime | No | Timestamp of last modification | `2025-01-20T14:45:00Z` |

### Controlled Vocabularies

#### Connection Type
- **`causative`**: The pathway directly causes the Key Event to occur
- **`responsive`**: The pathway responds to or is activated by the Key Event
- **`other`**: Other defined biological relationship exists
- **`undefined`**: Relationship type has not been determined

#### Confidence Level
- **`high`**: Direct and specific biological link with strong experimental evidence
- **`medium`**: Partial or indirect biological relationship with moderate evidence
- **`low`**: Weak, speculative, or unclear biological connection

### Data Validation Rules

1. **ID Format Validation**:
   - Key Event ID: Must match pattern `^KE\\s+\\d+$`
   - WikiPathway ID: Must match pattern `^WP\\d+$`

2. **Title Requirements**:
   - Maximum length: 500 characters
   - Cannot be empty or null

3. **Uniqueness Constraints**:
   - Each (ke_id, wp_id) pair must be unique
   - Prevents duplicate mappings

4. **Reference Validation**:
   - KE IDs must exist in AOP-Wiki database
   - WP IDs must exist in WikiPathways database

## Data Collection Methodology

### Source Databases

#### AOP-Wiki SPARQL Endpoint
- **URL**: https://aopwiki.rdf.bigcat-bioinformatics.org/sparql
- **Data Retrieved**: Key Event identifiers, titles, biological levels, gene associations
- **Update Frequency**: Real-time queries
- **Schema Used**: `edam:data_1025` (Key Event), `edam:data_2298` (Gene)

#### WikiPathways SPARQL Endpoint  
- **URL**: https://sparql.wikipathways.org/sparql
- **Data Retrieved**: Pathway identifiers, titles, descriptions, organism information
- **Update Frequency**: Real-time queries
- **Visual Resources**: SVG pathway diagrams from `https://www.wikipathways.org/wikipathways-assets/pathways/`

### Curation Workflow

1. **Key Event Selection**: Researchers browse AOP-Wiki database for relevant Key Events
2. **Pathway Identification**: System provides pathway suggestions using:
   - Text similarity algorithms (Jaccard, SequenceMatcher)
   - Gene-based matching from AOP-Wiki annotations
   - Biological level-aware scoring
3. **Expert Assessment**: Guided confidence evaluation through structured questionnaire:
   - Biological relevance assessment
   - Evidence strength evaluation
   - Pathway specificity analysis
   - Functional independence determination
4. **Quality Review**: Community feedback and admin review for high-impact mappings
5. **Version Control**: All changes tracked with user attribution and timestamps

### Assessment Criteria

#### Confidence Level Determination
The confidence assessment follows a structured workflow evaluating:

1. **Biological Relevance**: Direct relationship between KE and pathway
2. **Evidence Quality**: Experimental support (in vitro/in vivo studies)
3. **Pathway Specificity**: How well the pathway matches the Key Event
4. **Functional Independence**: Whether pathway components can operate as discrete units
5. **Biological Level Compatibility**: Molecular/cellular KEs receive higher confidence than organ/population level

#### Connection Type Classification
- **Causative Relationships**: Pathway activation leads to Key Event occurrence
- **Responsive Relationships**: Key Event triggers pathway activation
- **Complex Relationships**: Bidirectional or indirect associations

## Quality Control

### Automated Validation
- **Schema Validation**: Marshmallow schemas ensure data integrity
- **Format Checking**: Regular expressions validate ID formats
- **Duplicate Prevention**: Database constraints prevent redundant mappings
- **Reference Verification**: SPARQL queries validate source database references

### Expert Review Process
- **Community Feedback**: Public review system for mapping quality
- **Admin Oversight**: Expert review for scientifically significant mappings  
- **Correction Mechanisms**: Proposal system for mapping updates/corrections
- **Version Tracking**: Complete audit trail of all changes

### Data Quality Metrics
- **Completeness**: Percentage of required fields populated
- **Consistency**: Standardization across controlled vocabularies
- **Accuracy**: Validation against source databases
- **Currency**: Freshness of references and mappings

## Access Methods

### Web Interface
- **Main Application**: https://molaop-builder.vhp4safety.nl/
- **Features**: Interactive mapping creation, confidence assessment, data exploration
- **Authentication**: GitHub OAuth for contributions
- **Export Options**: Multiple format downloads

### RESTful API
- **Base URL**: `/api/v1/`
- **Authentication**: Optional (required for write operations)
- **Rate Limits**: 1000 requests/hour (general), 100 requests/hour (submissions)
- **Documentation**: Built-in OpenAPI specification

### Direct Database Access
- **Format**: SQLite database file
- **Location**: `ke_wp_mapping.db`
- **Tables**: `mappings`, `proposals`, `dataset_versions`, `sparql_cache`
- **Backup**: Automated daily snapshots

## Export Formats

### CSV Export
- **Description**: Comma-separated values with comprehensive metadata header
- **Use Cases**: Spreadsheet analysis, basic data processing
- **Features**: Statistical summaries, field descriptions, data provenance
- **Access**: `/download` endpoint

### JSON Export
- **Description**: Structured JSON with schema documentation
- **Features**: Complete metadata, provenance information, statistics
- **Schema**: Full field definitions and validation rules included
- **Access**: `/export/json` endpoint

### JSON-LD Export
- **Description**: Linked Data format for semantic web applications
- **Vocabularies**: Schema.org, custom AOP and WikiPathways vocabularies
- **Features**: Machine-readable semantic annotations
- **Access**: `/export/jsonld` endpoint

### RDF/XML and Turtle
- **Description**: Semantic web formats using biological ontologies
- **Namespaces**: FOAF, Dublin Core, custom biological vocabularies
- **Features**: Ontology integration, semantic reasoning support
- **Access**: `/export/rdf` and `/export/turtle` endpoints

### Excel Export
- **Description**: Multi-sheet workbook with data dictionary
- **Sheets**: Main data, field definitions, value explanations, statistics, metadata
- **Features**: Color-coded headers, auto-sized columns, comprehensive documentation
- **Access**: `/export/excel` endpoint

### Parquet Export
- **Description**: Columnar format optimized for analytics
- **Features**: Optimized data types, derived analytical columns, field metadata
- **Compression**: Snappy compression (configurable)
- **Access**: `/export/parquet` endpoint

## API Documentation

### Base Endpoints

#### List Mappings
```
GET /api/v1/mappings
```
**Parameters:**
- `page`: Page number (default: 1)
- `per_page`: Items per page (1-100, default: 20)  
- `sort_by`: Sort field (default: created_at)
- `sort_order`: asc/desc (default: desc)
- `ke_id`: Filter by Key Event ID
- `wp_id`: Filter by WikiPathway ID
- `connection_type`: Filter by relationship type
- `confidence_level`: Filter by confidence
- `search`: Full-text search

#### Get Specific Mapping
```
GET /api/v1/mappings/{id}
```

#### Create New Mapping
```
POST /api/v1/mappings
Content-Type: application/json

{
  "ke_id": "KE 1234",
  "ke_title": "Oxidative stress in liver",
  "wp_id": "WP4949", 
  "wp_title": "Apoptosis signaling pathway",
  "connection_type": "causative",
  "confidence_level": "high"
}
```

#### Update Mapping
```
PATCH /api/v1/mappings/{id}
Content-Type: application/json

{
  "confidence_level": "medium"
}
```

#### Delete Mapping
```
DELETE /api/v1/mappings/{id}
```

### Bulk Operations
```
POST /api/v1/mappings/bulk
Content-Type: application/json

{
  "operation": "create",
  "mappings": [...]
}
```

### Statistics
```
GET /api/v1/mappings/stats
```

## Usage Examples

### Python Examples

#### Basic Data Access
```python
import requests
import pandas as pd

# Get all mappings with pagination
response = requests.get("https://molaop-builder.vhp4safety.nl/api/v1/mappings?per_page=100")
data = response.json()
mappings = data["data"]

# Convert to DataFrame
df = pd.DataFrame(mappings)
print(f"Retrieved {len(df)} mappings")
```

#### Filtering and Search
```python
# Search for oxidative stress related mappings
response = requests.get("https://molaop-builder.vhp4safety.nl/api/v1/mappings?search=oxidative stress")
oxidative_mappings = response.json()["data"]

# Filter by high confidence causative relationships  
response = requests.get("https://molaop-builder.vhp4safety.nl/api/v1/mappings?confidence_level=high&connection_type=causative")
high_confidence = response.json()["data"]
```

#### Data Export
```python
BASE = "https://molaop-builder.vhp4safety.nl"

# All mappings as CSV
response = requests.get(f"{BASE}/download")
with open("ke_wp_dataset.csv", "wb") as f:
    f.write(response.content)
df = pd.read_csv("ke_wp_dataset.csv")

# Gene sets for fgsea / clusterProfiler (GMT). Swap ke-wp for ke-go or
# ke-reactome; each also has a -centric variant keyed on the Key Event.
response = requests.get(f"{BASE}/exports/gmt/ke-wp")
with open("KE-WP.gmt", "wb") as f:
    f.write(response.content)

# Mappings with full curation provenance (Turtle)
response = requests.get(f"{BASE}/exports/rdf/ke-wp")
with open("KE-WP.ttl", "wb") as f:
    f.write(response.content)
```

> The `/export/json` and `/export/parquet` calls this section used to show answer `500` on
> the live service — the generic `/export/<format>` route needs a metadata manager that is
> not configured (#160). For Parquet and Excel, take them from the Zenodo deposit instead.

### R Examples

#### Data Loading
```r
library(httr)
library(jsonlite)
library(arrow)

# Get mappings data
response <- GET("https://molaop-builder.vhp4safety.nl/api/v1/mappings?per_page=1000")
data <- fromJSON(content(response, "text"))
mappings <- data$data

# Convert to data frame
df <- data.frame(mappings)
```

#### Analysis Example
```r
library(dplyr)
library(ggplot2)

# Confidence level distribution
confidence_summary <- df %>%
  count(confidence_level) %>%
  mutate(percentage = n / sum(n) * 100)

# Visualization
ggplot(confidence_summary, aes(x = confidence_level, y = n)) +
  geom_bar(stat = "identity") +
  labs(title = "Confidence Level Distribution",
       x = "Confidence Level", 
       y = "Number of Mappings")
```

### JavaScript Examples

#### Frontend Integration
```javascript
// Fetch mappings with filtering
async function fetchMappings(filters = {}) {
    const params = new URLSearchParams(filters);
    const response = await fetch(`/api/v1/mappings?${params}`);
    const data = await response.json();
    return data;
}

// Search functionality
const searchMappings = async (query) => {
    const data = await fetchMappings({ search: query, per_page: 50 });
    return data.data;
};

// Display results
const displayMappings = (mappings) => {
    const container = document.getElementById('mappings-container');
    container.innerHTML = mappings.map(mapping => `
        <div class="mapping-card">
            <h3>${mapping.ke_title}</h3>
            <p><strong>Pathway:</strong> ${mapping.wp_title}</p>
            <p><strong>Confidence:</strong> ${mapping.confidence_level}</p>
            <p><strong>Connection:</strong> ${mapping.connection_type}</p>
        </div>
    `).join('');
};
```

## Citation

Cite the **dataset**, not the application, when you have used the mappings. The two are
separate Zenodo deposits with separate DOIs; see [`docs/RELEASES.md`](RELEASES.md) for which
is which.

Use the **concept DOI** below. It always resolves to the newest version, which is what a
reader following the citation should land on. If you need the exact state you analysed, take
the version DOI of that release from the Zenodo record instead.

### APA Format
```
Martens, M. Molecular AOP Builder — Curated KE → WikiPathways / GO / Reactome Mappings
[Data set]. Zenodo. https://doi.org/10.5281/zenodo.20184643
```

### BibTeX Format
```bibtex
@dataset{martens_molaop_mappings,
    author    = {Martens, Marvin},
    title     = {Molecular AOP Builder — Curated KE → WikiPathways / GO / Reactome Mappings},
    publisher = {Zenodo},
    doi       = {10.5281/zenodo.20184643},
    url       = {https://doi.org/10.5281/zenodo.20184643}
}
```

Year and version are deliberately absent: the concept DOI spans every release, so pinning
either here would contradict the DOI beside it. Both are on the Zenodo record for the
version you actually used, and a citation manager will fill them in from the DOI.

### Machine-readable metadata
The DOI itself is the metadata endpoint. DataCite content negotiation returns the full
record, so no application route is needed:

```bash
curl -sL -H "Accept: application/vnd.citationstyles.csl+json" \
  https://doi.org/10.5281/zenodo.20184643
```

> Earlier revisions of this document pointed at an in-application `/dataset/datacite` route.
> That route is not implemented — it answers `503` — and is tracked in
> [#160](https://github.com/marvinm2/molAOP-builder/issues/160) along with `/dataset/metadata`
> and the `/export/*` family, which are documented elsewhere but likewise not yet serving.

## Licensing

### Dataset License
**Creative Commons Zero 1.0 Universal (CC0 1.0, Public Domain Dedication)**
- **URL**: https://creativecommons.org/publicdomain/zero/1.0/
- **Requirements**: No attribution required (encouraged as good scientific practice — see below)
- **Permissions**: Unrestricted commercial use, distribution, modification, and re-licensing of derivative datasets
- **Rationale**: CC0 was chosen to maximise downstream re-use in regulatory and commercial workflows where attribution requirements on derivative datasets can be operationally awkward. See `docs/DMP.md` §2.4 for the full reasoning.

### Source Data Licenses
The Builder re-uses four external knowledge resources at runtime. Each carries its own upstream licence:

- **AOP-Wiki**: Creative Commons Attribution-ShareAlike 4.0 International (**CC BY-SA 4.0**) for
  wiki content by default, with individual AOPs able to carry **All Rights Reserved** for a
  limited period while under development. Verified against the AOP-Wiki release notes
  ([aopwiki.org/info_pages/3](https://aopwiki.org/info_pages/3)): Release 2.6 states "Default
  license is now Creative Commons BY SA for all content. Authors can choose an All Rights
  Reserved license for a limited time while the AOP is being developed", and Release 2.8
  (2026-03-08) added automated conversion of expired ARR licences to BY-SA after a 30-day
  grace period. The AOP-Wiki RDF Explorer states the same for the RDF rendering of that
  content (`Data: CC-BY-SA 4.0`).

  This corrects two earlier claims in this document, neither of which was right: pre-2026-05
  revisions listed AOP-Wiki as CC0 1.0, and revisions since listed it as CC BY 4.0 with a
  standing "pending verification" note. **ShareAlike is a copyleft term, so this is not a
  cosmetic correction** — see the open question below.
- **WikiPathways**: Creative Commons Zero 1.0 Universal (CC0 1.0, Public Domain)
- **Gene Ontology** (incl. UniProt-GOA human annotations): Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Reactome**: Creative Commons Zero 1.0 Universal (CC0 1.0, Public Domain). Verified against [reactome.org/license](https://reactome.org/license) clause 1(c), "All data in the Reactome database and files derived from that data are licensed under the Creative Commons Public Domain Dedication (CC0)". The CC BY 4.0 on that page (clause 1a) covers Reactome's pathway illustrations, icon library, art and branding — none of which this project redistributes.

The curated mapping dataset itself is released under CC0 (above); the upstream licences govern any direct re-distribution of unmodified source data, not the curated KE → resource mappings produced by the Builder.

**Key Event titles are the one exception, and they are attributed rather than dedicated.**
The mapping itself — that KE 1234 corresponds to WP5678 — is a pair of identifiers and a fact
relating them, which is a defensible CC0 subject. The Turtle and GMT exports additionally
reproduce AOP-Wiki **Key Event titles** (`rdf_exporter.py` as a `vocab:keyEventName` literal,
`gmt_exporter.py` slugified into the GMT set name), and those are authored text carrying
AOP-Wiki's ShareAlike term. So, precisely:

- **The mappings, confidence assessments, provenance and everything derived from them: CC0 1.0.**
- **Key Event titles: reproduced from AOP-Wiki under CC BY-SA 4.0, attributed to AOP-Wiki.**
  The CC0 dedication does not extend to them.

This costs a re-user nothing they were not already doing — anyone working with KE titles is
citing AOP-Wiki anyway — and it avoids removing the titles, which the GMT set-naming
convention depends on.

### Software License
**GNU General Public License v2.0 (GPL-2.0)** — application source code and infrastructure. See [`LICENSE`](../LICENSE) at the repository root.

### Attribution Requirements
CC0 imposes no legal attribution requirement on the curated mapping dataset. Citation is nonetheless encouraged as good scientific practice. When using this dataset, please consider citing:

1. This dataset via its concept DOI [10.5281/zenodo.20184643](https://doi.org/10.5281/zenodo.20184643) (always resolves to the latest version)
2. The upstream resources whose IDs you rely on:
   - AOP-Wiki: https://aopwiki.org/
   - WikiPathways: https://www.wikipathways.org/
   - Gene Ontology: http://geneontology.org/
   - Reactome: https://reactome.org/

---

**Dataset Version**: see `data/zenodo_meta.json` (`version` field) — concept DOI [10.5281/zenodo.20184643](https://doi.org/10.5281/zenodo.20184643)
**Application Version**: whatever `/health` reports, which is `src/__init__.py::__version__`

> The two version lines here used to be hand-copied numbers and both were stale: they read
> `v2.7.2` against a deployment serving 2.8.0. A duplicated version string has no way to stay
> right, so they now name where the number lives instead of restating it. Same for the
> "Last Updated" date this block used to carry — `git log` for this file is accurate by
> construction and a hand-typed date is not.

For questions, issues, or contributions, please visit our GitHub repository or contact the development team through the platform interface.