"""
Service Container for Dependency Injection
Provides centralized management of application services and dependencies
"""
import json
import logging
import os

from authlib.integrations.flask_client import OAuth

from src import PROJECT_ROOT
from src.core.models import CacheModel, Database, GoMappingModel, GoProposalModel, GuestCodeModel, KeDescriptionOverrideModel, MappingModel, ProposalModel, ReactomeMappingModel, ReactomeProposalModel
from src.services.monitoring import MetricsCollector
from src.suggestions.pathway import PathwaySuggestionService
from src.suggestions.go import GoSuggestionService
from src.suggestions.reactome import ReactomeSuggestionService
from src.services.rate_limiter import RateLimiter
from src.core.config_loader import ConfigLoader
from src.services.embedding import BiologicalEmbeddingService

logger = logging.getLogger(__name__)

# Multi-provider OAuth configuration.
# Each entry maps a provider name to its env-var keys and OIDC discovery URL.
# Providers are only registered when their CLIENT_ID and CLIENT_SECRET env vars are set.
# NOTE: default_discovery_url values below are TEST/sandbox endpoints. Production discovery
# URLs are supplied via the *_DISCOVERY_URL env vars set on tgx1 — never hard-coded here.
PROVIDER_CONFIGS = {
    "orcid": {
        "env_client_id": "ORCID_CLIENT_ID",
        "env_client_secret": "ORCID_CLIENT_SECRET",
        "env_discovery_url": "ORCID_DISCOVERY_URL",
        "default_discovery_url": "https://sandbox.orcid.org/.well-known/openid-configuration",
        "client_kwargs": {"scope": "openid"},
    },
    "ls": {
        "env_client_id": "LS_CLIENT_ID",
        "env_client_secret": "LS_CLIENT_SECRET",
        "env_discovery_url": "LS_DISCOVERY_URL",
        "default_discovery_url": "https://oidc.pilot.lifescienceid.org/oauth2/.well-known/openid-configuration",
        "client_kwargs": {"scope": "openid profile email"},
    },
    "surf": {
        "env_client_id": "SURF_CLIENT_ID",
        "env_client_secret": "SURF_CLIENT_SECRET",
        "env_discovery_url": "SURF_DISCOVERY_URL",
        "default_discovery_url": "https://connect.test.surfconext.nl/.well-known/openid-configuration",
        "client_kwargs": {"scope": "openid"},
    },
}


