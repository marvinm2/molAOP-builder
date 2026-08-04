.PHONY: help install test lint run docker-build docker-run clean capture-versions backfill-versions go-hierarchy go-corpus mf-corpus wp-corpus wp-annotations ke-corpus ke-corpus-refresh ke-metadata check-releases refresh-stale-corpora

# NEITHER GO namespace has a size ceiling, on purpose. Decided 2026-08-04.
#
# `subset_go_corpus.py` cuts go_bp_metadata.json down to the
# [MIN_GENES, MAX_GENES] = [10, 500] band. It is no longer in either target, so
# both go_bp_metadata.json (~24.5k terms) and go_mf_metadata.json (10,123) hold
# their whole namespace and every GO term stays rankable.
#
# Why the ceiling stopped making sense: it was designed under v1.4, when gene
# overlap carried real ranking weight (bp 0.45, mf 0.40) and an umbrella term
# like GO:0003824 "catalytic activity" — 5,614 genes after closure — could
# dominate gene evidence. v1.5 made ranking pure-semantic: `hybrid_weights.gene`
# is now 0.0 for BOTH namespaces, so gene overlap is a display-only chip and the
# blow-up the ceiling existed to prevent cannot reach the score at all.
#
# What remains is a candidate-set argument — more terms in the thin score band
# that #221 documents — and the answer to that is a better encoder, not a filter
# that also makes valid terms unreachable. The band cut BOTH tails: screening a
# 196-term curated KE→GO set found 62 (32%) unreachable by any route, including
# PPAR signaling (9 genes) and hepatocyte proliferation (5).
#
# This also makes `make go-corpus` agree with the weekly cron. The cron's
# gene_ontology rebuild never ran subset_go_corpus.py, so the two paths produced
# different corpora and a manual rebuild silently re-narrowed what the cron had
# widened — which is how the discrepancy in #284 arose.
#
# subset_go_corpus.py is kept, unreferenced, so the band can be reapplied by
# hand. If either namespace ever moves off gene: 0.0, revisit this.

help:		## Show this help
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:	## Install dependencies
	pip install -r requirements.txt

test:		## Run tests
	pytest

test-cov:	## Run tests with coverage
	pytest --cov=. --cov-report=html

lint:		## Run linting (placeholder for future linting tools)
	@echo "Linting (add flake8/black/isort when ready)"

run:		## Run the application
	python app.py

run-prod:	## Run with gunicorn (production)
	gunicorn --bind 0.0.0.0:5000 --workers 4 app:app

docker-build:	## Build Docker image
	docker build -t ke-wp-mapping .

docker-run:	## Run Docker container
	docker run -p 5000:5000 --env-file .env ke-wp-mapping

docker-compose-up:	## Start with docker-compose
	docker-compose up -d

docker-compose-down:	## Stop docker-compose
	docker-compose down

migrate:	## Run database migration
	python migrate_csv_to_db.py

go-hierarchy:	## Build GO hierarchy data (IC scores, ancestors, depths) + the full-namespace search index
	python scripts/precompute_go_hierarchy.py

go-corpus:	## Rebuild the GO BP corpus (hierarchy + search index -> embeddings). Not subsetted — see below
	python scripts/precompute_go_hierarchy.py
	python scripts/download_go_annotations.py
	python scripts/precompute_go_embeddings.py

mf-corpus:	## Rebuild the GO MF corpus (annotations -> hierarchy/IC -> embeddings). Not subsetted — see below
	python scripts/download_go_annotations.py --namespace mf
	python scripts/precompute_go_hierarchy.py --namespace mf
	python scripts/precompute_go_embeddings.py --namespace mf

wp-corpus:	## Rebuild the WikiPathways corpus (annotations -> title embeddings -> combined embeddings). Metadata holds every pathway; the size filter marks which are rankable
	python scripts/download_wikipathways_annotations.py
	python scripts/precompute_pathway_title_embeddings.py
	python scripts/precompute_pathway_embeddings.py

ke-corpus:	## Rebuild the KE embeddings (title-only + with-description) from the existing ke_metadata.json snapshot
	python scripts/precompute_ke_embeddings.py

ke-corpus-refresh:	## As ke-corpus, but ALSO re-fetch Key Events from AOP-Wiki and rewrite ke_metadata.json (moves the KE snapshot)
	python scripts/precompute_ke_embeddings.py --refresh-metadata

ke-metadata:	## Refresh ONLY the KE snapshot from AOP-Wiki (no embeddings, no BioBERT) — the cheap dropdown refresh
	python scripts/precompute_ke_embeddings.py --metadata-only

wp-annotations:	## Refresh data/wikipathways_gene_annotations.json + wikipathways_gene_counts.json (gene-set sizes shown in search/suggestions)
	python scripts/download_wikipathways_annotations.py

oecd-status:	## Regenerate data/aop_oecd_status.json from AOP-Wiki RDF SPARQL (run quarterly)
	python scripts/precompute_oecd_status.py

check-releases:	## Report which upstream sources have released since the deployed corpus was built
	python scripts/check_source_releases.py

refresh-stale-corpora:	## Rebuild the corpus for every source that has released since (what the weekly cron runs)
	python scripts/check_source_releases.py --rebuild

capture-versions:	## Refresh data/source_versions.json from WP / GO / Reactome / AOP-Wiki
	python scripts/capture_source_versions.py

backfill-versions:	## One-shot: stamp current snapshot's versions onto NULL columns on all existing mappings (idempotent; --dry-run via DRY=1)
	python scripts/backfill_source_versions.py $(if $(DRY),--dry-run,)

clean:		## Clean up generated files
	rm -rf __pycache__
	rm -rf .pytest_cache
	rm -rf htmlcov
	rm -rf .coverage
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete

setup-dev:	## Setup development environment
	python -m venv venv
	bash -c "source venv/bin/activate && pip install -r requirements.txt"
	cp .env.example .env
	@echo "Don't forget to edit .env with your actual values!"

backup-db:	## Backup the database
	cp ke_wp_mapping.db ke_wp_mapping.db.backup.$(shell date +%Y%m%d_%H%M%S)

restore-db:	## Restore database from backup (requires BACKUP_FILE variable)
	@if [ -z "$(BACKUP_FILE)" ]; then echo "Usage: make restore-db BACKUP_FILE=backup.db"; exit 1; fi
	cp $(BACKUP_FILE) ke_wp_mapping.db