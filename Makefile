.PHONY: help install test lint run docker-build docker-run clean capture-versions backfill-versions go-hierarchy go-corpus wp-corpus wp-annotations

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

go-hierarchy:	## Build GO hierarchy data (IC scores, ancestors, depths)
	python scripts/precompute_go_hierarchy.py

go-corpus:	## Rebuild + size-filter the GO BP suggestion corpus (hierarchy -> filtered IDs -> subset embeddings/metadata)
	python scripts/precompute_go_hierarchy.py
	python scripts/subset_go_corpus.py

wp-corpus:	## Rebuild + size-filter the WikiPathways suggestion corpus (annotations -> title embeddings -> combined embeddings)
	python scripts/download_wikipathways_annotations.py
	python scripts/precompute_pathway_title_embeddings.py
	python scripts/precompute_pathway_embeddings.py

wp-annotations:	## Refresh only data/wikipathways_gene_annotations.json (gene-set sizes shown in search/suggestions)
	python scripts/download_wikipathways_annotations.py

oecd-status:	## Regenerate data/aop_oecd_status.json from AOP-Wiki RDF SPARQL (run quarterly)
	python scripts/precompute_oecd_status.py

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