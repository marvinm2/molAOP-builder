# Molecular AOP Builder

[![CI/CD Pipeline](https://github.com/marvinm2/molAOP-builder/actions/workflows/ci.yml/badge.svg)](https://github.com/marvinm2/molAOP-builder/actions/workflows/ci.yml)
[![Docker Build & Test](https://github.com/marvinm2/molAOP-builder/actions/workflows/docker.yml/badge.svg)](https://github.com/marvinm2/molAOP-builder/actions/workflows/docker.yml)
[![Code Quality](https://github.com/marvinm2/molAOP-builder/actions/workflows/code-quality.yml/badge.svg)](https://github.com/marvinm2/molAOP-builder/actions/workflows/code-quality.yml)
[![Security & Compliance](https://github.com/marvinm2/molAOP-builder/actions/workflows/security.yml/badge.svg)](https://github.com/marvinm2/molAOP-builder/actions/workflows/security.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20184643.svg)](https://doi.org/10.5281/zenodo.20184643)

Curator-in-the-loop tooling for building **molecular Adverse Outcome Pathways** — the
layer that connects the Key Events of an AOP to the molecular pathways and processes
that underlie them.

**Live instance:** https://molaop-builder.vhp4safety.nl
**Public REST API:** https://molaop-builder.vhp4safety.nl/api/docs

> **Note on the name.** This repository was previously `KE-WP-mapping`, from when it
> mapped Key Events to WikiPathways only. It now covers three target resources and a
> full curation, review, publication and analysis pipeline, so it is named after the
> tool it holds. GitHub redirects the old URLs; the deployed service, the container
> image (`ghcr.io/marvinm2/molaop-builder`) and the public API are unchanged.

## What is this?

A curator-in-the-loop tool to map Adverse Outcome Pathway (AOP) Key Events
to WikiPathways pathways, GO biological-process terms, and Reactome
pathways. Suggestions are ranked by BioBERT semantic similarity to the KE
description; curators assign confidence levels and an admin approves before
the mapping enters the public REST API. Approved mappings feed the
[Molecular AOP Analyser](https://molaop-analyser.vhp4safety.nl) and are
exportable as GMT (for fgsea/clusterProfiler) and RDF/TTL (for SPARQL).

## Features

### Core Functionality

- **Three mapping resources**: Key Events can be mapped to **WikiPathways** pathways,
  **Gene Ontology** Biological Process and Molecular Function terms, and **Reactome**
  pathways — each with its own suggestion corpus, curation tab, proposal queue and export.
- **Pure-semantic suggestions** (v1.5, 2026-05-10): candidates on all three resources are
  ranked by BioBERT semantic similarity between the Key Event and the target's metadata.
  Gene overlap is computed and shown to curators on each card as an informational chip but
  does **not** influence ordering. See `CHANGELOG.md` v2.7.0 and `docs/SCORING_CONFIG.md`.
- **GO hierarchy handling**: a precomputed GO hierarchy (24K+ BP terms) drives ancestor
  **redundancy filtering** — an ancestor term is pruned when a descendant is also
  suggested, unless it is the term the KE title actually names — and supplies the term
  depth shown on each card. The information-content specificity boost is present in the
  code but **disabled by default** (`ic_weight: 0.0`); under pure-semantic ranking it
  promoted over-specific terms above the umbrella term a generic KE calls for. See the
  "GO Hierarchy" section of `docs/SCORING_CONFIG.md`.
- **KE-Centric GMT Exports**: Gene union across all approved mappings per KE for fgsea/clusterProfiler
- **Proposer Provenance**: Every approved mapping records the submitting curator's identity
- **Streamlined Confidence Assessment**: 4-question guided workflow with biological level weighting:
  - Transparent scoring algorithm (0-7.5 points) with biological level bonus
  - Automatic +1 bonus for molecular/cellular/tissue-level Key Events
  - Progressive question disclosure with collapsible answered steps
  - KE + Pathway info cards displayed alongside each assessment
  - Edit previous answers with automatic recalculation of subsequent steps
  - Real-time score calculation and detailed feedback
- **Data Exploration**: Interactive, searchable dataset browser with advanced filtering
- **Proposal System**: Community-driven change proposals with admin review workflow, on all
  three resources — submit a new pair, propose a deletion, or revise an existing mapping.
  Admins review per-resource queues and can bulk-approve in a single transaction.
- **AOP Explorer**: Interactive AOP graph (`/aop-explorer`) showing each Key Event's
  position between its Molecular Initiating Event and Adverse Outcome, with per-resource
  coverage indicators on every KE node, OECD development-status filtering, and gap filters
  that highlight where curation is still missing.
- **Real-time SPARQL Integration**: Live data from AOP-Wiki and WikiPathways endpoints
- **Versioned public REST API**: `/api/v1` serves approved mappings for all three resources
  with pagination, filtering and JSON/CSV output, documented by an OpenAPI spec and Swagger
  UI. Consumed by the [Molecular AOP Analyser](https://molaop-analyser.vhp4safety.nl).
- **Export Capabilities**: Per-resource and KE-centric GMT (split by confidence tier),
  RDF/Turtle with full curation provenance, plus CSV, TSV, JSON and Excel.
- **Citable releases**: The curated dataset is deposited to Zenodo under a persistent
  concept DOI ([10.5281/zenodo.20184643](https://doi.org/10.5281/zenodo.20184643)) that
  always resolves to the latest version, released CC0. See `docs/RELEASES.md`.

### User Experience Enhancements

- **Unified Pathway Discovery**: Step 2 organizes pathway selection into three sub-tabs:
  - **Suggested**: AI-powered pathway recommendations based on selected Key Event
  - **Search**: Full-text pathway search with fuzzy matching
  - **Browse All**: Traditional dropdown with pathway descriptions, SVG previews, and collapsible text
- **KE Context Panel**: When a Key Event is selected, an expandable panel shows:
  - AOP membership with direct links to AOP-Wiki
  - Existing WP and GO mappings with confidence levels
  - Summary badges for quick overview
- **Pathway Previews**: Inline pathway information with SVG figure previews and click-to-expand
- **Data Provenance**: Version information display for data sources (AOP-Wiki, WikiPathways) in application footer
- **Responsive Design**: Mobile-friendly layouts with responsive grid for info cards and assessment panels

### Security & Authentication

- **OAuth Sign-in**: GitHub OAuth in production today; the codebase also supports ORCID, LS Login, and SURFconext OIDC providers — they activate automatically once their respective `*_CLIENT_ID` / `*_CLIENT_SECRET` environment variables are set.
- **Provider-Prefixed Identity**: Usernames stored as `provider:name` (e.g. `github:alice`) to prevent collisions when additional providers are enabled
- **Role-based Access Control**: Admin dashboard for proposal management with proper Docker deployment support
- **CSRF Protection**: Comprehensive security against cross-site attacks
- **Rate Limiting**: API protection with intelligent throttling

### Architecture

- **Blueprint Modular Design**: Clean separation of concerns
- **Dependency Injection**: Testable and maintainable code structure
- **Configuration Management**: Environment-aware settings with Docker support
- **Health Monitoring**: System status and performance metrics
- **Centralized Error Handling**: Robust error management
- **Database Migrations**: Automatic schema updates with admin field support

## CI/CD & Quality Assurance

This project includes comprehensive GitHub Actions workflows for automated testing, quality assurance, and deployment:

### CI/CD Pipeline

- **Matrix Testing**: Python 3.10 & 3.11 compatibility
- **Automated Testing**: Full test suite with pytest and coverage reporting
- **Code Formatting**: Black code formatting and isort import sorting
- **Environment Testing**: Validates application startup and health endpoints

### Docker Build & Test

- **Multi-platform Builds**: AMD64 and ARM64 architecture support
- **Container Testing**: Automated health checks and endpoint validation
- **Docker Compose Testing**: Full stack deployment validation
- **Production Ready**: Optimized containers with proper security practices

### Code Quality
- **Linting**: Flake8, Black, isort, MyPy, and Pylint validation
- **Security Analysis**: Bandit, Safety, and Semgrep security scanning
- **Complexity Analysis**: Code complexity monitoring with Radon
- **Documentation**: Style checking and coverage validation

### Security & Compliance
- **SAST**: Static Application Security Testing with multiple tools
- **Dependency Scanning**: Automated vulnerability detection
- **Container Security**: Trivy container image scanning
- **License Compliance**: Automated license checking and SBOM generation

All workflows run automatically on push to main branch and can be triggered manually for testing.

## Quick Start

### Prerequisites
- Python 3.10 or 3.11
- Git
- GitHub account (for OAuth)

> **Note:** The initial clone is ~170 MB due to pre-computed embedding files.

### Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/marvinm2/molAOP-builder.git
   cd molAOP-builder
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate   # Linux / macOS
   # venv\Scripts\activate    # Windows
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   This installs PyTorch and sentence-transformers (~2 GB download on first install).

4. **Set up GitHub OAuth App:**
   - Go to [GitHub Developer Settings](https://github.com/settings/developers)
   - Create new OAuth App with:
     - **Application name**: `KE-WP Mapping Tool`
     - **Homepage URL**: `http://localhost:5000`
     - **Authorization callback URL**: `http://localhost:5000/callback/github`
   - Copy Client ID and Client Secret

5. **Configure environment:**
   ```bash
   cp .env.example .env
   ```

   Open `.env` in a text editor and fill in each value:

   | Variable | How to get it |
   |----------|---------------|
   | `FLASK_SECRET_KEY` | Run `python -c "import secrets; print(secrets.token_hex(32))"` and paste the output |
   | `GITHUB_CLIENT_ID` | Copy the **Client ID** from the OAuth App you created in step 4 |
   | `GITHUB_CLIENT_SECRET` | Click **Generate a new client secret** in the OAuth App and copy it |
   | `ADMIN_USERS` | Your GitHub username (comma-separated for multiple admins, e.g. `alice,bob`) |

   Example `.env` (do **not** use these values):
   ```env
   FLASK_SECRET_KEY=a3f1b9c7e8d24...   # generated hex string
   GITHUB_CLIENT_ID=Iv1.abc123def456
   GITHUB_CLIENT_SECRET=0123456789abcdef...
   ADMIN_USERS=your-github-username
   PORT=5000
   ```

   Optional variables (defaults are fine for local development):
   ```env
   FLASK_ENV=development          # or "production"
   FLASK_DEBUG=true                # set to "false" in production
   DATABASE_PATH=ke_wp_mapping.db  # path to SQLite database
   HOST=127.0.0.1                  # bind address
   RATELIMIT_STORAGE_URL=memory:// # rate-limit backend
   ```

6. **Launch the application:**
   ```bash
   chmod +x start.sh
   ./start.sh
   ```
   Or run directly with `python app.py`.

7. **Access the application:**
   - Open: http://localhost:5000
   - Click "Login" and sign in with GitHub (or with whichever additional OAuth providers you have configured — see the `*_CLIENT_ID` env vars below)
   - Start mapping KE-WP relationships!

### Run with Docker

```bash
docker pull ghcr.io/marvinm2/molaop-builder:latest
docker run -d -p 5000:5000 \
  -e FLASK_SECRET_KEY=your-secret-key \
  -e GITHUB_CLIENT_ID=your-client-id \
  -e GITHUB_CLIENT_SECRET=your-client-secret \
  -e ADMIN_USERS=your-github-username \
  ghcr.io/marvinm2/molaop-builder:latest
```

Or with Docker Compose (clone repo first):
```bash
cp .env.example .env
# Edit .env with your credentials
docker-compose up -d
```

## Architecture Overview

### Project Layout
```
app.py                  # Application factory (create_app()) — stays at root
src/
├── core/               # Models, config, schemas, error handlers
├── services/           # Container, embedding, monitoring, rate limiter
├── suggestions/        # Pathway, GO, KE gene, scoring
├── utils/              # Text, timezone utilities
├── blueprints/         # Admin, API, auth, main routes
└── exporters/          # JSON, RDF, Excel, Parquet exporters
data/                   # Pre-computed embeddings & metadata
scripts/                # Embedding pre-computation scripts
tests/                  # Pytest test suite
```

### Key Components
- **Application Factory**: Creates configured Flask instances
- **Service Container**: Manages dependencies with singleton patterns
- **Blueprint System**: Modular route organization by functionality
- **Configuration Classes**: Environment-specific settings (dev/prod/test)
- **Error Handlers**: Consistent error responses across all endpoints

## API Documentation

### Page Routes

| Endpoint | Method | Description | Authentication |
|----------|--------|-------------|----------------|
| `/` | GET | Main application page | Optional |
| `/explore` | GET | Dataset exploration interface | Optional |
| `/download` | GET | Dataset download page | Optional |
| `/ke-details` | GET | Key Event detail page | Optional |
| `/pw-details` | GET | Pathway detail page | Optional |
| `/documentation` | GET | Application documentation | Optional |
| `/login/<provider>` | GET | OAuth login (github, orcid, ls, surf) | None |
| `/logout` | GET | User logout | Required |
| `/aop-network` | GET | Interactive AOP network graph | Optional |
| `/downloads` | GET | Dataset download page | Optional |

### API Endpoints

| Endpoint | Method | Description | Rate Limit |
|----------|--------|-------------|------------|
| `/check` | POST | Validate KE-WP pair existence | General |
| `/submit` | POST | Create new KE-WP mapping | Submission |
| `/get_ke_options` | GET | Fetch Key Event options | SPARQL |
| `/get_pathway_options` | GET | Fetch pathway options | SPARQL |
| `/get_aop_options` | GET | Fetch AOP options | SPARQL |
| `/get_aop_kes/<aop_id>` | GET | Fetch Key Events for a specific AOP | SPARQL |
| `/get_data_versions` | GET | Fetch data source version info | SPARQL |
| `/suggest_pathways/<ke_id>` | GET | Pathway suggestions for a Key Event | SPARQL |
| `/search_pathways` | GET | Full-text pathway search with fuzzy matching | SPARQL |
| `/ke_genes/<ke_id>` | GET | Genes associated with a Key Event | SPARQL |
| `/api/ke_context/<ke_id>` | GET | KE context: AOPs, existing WP/GO mappings | General |
| `/api/scoring-config` | GET | KE-WP assessment scoring configuration | General |
| `/suggest_go_terms/<ke_id>` | GET | GO BP term suggestions for a Key Event | SPARQL |
| `/submit_go_mapping` | POST | Create new KE-GO mapping | Submission |
| `/check_go_entry` | POST | Check if KE-GO pair exists | General |
| `/api/go-scoring-config` | GET | KE-GO assessment scoring configuration | General |
| `/submit_proposal` | POST | Submit change proposal | Submission |

### Admin Endpoints

| Endpoint | Method | Description | Access |
|----------|--------|-------------|---------|
| `/admin/proposals` | GET | Proposal management dashboard | Admin only |
| `/admin/proposals/<id>` | GET | View proposal details | Admin only |
| `/admin/proposals/<id>/approve` | POST | Approve proposal | Admin only |
| `/admin/proposals/<id>/reject` | POST | Reject proposal | Admin only |

### Export & Data Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/export/<format>` | GET | Export dataset (csv, tsv, json, excel, rdf) |
| `/export/formats` | GET | List available export formats |
| `/dataset/metadata` | GET | Dataset metadata |
| `/dataset/versions` | GET | Dataset version history |
| `/dataset/citation` | GET | Citation information |

### Monitoring Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | System health status |
| `/metrics` | GET | Application metrics |
| `/metrics/<endpoint>` | GET | Endpoint-specific stats |

## Security Features

- **OAuth 2.0 / OIDC**: GitHub today; ORCID, LS Login, and SURFconext supported in code and activated by setting their `*_CLIENT_ID` / `*_CLIENT_SECRET` env vars
- **CSRF Protection**: All forms protected with tokens
- **Input Validation**: Marshmallow schema validation
- **SQL Injection Prevention**: Parameterized queries
- **XSS Protection**: Input sanitization and escaping
- **Rate Limiting**: Configurable request throttling
- **Session Security**: HTTPOnly, Secure, SameSite cookies

## Configuration

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `FLASK_SECRET_KEY` | Flask session encryption key | - | Yes |
| `GITHUB_CLIENT_ID` | GitHub OAuth client ID | - | Yes |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth client secret | - | Yes |
| `ADMIN_USERS` | Comma-separated admin usernames (supports provider prefix, e.g. `github:alice,orcid:0000-0001-...`) | - | Yes |
| `FLASK_ENV` | Environment mode | `development` | No |
| `FLASK_DEBUG` | Debug mode toggle | `true` | No |
| `PORT` | Server port | `5000` | No |
| `DATABASE_PATH` | SQLite database path | `ke_wp_mapping.db` | No |
| `RATELIMIT_STORAGE_URL` | Rate limiting backend | `memory://` | No |
| `ORCID_CLIENT_ID` | ORCID OAuth client ID | - | No |
| `ORCID_CLIENT_SECRET` | ORCID OAuth client secret | - | No |
| `LS_CLIENT_ID` | LS Login OAuth client ID | - | No |
| `LS_CLIENT_SECRET` | LS Login OAuth client secret | - | No |
| `SURF_CLIENT_ID` | SURFconext OAuth client ID | - | No |
| `SURF_CLIENT_SECRET` | SURFconext OAuth client secret | - | No |

### Configuration Classes

- **DevelopmentConfig**: Local development settings
- **ProductionConfig**: Production-ready configuration
- **TestingConfig**: Unit testing environment

## Testing

```bash
# Run the full test suite
PYTHONPATH=. pytest tests/ -v

# Run with test configuration
python -c "from app import create_app; app = create_app('testing')"

# Test specific endpoints
curl http://localhost:5000/health
curl http://localhost:5000/metrics

# Try the public v1 API on the live instance
curl 'https://molaop-builder.vhp4safety.nl/api/v1/mappings?ke_id=KE+149&confidence_level=high'
```

## Monitoring & Health Checks

### Health Check Response

```json
{
  "status": "healthy|degraded|unhealthy",
  "timestamp": 1754582360,
  "version": "2.7.0",
  "services": {
    "database": true,
    "oauth": true,
    "services": {
      "mapping_model": true,
      "proposal_model": true,
      "cache_model": true,
      "metrics_collector": false,
      "rate_limiter": false
    }
  }
}
```

### Metrics Available

- System resource usage
- Endpoint response times
- Request/error rates
- Database performance
- Cache hit ratios

## Development

### Local Development

```bash
# Enable debug mode
export FLASK_DEBUG=true
export FLASK_ENV=development

# Start with auto-reload
python app.py
```

### Adding New Features

1. Create new blueprint in `src/blueprints/`
2. Register in `app.py`
3. Add configuration in `config.py`
4. Update service container if needed
5. Add error handling
6. Write tests

## Troubleshooting

### Common Issues

**Port already in use:**

```bash
# Change port in .env
PORT=5001
# Update GitHub OAuth callback URL accordingly
```

**OAuth not working:**

- Verify callback URL: `http://localhost:5000/callback/github`
- Check Client ID/Secret in GitHub settings
- Ensure OAuth app is not suspended

**Database errors:**

```bash
# Reset database
rm ke_wp_mapping.db
python app.py  # Will recreate automatically
```

**Permission errors:**

```bash
# Make startup script executable
chmod +x start.sh
```

## Data Sources

- **Key Events**: [AOP-Wiki SPARQL Endpoint](https://aopwiki.rdf.bigcat-bioinformatics.org/sparql)
- **WikiPathways**: [WikiPathways SPARQL Endpoint](https://sparql.wikipathways.org/sparql)
- **Gene Ontology**: [go-basic.obo](http://purl.obolibrary.org/obo/go/go-basic.obo) (Biological Process terms; used for hierarchy precomputation)
- **GO Annotations**: [UniProt-GOA Human](https://www.ebi.ac.uk/GOA) (GO-gene associations)
- **Caching**: 24-hour cache for SPARQL responses

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes following the blueprint architecture
4. Add tests for new functionality
5. Update documentation
6. Submit a pull request

### Code Style

- Follow PEP 8 Python style guidelines
- Use type hints where applicable
- Add docstrings for all functions/classes
- Maintain separation of concerns with blueprints

## Support

- **Issues**: [GitHub Issues](https://github.com/marvinm2/molAOP-builder/issues)
- **Documentation**: This README and inline code documentation
- **Data Management Plan**: [`docs/DMP.md`](docs/DMP.md) (Horizon Europe / Science Europe template)
- **Release Runbook**: [`docs/RELEASES.md`](docs/RELEASES.md) (how to cut a new Zenodo version)
- **Contact**: [marvin.martens@maastrichtuniversity.nl]

## License

This project is licensed under the GPL-2.0 License - see the LICENSE file for details.

## Acknowledgments

- **AOP-Wiki**: Key Event data and SPARQL endpoint
- **WikiPathways**: Pathway data and SPARQL integration
- **Gene Ontology Consortium**: GO term ontology and annotations
- **UniProt-GOA**: Gene Ontology annotation database
- **Department of Translational Genomics, Maastricht University**: hosting research group
- **Flask Community**: Framework and extension ecosystem

---

**Built with modern Flask best practices and blueprint architecture for maintainable, scalable bioinformatics applications.**