class ServiceContainer:
    """
    Dependency injection container for managing application services

    This container follows the singleton pattern for database connections
    and provides factory methods for creating service instances.
    """

    def __init__(self, config):
        self.config = config
        self._database = None
        self._mapping_model = None
        self._proposal_model = None
        self._cache_model = None
        self._metrics_collector = None
        self._rate_limiter = None
        self._pathway_suggestion_service = None
        self._go_suggestion_service = None
        self._reactome_suggestion_service = None
        self._go_mapping_model = None
        self._go_proposal_model = None
        self._guest_code_model = None
        self._ke_override_model = None
        self._reactome_mapping_model = None
        self._reactome_proposal_model = None
        self._oauth = None
        self._github_client = None
        self._provider_clients = {}
        self._scoring_config = None
        self._embedding_service = None
        self._ke_metadata = None
        self._pathway_metadata = None
        self._ke_aop_membership = None
        self._ke_metadata_index = None
        self._ker_adjacency = None
        self._go_hierarchy = None
        self._go_bp_metadata = None
        self._go_mf_metadata = None
        self._source_versions = None  # Phase C — lazy-loaded data/source_versions.json

        logger.info("Service container initialized")

    @property
    def database(self) -> Database:
        """Get or create database instance (singleton)"""
        if self._database is None:
            self._database = Database(self.config.DATABASE_PATH)
            logger.info(f"Database instance created: {self.config.DATABASE_PATH}")
        return self._database

    @property
    def mapping_model(self) -> MappingModel:
        """Get or create mapping model instance"""
        if self._mapping_model is None:
            self._mapping_model = MappingModel(self.database)
            logger.debug("MappingModel instance created")
        return self._mapping_model

    @property
    def proposal_model(self) -> ProposalModel:
        """Get or create proposal model instance"""
        if self._proposal_model is None:
            self._proposal_model = ProposalModel(self.database)
            logger.debug("ProposalModel instance created")
        return self._proposal_model

    @property
    def cache_model(self) -> CacheModel:
        """Get or create cache model instance"""
        if self._cache_model is None:
            self._cache_model = CacheModel(self.database)
            logger.debug("CacheModel instance created")
        return self._cache_model

    @property
    def metrics_collector(self) -> MetricsCollector:
        """Get or create metrics collector instance"""
        if self._metrics_collector is None:
            self._metrics_collector = MetricsCollector(self.config.DATABASE_PATH)
            logger.debug("MetricsCollector instance created")
        return self._metrics_collector

    @property
    def rate_limiter(self) -> RateLimiter:
        """Get or create rate limiter instance"""
        if self._rate_limiter is None:
            self._rate_limiter = RateLimiter(self.config.DATABASE_PATH)
            logger.debug("RateLimiter instance created")
        return self._rate_limiter

    @property
    def scoring_config(self):
        """Get or load scoring configuration"""
        if self._scoring_config is None:
            try:
                self._scoring_config = ConfigLoader.load_config()
                logger.info("Scoring configuration loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load scoring config: {e}")
                self._scoring_config = ConfigLoader.get_default_config()
                logger.info("Using default scoring configuration")
        return self._scoring_config

    @property
    def ke_metadata(self):
        """Load and cache KE metadata from pre-computed JSON file"""
        if self._ke_metadata is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'ke_metadata.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._ke_metadata = json.load(f)
                    logger.info("Loaded %d KE metadata entries from %s", len(self._ke_metadata), path)
                except Exception as e:
                    logger.warning("Failed to load ke_metadata.json: %s", e)
        return self._ke_metadata

    @property
    def pathway_metadata(self):
        """Load and cache pathway metadata from pre-computed JSON file"""
        if self._pathway_metadata is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'pathway_metadata.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._pathway_metadata = json.load(f)
                    logger.info("Loaded %d pathway metadata entries from %s", len(self._pathway_metadata), path)
                except Exception as e:
                    logger.warning("Failed to load pathway_metadata.json: %s", e)
        return self._pathway_metadata

    @property
    def ke_aop_membership(self):
        """Load and cache KE AOP membership from pre-computed JSON file"""
        if self._ke_aop_membership is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'ke_aop_membership.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._ke_aop_membership = json.load(f)
                    logger.info(
                        "Loaded KE AOP membership for %d KEs from %s",
                        len(self._ke_aop_membership), path,
                    )
                except Exception as e:
                    logger.warning("Failed to load ke_aop_membership.json: %s", e)
        return self._ke_aop_membership

    @property
    def source_versions(self):
        """
        Load and cache the upstream source-versions manifest.

        Phase C of source-data versioning (DMP §7). The manifest is written by
        scripts/capture_source_versions.py and travels with the snapshot at
        data/source_versions.json. New approvals are stamped with the current
        snapshot's versions via `source_version_fields_for(resource)` below.

        Returns the parsed manifest dict, or `{}` if the file is missing or
        unreadable — that case logs a warning but does not fail startup
        (mappings just get NULL version columns until the manifest exists).
        """
        if self._source_versions is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'source_versions.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._source_versions = json.load(f)
                    sources = self._source_versions.get('sources', {})
                    ok = [k for k, v in sources.items() if v.get('status') == 'ok']
                    logger.info(
                        "Loaded source_versions.json: %d/%d sources OK (%s)",
                        len(ok), len(sources), ', '.join(sorted(ok)) or 'none',
                    )
                except Exception as e:
                    logger.warning("Failed to load source_versions.json: %s", e)
                    self._source_versions = {}
            else:
                logger.warning(
                    "source_versions.json not found at %s — new approvals will "
                    "have NULL version columns until the manifest is generated "
                    "(run `make capture-versions`).",
                    path,
                )
                self._source_versions = {}
        return self._source_versions

    def source_version_fields_for(self, resource: str) -> dict:
        """
        Return the kwargs dict to stamp on a newly-approved mapping of `resource`.

        `resource` is one of {"wp", "go", "reactome"}. The returned dict contains
        column-name → value entries suitable for passing directly into the
        relevant `create_mapping` / `update_mapping` / `create_approved_mapping`
        call. Sources whose manifest status is not "ok" (or missing entirely)
        are silently omitted, so the corresponding column stays NULL.

        Every resource also stamps `aopwiki_snapshot_date` (the KE side of the
        mapping is anchored in AOP-Wiki regardless of the molecular resource).

        Returns `{}` if the manifest is empty or all relevant sources are
        unknown — the caller can `**unpack` the result safely either way.
        """
        manifest = self.source_versions
        sources = manifest.get('sources', {}) if manifest else {}

        fields = {}
        aopwiki = sources.get('aopwiki', {})
        if aopwiki.get('status') == 'ok' and aopwiki.get('snapshot_date'):
            fields['aopwiki_snapshot_date'] = aopwiki['snapshot_date']

        if resource == 'wp':
            wp = sources.get('wikipathways', {})
            if wp.get('status') == 'ok' and wp.get('release_date'):
                fields['wp_release_date'] = wp['release_date']
        elif resource == 'go':
            go = sources.get('gene_ontology', {})
            if go.get('status') == 'ok' and go.get('release_date'):
                fields['go_release_date'] = go['release_date']
        elif resource == 'reactome':
            rx = sources.get('reactome', {})
            if rx.get('status') == 'ok':
                if rx.get('release_version'):
                    fields['reactome_release_version'] = rx['release_version']
                if rx.get('release_date'):
                    fields['reactome_release_date'] = rx['release_date']
        else:
            raise ValueError(
                f"Unknown resource {resource!r}; expected 'wp', 'go', or 'reactome'"
            )
        return fields

    @property
    def ker_adjacency(self):
        """Load and cache KER adjacency data from pre-computed JSON file"""
        if self._ker_adjacency is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'ker_adjacency.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._ker_adjacency = json.load(f)
                    logger.info(
                        "Loaded KER adjacency for %d AOPs from %s",
                        len([k for k in self._ker_adjacency if k != '_metadata']), path,
                    )
                except Exception as e:
                    logger.warning("Failed to load ker_adjacency.json: %s", e)
        return self._ker_adjacency

    @property
    def ke_metadata_index(self):
        """Build and cache a dict-index of KE metadata keyed by KElabel for O(1) lookup"""
        if self._ke_metadata_index is None:
            meta = self.ke_metadata
            self._ke_metadata_index = (
                {ke["KElabel"]: ke for ke in meta} if meta else {}
            )
            logger.info(
                "Built KE metadata index with %d entries", len(self._ke_metadata_index)
            )
        return self._ke_metadata_index

    @property
    def go_hierarchy(self):
        """Load and cache GO hierarchy data from pre-computed JSON file"""
        if self._go_hierarchy is None:
            # Filename matches precompute_go_hierarchy.py's actual output (go_{ns}_hierarchy.json).
            path = os.path.join(PROJECT_ROOT, 'data', 'go_bp_hierarchy.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._go_hierarchy = json.load(f)
                    logger.info(
                        "Loaded GO hierarchy for %d terms from %s",
                        len(self._go_hierarchy), path,
                    )
                except Exception as e:
                    logger.warning("Failed to load go_bp_hierarchy.json: %s", e)
                    self._go_hierarchy = {}
            else:
                logger.info("go_bp_hierarchy.json not found at %s — GO depth/IC will be unavailable", path)
                self._go_hierarchy = {}
        return self._go_hierarchy

    @property
    def go_bp_metadata(self):
        """Load and cache GO biological process metadata from pre-computed JSON file"""
        if self._go_bp_metadata is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'go_bp_metadata.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._go_bp_metadata = json.load(f)
                    logger.info(
                        "Loaded GO BP metadata for %d terms from %s",
                        len(self._go_bp_metadata), path,
                    )
                except Exception as e:
                    logger.warning("Failed to load go_bp_metadata.json: %s", e)
                    self._go_bp_metadata = {}
            else:
                logger.info("go_bp_metadata.json not found at %s — GO definitions will be unavailable", path)
                self._go_bp_metadata = {}
        return self._go_bp_metadata

    @property
    def go_mf_metadata(self):
        """Load and cache GO molecular function metadata from pre-computed JSON file"""
        if self._go_mf_metadata is None:
            path = os.path.join(PROJECT_ROOT, 'data', 'go_mf_metadata.json')
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._go_mf_metadata = json.load(f)
                    logger.info(
                        "Loaded GO MF metadata for %d terms from %s",
                        len(self._go_mf_metadata), path,
                    )
                except Exception as e:
                    logger.warning("Failed to load go_mf_metadata.json: %s", e)
                    self._go_mf_metadata = {}
            else:
                logger.info("go_mf_metadata.json not found at %s — GO MF definitions will be unavailable", path)
                self._go_mf_metadata = {}
        return self._go_mf_metadata

    @property
    def pathway_suggestion_service(self) -> PathwaySuggestionService:
        """Get or create pathway suggestion service instance"""
        if self._pathway_suggestion_service is None:
            scoring_config = self.scoring_config
            self._pathway_suggestion_service = PathwaySuggestionService(
                self.cache_model,
                config=scoring_config,
                embedding_service=self.embedding_service,
                ke_override_model=self.ke_override_model,
            )
            logger.debug("PathwaySuggestionService instance created with config")
        return self._pathway_suggestion_service

    @property
    def go_mapping_model(self) -> GoMappingModel:
        """Get or create GO mapping model instance"""
        if self._go_mapping_model is None:
            self._go_mapping_model = GoMappingModel(self.database)
            logger.debug("GoMappingModel instance created")
        return self._go_mapping_model

    @property
    def go_proposal_model(self) -> GoProposalModel:
        """Get or create GO proposal model instance"""
        if self._go_proposal_model is None:
            self._go_proposal_model = GoProposalModel(self.database)
            logger.debug("GoProposalModel instance created")
        return self._go_proposal_model

    @property
    def guest_code_model(self) -> GuestCodeModel:
        """Get or create guest code model instance"""
        if self._guest_code_model is None:
            self._guest_code_model = GuestCodeModel(self.database)
            logger.debug("GuestCodeModel instance created")
        return self._guest_code_model

    @property
    def ke_override_model(self) -> KeDescriptionOverrideModel:
        """Get or create KE description override model instance"""
        if self._ke_override_model is None:
            self._ke_override_model = KeDescriptionOverrideModel(self.database)
            logger.debug("KeDescriptionOverrideModel instance created")
        return self._ke_override_model

    @property
    def reactome_mapping_model(self) -> ReactomeMappingModel:
        """Get or create Reactome mapping model instance"""
        if self._reactome_mapping_model is None:
            self._reactome_mapping_model = ReactomeMappingModel(self.database)
            logger.debug("ReactomeMappingModel instance created")
        return self._reactome_mapping_model

    @property
    def reactome_proposal_model(self) -> ReactomeProposalModel:
        """Get or create Reactome proposal model instance"""
        if self._reactome_proposal_model is None:
            self._reactome_proposal_model = ReactomeProposalModel(self.database)
            logger.debug("ReactomeProposalModel instance created")
        return self._reactome_proposal_model

    @property
    def go_suggestion_service(self) -> GoSuggestionService:
        """Get or create GO suggestion service instance"""
        if self._go_suggestion_service is None:
            try:
                scoring_config = self.scoring_config
                self._go_suggestion_service = GoSuggestionService(
                    cache_model=self.cache_model,
                    config=scoring_config,
                    embedding_service=self.embedding_service,
                    ke_override_model=self.ke_override_model,
                )
                logger.info("GoSuggestionService instance created")
            except Exception as e:
                logger.error(f"Failed to initialize GO suggestion service: {e}")
                self._go_suggestion_service = None
        return self._go_suggestion_service

    @property
    def reactome_suggestion_service(self) -> ReactomeSuggestionService:
        """Get or create Reactome suggestion service instance"""
        if self._reactome_suggestion_service is None:
            try:
                scoring_config = self.scoring_config
                self._reactome_suggestion_service = ReactomeSuggestionService(
                    cache_model=self.cache_model,
                    config=scoring_config,
                    embedding_service=self.embedding_service,
                    ke_override_model=self.ke_override_model,
                )
                logger.info("ReactomeSuggestionService instance created")
            except Exception as e:
                logger.error(f"Failed to initialize Reactome suggestion service: {e}")
                self._reactome_suggestion_service = None
        return self._reactome_suggestion_service

    @property
    def embedding_service(self) -> BiologicalEmbeddingService:
        """Get or create embedding service instance"""
        if self._embedding_service is None:
            try:
                # Check if embeddings are enabled
                embedding_config = getattr(
                    self.scoring_config.pathway_suggestion,
                    'embedding_based_matching',
                    None
                )

                enabled = getattr(embedding_config, 'enabled', False) if embedding_config else False

                if enabled:
                    model_name = getattr(
                        embedding_config,
                        'model',
                        'dmis-lab/biobert-base-cased-v1.2'
                    )
                    precomputed_path = getattr(
                        embedding_config,
                        'precomputed_embeddings',
                        'data/pathway_embeddings.npy'
                    )
                    precomputed_ke_path = getattr(
                        embedding_config,
                        'precomputed_ke_embeddings',
                        'data/ke_embeddings.npy'
                    )

                    # Extract score transformation config
                    score_transform = getattr(
                        embedding_config,
                        'score_transformation',
                        None
                    )
                    score_transform_config = None
                    if score_transform:
                        score_transform_config = {
                            'method': getattr(score_transform, 'method', 'power'),
                            'power_exponent': getattr(score_transform, 'power_exponent', 2.5),
                            'scale_factor': getattr(score_transform, 'scale_factor', 0.75),
                            'output_min': getattr(score_transform, 'output_min', 0.0),
                            'output_max': getattr(score_transform, 'output_max', 0.70),
                            # Read from embedding_config, not score_transform
                            'skip_precomputed_for_titles': getattr(embedding_config, 'skip_precomputed_for_titles', True)
                        }
                        logger.info(f"Score transformation config: {score_transform_config}")

                    # Extract title weight (higher = more emphasis on title matching)
                    title_weight = getattr(embedding_config, 'title_weight', 0.85)
                    logger.info(f"Title weight: {title_weight}")

                    # Extract entity extraction config
                    entity_extract = getattr(embedding_config, 'entity_extraction', None)
                    entity_extract_config = None
                    if entity_extract:
                        entity_extract_config = {
                            'enabled': getattr(entity_extract, 'enabled', True),
                            'min_entity_length': getattr(entity_extract, 'min_entity_length', 3),
                            'include_numbers': getattr(entity_extract, 'include_numbers', True),
                            'biological_terms_only': getattr(entity_extract, 'biological_terms_only', False)
                        }
                        logger.info(f"Entity extraction: {entity_extract_config}")

                    self._embedding_service = BiologicalEmbeddingService(
                        model_name=model_name,
                        use_gpu=True,
                        precomputed_embeddings_path=precomputed_path,
                        precomputed_ke_embeddings_path=precomputed_ke_path,
                        score_transform_config=score_transform_config,
                        title_weight=title_weight,
                        entity_extract_config=entity_extract_config
                    )
                    logger.info("Embedding service initialized")
                else:
                    logger.info("Embedding service disabled by config")
                    self._embedding_service = None

            except Exception as e:
                logger.error(f"Failed to initialize embedding service: {e}")
                self._embedding_service = None

        return self._embedding_service

    def init_oauth(self, app) -> OAuth:
        """Initialize OAuth with the Flask app.

        Registers GitHub (always) plus any OIDC providers whose env vars are set.
        """
        if self._oauth is None:
            self._oauth = OAuth(app)

            # GitHub is always registered
            self._github_client = self._oauth.register(
                name="github",
                client_id=self.config.GITHUB_CLIENT_ID,
                client_secret=self.config.GITHUB_CLIENT_SECRET,
                access_token_url="https://github.com/login/oauth/access_token",
                authorize_url="https://github.com/login/oauth/authorize",
                api_base_url="https://api.github.com/",
                client_kwargs={"scope": "user:email"},
            )
            self._provider_clients["github"] = self._github_client
            logger.info("OAuth initialized with GitHub")

            # Register optional OIDC providers
            for provider, cfg in PROVIDER_CONFIGS.items():
                client_id = os.getenv(cfg["env_client_id"])
                client_secret = os.getenv(cfg["env_client_secret"])
                if not client_id or not client_secret:
                    continue

                discovery_url = os.getenv(
                    cfg["env_discovery_url"], cfg["default_discovery_url"]
                )
                client = self._oauth.register(
                    name=provider,
                    client_id=client_id,
                    client_secret=client_secret,
                    server_metadata_url=discovery_url,
                    client_kwargs=cfg["client_kwargs"],
                )
                self._provider_clients[provider] = client
                logger.info("OAuth provider registered: %s", provider)

        return self._oauth

    @property
    def provider_clients(self) -> dict:
        """Return dict of registered OAuth provider clients"""
        return self._provider_clients

    @property
    def github_client(self):
        """Get GitHub OAuth client"""
        if self._github_client is None:
            raise RuntimeError("OAuth not initialized. Call init_oauth() first.")
        return self._github_client

    def cleanup(self):
        """Cleanup resources on shutdown"""
        if self._metrics_collector:
            # Cleanup any background threads in metrics collector
            logger.info("Cleaning up metrics collector")

        if self._database:
            # Database connections are automatically closed in models
            logger.info("Database cleanup completed")

        logger.info("Service container cleanup completed")

    def get_health_status(self) -> dict:
        """Get health status of all services"""
        status = {
            "database": False,
            "oauth": False,
            # Top-level boolean on purpose: app.py's health route aggregates
            # with all(health_status.values()), and the nested "services" dict
            # below is unconditionally truthy — a flag placed inside it could
            # never flip the reported status (#209).
            "embeddings_ok": True,
            "services": {
                "mapping_model": self._mapping_model is not None,
                "proposal_model": self._proposal_model is not None,
                "cache_model": self._cache_model is not None,
                "metrics_collector": self._metrics_collector is not None,
                "rate_limiter": self._rate_limiter is not None,
                "pathway_suggestion_service": self._pathway_suggestion_service is not None,
                "go_suggestion_service": self._go_suggestion_service is not None,
            },
        }

        # Test database connection
        try:
            conn = self.database.get_connection()
            conn.execute("SELECT 1")
            conn.close()
            status["database"] = True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")

        # Test OAuth configuration
        try:
            status["oauth"] = bool(
                self.config.GITHUB_CLIENT_ID and self.config.GITHUB_CLIENT_SECRET
            )
        except Exception as e:
            logger.error(f"OAuth health check failed: {e}")

        # Report precomputed-artifact degradation. Read the already-constructed
        # service only — never touch the lazy property here, or a health check
        # would load BioBERT.
        try:
            degraded = getattr(self._embedding_service, "embeddings_degraded", [])
            status["embeddings_ok"] = not degraded
            status["services"]["degraded_embedding_artifacts"] = list(degraded)
        except Exception as e:
            logger.error(f"Embedding health check failed: {e}")

        return status
