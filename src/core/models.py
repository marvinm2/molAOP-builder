"""
Database models for KE-WP Mapping Application
"""
import hashlib
import logging
import secrets
import sqlite3
import uuid as uuid_lib
from datetime import datetime
from typing import Dict, List, Optional

from src.utils.text import detect_go_direction

logger = logging.getLogger(__name__)

# Reactome proposal carry fields — columns copied from proposal to mapping at admin approval time.
# Phase 25 admin route reads this constant; change it if schema changes.
# Phase 34 (ASMT-10): the four assessment-question answers ride the same carry path on approve.
# Imported by ReactomeMappingModel.create_approved_mapping in Plan 02.
# KE-WP wired differently per WP/Reactome asymmetry (Plan 03).
REACTOME_PROPOSAL_CARRY_FIELDS = (
    'pathway_name',
    'species',
    'suggestion_score',
    'confidence_level',
    # Phase 34 (ASMT-10): assessment-question answers — wire-up in Plan 02
    'proposed_relationship',
    'proposed_basis',
    'proposed_specificity',
    'proposed_coverage',
)


def _classify_assessment_version(
    proposed_relationship: Optional[str],
    proposed_basis: Optional[str],
    proposed_specificity: Optional[str],
    proposed_coverage: Optional[str],
) -> str:
    """Phase 34 model-layer rule: any non-NULL assessment answer => 'v2'; else 'v1'.

    CONTEXT.md locks: no completeness validation — partial submissions during the
    Phase 34->37 transition window are still 'v2'. Used by both WP create_mapping/
    update_mapping and Reactome create_approved_mapping/create_mapping.
    """
    if any(v is not None for v in (
        proposed_relationship, proposed_basis,
        proposed_specificity, proposed_coverage,
    )):
        return "v2"
    return "v1"


class Database:
    def __init__(self, db_path: str = "ke_wp_mapping.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        """Get database connection with WAL mode, busy timeout, and row factory."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_db(self):
        """Initialize database with required tables"""
        conn = self.get_connection()
        try:
            # Create mappings table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ke_id TEXT NOT NULL,
                    ke_title TEXT NOT NULL,
                    wp_id TEXT NOT NULL,
                    wp_title TEXT NOT NULL,
                    connection_type TEXT NOT NULL DEFAULT 'undefined',
                    confidence_level TEXT NOT NULL DEFAULT 'low',
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ke_id, wp_id)
                )
            """
            )

            # Create proposals table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mapping_id INTEGER,
                    user_name TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    user_affiliation TEXT NOT NULL,
                    github_username TEXT,
                    proposed_delete BOOLEAN DEFAULT FALSE,
                    proposed_confidence TEXT,
                    proposed_connection_type TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mapping_id) REFERENCES mappings (id)
                )
            """
            )

            # Create cache table for SPARQL responses
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sparql_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    response_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    UNIQUE(endpoint, query_hash)
                )
            """
            )

            # Create indexes for performance
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mappings_ke_id ON mappings(ke_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mappings_wp_id ON mappings(wp_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mappings_created_by ON mappings(created_by)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_proposals_mapping_id ON proposals(mapping_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_expires ON sparql_cache(expires_at)"
            )

            # Create KE-GO mappings table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ke_go_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ke_id TEXT NOT NULL,
                    ke_title TEXT NOT NULL,
                    go_id TEXT NOT NULL,
                    go_name TEXT NOT NULL,
                    connection_type TEXT NOT NULL DEFAULT 'related',
                    confidence_level TEXT NOT NULL DEFAULT 'low',
                    evidence_code TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ke_id, go_id)
                )
            """
            )

            # Create KE-GO proposals table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ke_go_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mapping_id INTEGER,
                    user_name TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    user_affiliation TEXT NOT NULL,
                    github_username TEXT,
                    proposed_delete BOOLEAN DEFAULT FALSE,
                    proposed_confidence TEXT,
                    proposed_connection_type TEXT,
                    status TEXT DEFAULT 'pending',
                    admin_notes TEXT,
                    approved_by TEXT,
                    approved_at TIMESTAMP,
                    rejected_by TEXT,
                    rejected_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mapping_id) REFERENCES ke_go_mappings(id)
                )
            """
            )

            # Create indexes for GO mappings
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_go_mappings_ke_id ON ke_go_mappings(ke_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_go_mappings_go_id ON ke_go_mappings(go_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_go_proposals_mapping_id ON ke_go_proposals(mapping_id)"
            )

            # Create guest codes table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guest_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    max_uses INTEGER DEFAULT 1,
                    use_count INTEGER DEFAULT 0,
                    is_revoked BOOLEAN DEFAULT FALSE,
                    revoked_at TIMESTAMP,
                    revoked_by TEXT
                )
            """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_guest_codes_code ON guest_codes(code)"
            )

            # Create KE description overrides table (Phase 17)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ke_description_overrides (
                    ke_id TEXT PRIMARY KEY,
                    description_disabled INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Create KE-Reactome mappings table (Phase 24)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ke_reactome_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ke_id TEXT NOT NULL,
                    ke_title TEXT NOT NULL,
                    reactome_id TEXT NOT NULL,
                    pathway_name TEXT NOT NULL,
                    species TEXT DEFAULT 'Homo sapiens',
                    confidence_level TEXT NOT NULL DEFAULT 'low',
                    suggestion_score REAL,
                    proposed_by TEXT,
                    created_by TEXT,
                    uuid TEXT,
                    approved_by_curator TEXT,
                    approved_at_curator TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ke_id, reactome_id)
                )
            """
            )

            # Create KE-Reactome proposals table (Phase 24)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ke_reactome_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mapping_id INTEGER,
                    user_name TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    user_affiliation TEXT NOT NULL,
                    provider_username TEXT,
                    proposed_delete BOOLEAN DEFAULT FALSE,
                    proposed_confidence TEXT,
                    proposed_connection_type TEXT,
                    status TEXT DEFAULT 'pending',
                    admin_notes TEXT,
                    approved_by TEXT,
                    approved_at TIMESTAMP,
                    rejected_by TEXT,
                    rejected_at TIMESTAMP,
                    uuid TEXT,
                    suggestion_score REAL,
                    is_stale BOOLEAN DEFAULT FALSE,
                    ke_id TEXT,
                    ke_title TEXT,
                    reactome_id TEXT,
                    pathway_name TEXT,
                    species TEXT,
                    new_pair_confidence_level TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mapping_id) REFERENCES ke_reactome_mappings(id)
                )
            """
            )

            # Create indexes for Reactome tables
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reactome_mappings_ke_id ON ke_reactome_mappings(ke_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reactome_mappings_reactome_id ON ke_reactome_mappings(reactome_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reactome_proposals_mapping_id ON ke_reactome_proposals(mapping_id)"
            )

            # Migrate proposals table to add admin fields if needed
            self._migrate_proposals_admin_fields(conn)

            # Migrate mappings table to add updated_by field if needed
            self._migrate_mappings_updated_by_field(conn)

            # Migrate mapping tables to add uuid and provenance columns (Phase 2)
            self._migrate_mappings_uuid_and_provenance(conn)
            self._migrate_go_mappings_uuid_and_provenance(conn)

            # Migrate proposal tables to add phase 2 fields (Phase 2)
            self._migrate_proposals_phase2_fields(conn)
            self._migrate_go_proposals_phase2_fields(conn)

            # Migrate mappings/go_mappings tables to add Phase 3 columns
            self._migrate_mappings_suggestion_score(conn)
            self._migrate_go_mappings_suggestion_score(conn)
            self._migrate_go_mappings_go_namespace(conn)

            # Migrate proposals table to add new-pair fields (Phase 3 gap closure)
            self._migrate_proposals_new_pair_fields(conn)

            # Migrate ke_go_proposals table to add new-pair fields (Phase 7)
            self._migrate_go_proposals_new_pair_fields(conn)

            # Migrate mappings/ke_go_mappings to add proposed_by provenance column
            self._migrate_mappings_proposed_by(conn)
            self._migrate_go_mappings_proposed_by(conn)

            # Migrate identity columns to provider-prefixed format (Phase 14)
            self._migrate_provider_prefix(conn)

            # Migrate ke_go_mappings to add go_direction column (Phase 18)
            self._migrate_go_mappings_go_direction(conn)

            # Migrate ke_go_proposals and ke_go_mappings for dimension scores (Phase 19)
            self._migrate_go_proposals_dimension_scores(conn)
            self._migrate_go_mappings_dimension_scores(conn)

            # Phase 34 (ASMT-01/02/03): assessment-question persistence on WP + Reactome
            # proposal/mapping tables. Idempotent — PRAGMA-guarded ALTER TABLE.
            self._migrate_proposals_assessment_fields(conn)
            self._migrate_mappings_assessment_fields(conn)
            self._migrate_reactome_proposals_assessment_fields(conn)
            self._migrate_reactome_mappings_assessment_fields(conn)

            # Migrate ke_go_proposals to add go_namespace column (Phase 21)
            self._migrate_proposals_go_namespace(conn)

            # Phase 25 review H-2: partial-unique index on pending Reactome
            # proposals so concurrent submits cannot create duplicate
            # pending rows for the same (ke_id, reactome_id) pair.
            self._migrate_reactome_proposals_pending_unique_index(conn)

            # Phase 32 review H-2 port: same partial-unique guarantee for
            # the WP `proposals` table, with a pre-migration cleanup pass
            # (legacy table predates this constraint by many phases).
            self._migrate_proposals_pending_unique_index(conn)

            # Phase 32 review H-2 port (GO sibling): same partial-unique
            # guarantee for `ke_go_proposals`, with a parallel pre-migration
            # cleanup pass.
            self._migrate_go_proposals_pending_unique_index(conn)

            # Source-data versioning (DMP §7, Phase B):
            # add nullable upstream-release columns to each mapping table so
            # Phase C can stamp every new approval with the snapshot version.
            self._migrate_mappings_source_versions(conn)
            self._migrate_go_mappings_source_versions(conn)
            self._migrate_reactome_mappings_source_versions(conn)

            # #158 follow-up: legacy timestamp rows persisted via SQLite
            # CURRENT_TIMESTAMP land as "YYYY-MM-DD HH:MM:SS" (space, no 'T'),
            # which trips rdflib XSD.dateTime parsing during Turtle export.
            # Normalise once on startup so downstream exporters see ISO-8601.
            self._migrate_iso8601_datetime_backfill(conn)

            # Phase 35 (AUTH-04): DB-level provider-prefixed identity enforcement.
            # Creates BEFORE INSERT/UPDATE triggers that abort writes where an
            # identity-bearing column is non-NULL and lacks a ':' prefix separator.
            self._migrate_identity_check_constraint(conn)

            conn.commit()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error("Database initialization failed: %s", e)
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_proposals_admin_fields(self, conn):
        """
        Add admin fields to proposals table if they don't exist

        Args:
            conn: Database connection
        """
        try:
            # Check if admin fields exist
            cursor = conn.execute("PRAGMA table_info(proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            admin_fields = [
                "admin_notes",
                "approved_by",
                "approved_at",
                "rejected_by",
                "rejected_at",
            ]
            missing_fields = [field for field in admin_fields if field not in columns]

            if missing_fields:
                logger.info(
                    "Adding missing admin fields to proposals table: %s", missing_fields
                )

                for field in missing_fields:
                    if field.endswith("_at"):
                        conn.execute(
                            f"ALTER TABLE proposals ADD COLUMN {field} TIMESTAMP"
                        )
                    else:
                        conn.execute(f"ALTER TABLE proposals ADD COLUMN {field} TEXT")

                logger.info("Successfully migrated proposals table with admin fields")

        except Exception as e:
            logger.error("Error migrating proposals table: %s", e)
            raise

    def _migrate_mappings_updated_by_field(self, conn):
        """
        Add updated_by field to mappings table if it doesn't exist

        Args:
            conn: Database connection
        """
        try:
            # Check if updated_by field exists
            cursor = conn.execute("PRAGMA table_info(mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            if "updated_by" not in columns:
                logger.info("Adding missing updated_by field to mappings table")
                conn.execute("ALTER TABLE mappings ADD COLUMN updated_by TEXT")
                logger.info("Successfully migrated mappings table with updated_by field")

        except Exception as e:
            logger.error("Error migrating mappings table: %s", e)
            raise

    def _migrate_mappings_uuid_and_provenance(self, conn):
        """
        Add uuid and curator provenance columns to mappings table if they don't exist.

        Columns added:
            - uuid TEXT  — stable UUID per row (backfilled for existing rows)
            - approved_by_curator TEXT — GitHub username of curator who approved
            - approved_at_curator TIMESTAMP — when curator approved

        Args:
            conn: Database connection
        """
        try:
            cursor = conn.execute("PRAGMA table_info(mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            if "uuid" not in columns:
                conn.execute("ALTER TABLE mappings ADD COLUMN uuid TEXT")
                new_columns.append("uuid")
            if "approved_by_curator" not in columns:
                conn.execute("ALTER TABLE mappings ADD COLUMN approved_by_curator TEXT")
                new_columns.append("approved_by_curator")
            if "approved_at_curator" not in columns:
                conn.execute(
                    "ALTER TABLE mappings ADD COLUMN approved_at_curator TIMESTAMP"
                )
                new_columns.append("approved_at_curator")

            # Backfill uuid for any rows where uuid IS NULL
            conn.execute(
                """
                UPDATE mappings SET uuid = lower(hex(randomblob(4))) || '-' ||
                lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))),2) ||
                '-' || substr('89ab', abs(random()) % 4 + 1, 1) ||
                substr(lower(hex(randomblob(2))),2) || '-' ||
                lower(hex(randomblob(6)))
                WHERE uuid IS NULL
                """
            )

            # Ensure unique index on uuid
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_mappings_uuid ON mappings(uuid)"
            )

            if new_columns:
                logger.info(
                    "Migrated mappings table with uuid and provenance columns: %s",
                    new_columns,
                )

        except Exception as e:
            logger.error("Error migrating mappings uuid/provenance columns: %s", e)
            raise

    def _migrate_go_mappings_uuid_and_provenance(self, conn):
        """
        Add uuid and curator provenance columns to ke_go_mappings table if they don't exist.

        Columns added:
            - uuid TEXT  — stable UUID per row (backfilled for existing rows)
            - approved_by_curator TEXT — GitHub username of curator who approved
            - approved_at_curator TIMESTAMP — when curator approved

        Args:
            conn: Database connection
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            if "uuid" not in columns:
                conn.execute("ALTER TABLE ke_go_mappings ADD COLUMN uuid TEXT")
                new_columns.append("uuid")
            if "approved_by_curator" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN approved_by_curator TEXT"
                )
                new_columns.append("approved_by_curator")
            if "approved_at_curator" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN approved_at_curator TIMESTAMP"
                )
                new_columns.append("approved_at_curator")

            # Backfill uuid for any rows where uuid IS NULL
            conn.execute(
                """
                UPDATE ke_go_mappings SET uuid = lower(hex(randomblob(4))) || '-' ||
                lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))),2) ||
                '-' || substr('89ab', abs(random()) % 4 + 1, 1) ||
                substr(lower(hex(randomblob(2))),2) || '-' ||
                lower(hex(randomblob(6)))
                WHERE uuid IS NULL
                """
            )

            # Ensure unique index on uuid
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_go_mappings_uuid ON ke_go_mappings(uuid)"
            )

            if new_columns:
                logger.info(
                    "Migrated ke_go_mappings table with uuid and provenance columns: %s",
                    new_columns,
                )

        except Exception as e:
            logger.error("Error migrating ke_go_mappings uuid/provenance columns: %s", e)
            raise

    def _migrate_proposals_phase2_fields(self, conn):
        """
        Add Phase 2 fields to proposals table if they don't exist.

        Columns added:
            - uuid TEXT — stable UUID assigned at proposal creation time
            - suggestion_score REAL — BioBERT hybrid score from suggestion card
            - is_stale BOOLEAN DEFAULT FALSE — curator flag for admin review

        Args:
            conn: Database connection
        """
        try:
            cursor = conn.execute("PRAGMA table_info(proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            if "uuid" not in columns:
                conn.execute("ALTER TABLE proposals ADD COLUMN uuid TEXT")
                new_columns.append("uuid")
            if "suggestion_score" not in columns:
                conn.execute("ALTER TABLE proposals ADD COLUMN suggestion_score REAL")
                new_columns.append("suggestion_score")
            if "is_stale" not in columns:
                conn.execute(
                    "ALTER TABLE proposals ADD COLUMN is_stale BOOLEAN DEFAULT FALSE"
                )
                new_columns.append("is_stale")

            if new_columns:
                logger.info(
                    "Migrated proposals table with Phase 2 fields: %s", new_columns
                )

        except Exception as e:
            logger.error("Error migrating proposals Phase 2 fields: %s", e)
            raise

    def _migrate_go_proposals_phase2_fields(self, conn):
        """
        Add Phase 2 fields to ke_go_proposals table if they don't exist.

        Columns added:
            - uuid TEXT — stable UUID assigned at proposal creation time
            - suggestion_score REAL — BioBERT hybrid score from suggestion card
            - is_stale BOOLEAN DEFAULT FALSE — curator flag for admin review

        Note: ke_go_proposals already has approved_by, approved_at, rejected_by,
        rejected_at from its CREATE TABLE definition — these are not re-added.

        Args:
            conn: Database connection
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            if "uuid" not in columns:
                conn.execute("ALTER TABLE ke_go_proposals ADD COLUMN uuid TEXT")
                new_columns.append("uuid")
            if "suggestion_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals ADD COLUMN suggestion_score REAL"
                )
                new_columns.append("suggestion_score")
            if "is_stale" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals ADD COLUMN is_stale BOOLEAN DEFAULT FALSE"
                )
                new_columns.append("is_stale")

            if new_columns:
                logger.info(
                    "Migrated ke_go_proposals table with Phase 2 fields: %s", new_columns
                )

        except Exception as e:
            logger.error("Error migrating ke_go_proposals Phase 2 fields: %s", e)
            raise

    def _migrate_mappings_suggestion_score(self, conn):
        """
        Add suggestion_score column to mappings table if it does not exist.

        suggestion_score (REAL, nullable) — BioBERT hybrid score copied from
        the approved proposal at admin approval time. NULL for all pre-Phase-3
        rows (score was only stored on proposals before this migration).
        """
        try:
            cursor = conn.execute("PRAGMA table_info(mappings)")
            columns = [row[1] for row in cursor.fetchall()]
            if "suggestion_score" not in columns:
                conn.execute("ALTER TABLE mappings ADD COLUMN suggestion_score REAL")
                logger.info("Migrated mappings table: added suggestion_score column")
        except Exception as e:
            logger.error("Error migrating mappings suggestion_score: %s", e)
            raise

    def _migrate_go_mappings_suggestion_score(self, conn):
        """
        Add suggestion_score column to ke_go_mappings table if it does not exist.

        suggestion_score (REAL, nullable) — BioBERT hybrid score copied from
        the approved GO proposal at admin approval time. NULL for all pre-Phase-3
        rows (score was only stored on proposals before this migration).
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_mappings)")
            columns = [row[1] for row in cursor.fetchall()]
            if "suggestion_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN suggestion_score REAL"
                )
                logger.info(
                    "Migrated ke_go_mappings table: added suggestion_score column"
                )
        except Exception as e:
            logger.error("Error migrating ke_go_mappings suggestion_score: %s", e)
            raise

    def _migrate_go_mappings_go_namespace(self, conn):
        """
        Add go_namespace column to ke_go_mappings table if it does not exist.

        go_namespace (TEXT, NOT NULL, DEFAULT 'biological_process') — the ontology
        namespace for the GO term. All current GO mappings are Biological Process;
        the column is present for extensibility when MF/CC mappings are added.
        Existing rows receive 'biological_process' via the DEFAULT.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_mappings)")
            columns = [row[1] for row in cursor.fetchall()]
            if "go_namespace" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN "
                    "go_namespace TEXT NOT NULL DEFAULT 'biological_process'"
                )
                logger.info(
                    "Migrated ke_go_mappings table: added go_namespace column"
                )
        except Exception as e:
            logger.error("Error migrating ke_go_mappings go_namespace: %s", e)
            raise

    def _migrate_proposals_go_namespace(self, conn):
        """
        Add go_namespace column to ke_go_proposals table if it does not exist.

        go_namespace (TEXT, NOT NULL, DEFAULT 'biological_process') — the ontology
        namespace for the GO term. All existing proposals are Biological Process;
        the column is added so MF proposals store their namespace at submission time.
        Existing rows receive 'biological_process' via the DEFAULT.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_proposals)")
            columns = [row[1] for row in cursor.fetchall()]
            if "go_namespace" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals ADD COLUMN "
                    "go_namespace TEXT NOT NULL DEFAULT 'biological_process'"
                )
                logger.info(
                    "Migrated ke_go_proposals table: added go_namespace column"
                )
        except Exception as e:
            logger.error("Error migrating ke_go_proposals go_namespace: %s", e)
            raise

    def _migrate_proposals_new_pair_fields(self, conn):
        """
        Add new-pair columns to proposals table if they don't exist.

        New-pair proposals (mapping_id=NULL) need to store the pair data that would
        normally come from the joined mappings row.

        Columns added:
            - ke_id TEXT — Key Event ID for new-pair proposals
            - ke_title TEXT — Key Event title for new-pair proposals
            - wp_id TEXT — WikiPathways ID for new-pair proposals
            - wp_title TEXT — WikiPathways title for new-pair proposals
            - new_pair_connection_type TEXT — connection type for new-pair proposals
            - new_pair_confidence_level TEXT — confidence level for new-pair proposals

        Args:
            conn: Database connection
        """
        try:
            cursor = conn.execute("PRAGMA table_info(proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            fields_to_add = [
                ("ke_id", "TEXT"),
                ("ke_title", "TEXT"),
                ("wp_id", "TEXT"),
                ("wp_title", "TEXT"),
                ("new_pair_connection_type", "TEXT"),
                ("new_pair_confidence_level", "TEXT"),
            ]
            for field_name, field_type in fields_to_add:
                if field_name not in columns:
                    conn.execute(
                        f"ALTER TABLE proposals ADD COLUMN {field_name} {field_type}"
                    )
                    new_columns.append(field_name)

            if new_columns:
                logger.info(
                    "Migrated proposals table with new-pair fields: %s", new_columns
                )

        except Exception as e:
            logger.error("Error migrating proposals new-pair fields: %s", e)
            raise

    def _migrate_go_proposals_new_pair_fields(self, conn):
        """
        Add new-pair columns to ke_go_proposals table if they don't exist.

        New-pair proposals (mapping_id=NULL) need to store the pair data that would
        normally come from the joined ke_go_mappings row.

        Columns added:
            - ke_id TEXT
            - ke_title TEXT
            - go_id TEXT
            - go_name TEXT
            - new_pair_connection_type TEXT
            - new_pair_confidence_level TEXT
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            fields_to_add = [
                ("ke_id", "TEXT"),
                ("ke_title", "TEXT"),
                ("go_id", "TEXT"),
                ("go_name", "TEXT"),
                ("new_pair_connection_type", "TEXT"),
                ("new_pair_confidence_level", "TEXT"),
            ]
            for field_name, field_type in fields_to_add:
                if field_name not in columns:
                    conn.execute(
                        f"ALTER TABLE ke_go_proposals ADD COLUMN {field_name} {field_type}"
                    )
                    new_columns.append(field_name)

            if new_columns:
                logger.info(
                    "Migrated ke_go_proposals table with new-pair fields: %s", new_columns
                )

        except Exception as e:
            logger.error("Error migrating ke_go_proposals new-pair fields: %s", e)
            raise

    def _migrate_mappings_proposed_by(self, conn):
        """
        Add proposed_by column to mappings table if it does not exist.

        proposed_by (TEXT, nullable) — GitHub username of the curator who submitted
        the proposal that was approved into this mapping. NULL for all pre-Phase-13
        rows. Populated at admin approval time from proposals.github_username.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(mappings)")
            columns = [row[1] for row in cursor.fetchall()]
            if "proposed_by" not in columns:
                conn.execute("ALTER TABLE mappings ADD COLUMN proposed_by TEXT")
                logger.info("Migrated mappings table: added proposed_by column")
        except Exception as e:
            logger.error("Error migrating mappings proposed_by: %s", e)
            raise

    def _migrate_go_mappings_proposed_by(self, conn):
        """
        Add proposed_by column to ke_go_mappings table if it does not exist.

        proposed_by (TEXT, nullable) — GitHub username of the curator who submitted
        the GO proposal that was approved into this mapping. NULL for all pre-Phase-13
        rows. Populated at admin approval time from ke_go_proposals.github_username.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_mappings)")
            columns = [row[1] for row in cursor.fetchall()]
            if "proposed_by" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN proposed_by TEXT"
                )
                logger.info("Migrated ke_go_mappings table: added proposed_by column")
        except Exception as e:
            logger.error("Error migrating ke_go_mappings proposed_by: %s", e)
            raise

    def _migrate_provider_prefix(self, conn):
        """
        Rename github_username column to provider_username on proposal tables
        and prefix existing bare usernames with 'github:' across all identity columns.

        Idempotent: column rename only fires when github_username exists and
        provider_username does not; prefix UPDATE uses NOT LIKE '%:%' guard.
        """
        try:
            # --- Rename github_username -> provider_username on proposals ---
            cursor = conn.execute("PRAGMA table_info(proposals)")
            columns = [row[1] for row in cursor.fetchall()]
            if "github_username" in columns and "provider_username" not in columns:
                conn.execute(
                    "ALTER TABLE proposals RENAME COLUMN github_username TO provider_username"
                )
                logger.info("Migrated proposals: renamed github_username -> provider_username")

            # --- Rename github_username -> provider_username on ke_go_proposals ---
            cursor = conn.execute("PRAGMA table_info(ke_go_proposals)")
            columns = [row[1] for row in cursor.fetchall()]
            if "github_username" in columns and "provider_username" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals RENAME COLUMN github_username TO provider_username"
                )
                logger.info("Migrated ke_go_proposals: renamed github_username -> provider_username")

            # --- Prefix existing bare usernames with 'github:' ---
            prefix_targets = [
                ("proposals", "provider_username"),
                ("ke_go_proposals", "provider_username"),
                ("mappings", "created_by"),
                ("mappings", "proposed_by"),
                ("mappings", "updated_by"),
                ("ke_go_mappings", "created_by"),
                ("ke_go_mappings", "proposed_by"),
            ]
            for table, col in prefix_targets:
                # Verify column exists before updating
                cursor = conn.execute(f"PRAGMA table_info({table})")
                table_columns = [row[1] for row in cursor.fetchall()]
                if col in table_columns:
                    conn.execute(
                        f"UPDATE {table} SET {col} = 'github:' || {col} "
                        f"WHERE {col} IS NOT NULL AND {col} != '' AND {col} NOT LIKE '%:%'"
                    )
            logger.info("Migrated identity columns: prefixed bare usernames with 'github:'")
        except Exception as e:
            logger.error("Error in _migrate_provider_prefix: %s", e)
            raise

    def _migrate_go_mappings_go_direction(self, conn):
        """
        Add go_direction column to ke_go_mappings table if it does not exist.

        go_direction (TEXT, nullable) — direction of the GO term derived from its name.
        Stored as "positive" or "negative"; NULL for "unspecified" (API convention).

        Existing rows are backfilled by calling detect_go_direction(go_name) for
        each row. Rows with NULL/empty go_name receive NULL go_direction.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            if "go_direction" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN go_direction TEXT"
                )
                logger.info("Migrated ke_go_mappings table: added go_direction column")

                # Backfill existing rows
                cursor = conn.execute("SELECT id, go_name FROM ke_go_mappings")
                rows = cursor.fetchall()
                for row in rows:
                    row_id = row[0]
                    go_name = row[1]
                    if not go_name:
                        direction_value = None
                    else:
                        detected = detect_go_direction(go_name)
                        direction_value = detected if detected != "unspecified" else None
                    conn.execute(
                        "UPDATE ke_go_mappings SET go_direction = ? WHERE id = ?",
                        (direction_value, row_id)
                    )
                logger.info(
                    "Backfilled go_direction for %d existing ke_go_mappings rows", len(rows)
                )
        except Exception as e:
            logger.error("Error migrating ke_go_mappings go_direction: %s", e)
            raise

    def _migrate_go_proposals_dimension_scores(self, conn):
        """
        Add proposed dimension score columns to ke_go_proposals table if they do not exist.

        proposed_connection_score INTEGER (nullable) — curator's connection score (0-3)
        proposed_specificity_score INTEGER (nullable) — curator's specificity score (0-3)
        proposed_evidence_score    INTEGER (nullable) — curator's evidence score (0-3)

        Uses proposed_ prefix to match existing naming convention
        (proposed_confidence, proposed_connection_type).
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            if "proposed_connection_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals ADD COLUMN proposed_connection_score INTEGER"
                )
                logger.info("Migrated ke_go_proposals table: added proposed_connection_score column")

            if "proposed_specificity_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals ADD COLUMN proposed_specificity_score INTEGER"
                )
                logger.info("Migrated ke_go_proposals table: added proposed_specificity_score column")

            if "proposed_evidence_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_proposals ADD COLUMN proposed_evidence_score INTEGER"
                )
                logger.info("Migrated ke_go_proposals table: added proposed_evidence_score column")

        except Exception as e:
            logger.error("Error migrating ke_go_proposals dimension scores: %s", e)
            raise

    def _migrate_go_mappings_dimension_scores(self, conn):
        """
        Add dimension score columns to ke_go_mappings table if they do not exist.

        connection_score  INTEGER (nullable) — NULL for v1 mappings
        specificity_score INTEGER (nullable) — NULL for v1 mappings
        evidence_score    INTEGER (nullable) — NULL for v1 mappings
        assessment_version TEXT NOT NULL DEFAULT 'v1' — 'v1' for legacy, 'v2' for scored mappings
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_go_mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            if "connection_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN connection_score INTEGER"
                )
                logger.info("Migrated ke_go_mappings table: added connection_score column")

            if "specificity_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN specificity_score INTEGER"
                )
                logger.info("Migrated ke_go_mappings table: added specificity_score column")

            if "evidence_score" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN evidence_score INTEGER"
                )
                logger.info("Migrated ke_go_mappings table: added evidence_score column")

            if "assessment_version" not in columns:
                conn.execute(
                    "ALTER TABLE ke_go_mappings ADD COLUMN assessment_version TEXT NOT NULL DEFAULT 'v1'"
                )
                logger.info("Migrated ke_go_mappings table: added assessment_version column")

        except Exception as e:
            logger.error("Error migrating ke_go_mappings dimension scores: %s", e)
            raise

    def _migrate_proposals_assessment_fields(self, conn):
        """
        Add assessment-question columns to the WP proposals table if they do not exist.

        Phase 34 (ASMT-01) — transcribes the Phase 19 KE-GO migration pattern
        (see _migrate_go_proposals_dimension_scores, models.py:905-940) for the
        WP proposal table.

        Columns added:
            proposed_relationship TEXT (nullable) — how the pathway relates to the KE
            proposed_basis        TEXT (nullable) — what biological basis supports the link
            proposed_specificity  TEXT (nullable) — how specifically the pathway maps to the KE
            proposed_coverage     TEXT (nullable) — how broadly the pathway covers the KE

        NOTE: proposals tables do NOT receive assessment_version. That column
        lives only on the mapping (approved) tables — version is decided at approval
        time (CONTEXT.md decision, Phase 34).
        """
        try:
            cursor = conn.execute("PRAGMA table_info(proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            fields_to_add = [
                ("proposed_relationship", "TEXT"),
                ("proposed_basis", "TEXT"),
                ("proposed_specificity", "TEXT"),
                ("proposed_coverage", "TEXT"),
            ]
            for field_name, field_type in fields_to_add:
                if field_name not in columns:
                    conn.execute(
                        f"ALTER TABLE proposals ADD COLUMN {field_name} {field_type}"
                    )
                    new_columns.append(field_name)

            if new_columns:
                logger.info(
                    "Migrated proposals table: added assessment fields: %s",
                    new_columns,
                )

        except Exception as e:
            logger.error("Error migrating proposals assessment fields: %s", e)
            raise

    def _migrate_mappings_assessment_fields(self, conn):
        """
        Add assessment-question columns + assessment_version to the WP mappings table
        if they do not exist.

        Phase 34 (ASMT-01) — transcribes the Phase 19 KE-GO migration pattern
        (see _migrate_go_mappings_dimension_scores, models.py:942-981) for the
        WP mapping table.

        Columns added:
            proposed_relationship TEXT (nullable) — carried from proposal at approval
            proposed_basis        TEXT (nullable) — carried from proposal at approval
            proposed_specificity  TEXT (nullable) — carried from proposal at approval
            proposed_coverage     TEXT (nullable) — carried from proposal at approval
            assessment_version    TEXT NOT NULL DEFAULT 'v1' — 'v1' for legacy rows,
                                  'v2' for mappings where any proposed_* field is set.
                                  The DEFAULT 'v1' backfills every pre-Phase-34 row in
                                  one SQLite statement (same technique as models.py:973-977).
        """
        try:
            cursor = conn.execute("PRAGMA table_info(mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            fields_to_add = [
                ("proposed_relationship", "TEXT"),
                ("proposed_basis", "TEXT"),
                ("proposed_specificity", "TEXT"),
                ("proposed_coverage", "TEXT"),
            ]
            for field_name, field_type in fields_to_add:
                if field_name not in columns:
                    conn.execute(
                        f"ALTER TABLE mappings ADD COLUMN {field_name} {field_type}"
                    )
                    new_columns.append(field_name)

            if "assessment_version" not in columns:
                conn.execute(
                    "ALTER TABLE mappings ADD COLUMN assessment_version "
                    "TEXT NOT NULL DEFAULT 'v1'"
                )
                new_columns.append("assessment_version")

            if new_columns:
                logger.info(
                    "Migrated mappings table: added assessment fields: %s",
                    new_columns,
                )

        except Exception as e:
            logger.error("Error migrating mappings assessment fields: %s", e)
            raise

    def _migrate_reactome_proposals_assessment_fields(self, conn):
        """
        Add assessment-question columns to the ke_reactome_proposals table if they do
        not exist.

        Phase 34 (ASMT-02) — Reactome-side sibling of _migrate_proposals_assessment_fields.
        Transcribes the Phase 19 KE-GO migration pattern for the Reactome proposal table.

        Columns added:
            proposed_relationship TEXT (nullable)
            proposed_basis        TEXT (nullable)
            proposed_specificity  TEXT (nullable)
            proposed_coverage     TEXT (nullable)

        NOTE: no assessment_version here — proposals tables do not carry version
        (CONTEXT.md decision, Phase 34).
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_reactome_proposals)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            fields_to_add = [
                ("proposed_relationship", "TEXT"),
                ("proposed_basis", "TEXT"),
                ("proposed_specificity", "TEXT"),
                ("proposed_coverage", "TEXT"),
            ]
            for field_name, field_type in fields_to_add:
                if field_name not in columns:
                    conn.execute(
                        f"ALTER TABLE ke_reactome_proposals ADD COLUMN {field_name} {field_type}"
                    )
                    new_columns.append(field_name)

            if new_columns:
                logger.info(
                    "Migrated ke_reactome_proposals table: added assessment fields: %s",
                    new_columns,
                )

        except Exception as e:
            logger.error(
                "Error migrating ke_reactome_proposals assessment fields: %s", e
            )
            raise

    def _migrate_reactome_mappings_assessment_fields(self, conn):
        """
        Add assessment-question columns + assessment_version to the ke_reactome_mappings
        table if they do not exist.

        Phase 34 (ASMT-02) — Reactome-side sibling of _migrate_mappings_assessment_fields.
        Transcribes the Phase 19 KE-GO migration pattern for the Reactome mapping table.

        Columns added:
            proposed_relationship TEXT (nullable)
            proposed_basis        TEXT (nullable)
            proposed_specificity  TEXT (nullable)
            proposed_coverage     TEXT (nullable)
            assessment_version    TEXT NOT NULL DEFAULT 'v1' — backfills all legacy rows.
        """
        try:
            cursor = conn.execute("PRAGMA table_info(ke_reactome_mappings)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = []
            fields_to_add = [
                ("proposed_relationship", "TEXT"),
                ("proposed_basis", "TEXT"),
                ("proposed_specificity", "TEXT"),
                ("proposed_coverage", "TEXT"),
            ]
            for field_name, field_type in fields_to_add:
                if field_name not in columns:
                    conn.execute(
                        f"ALTER TABLE ke_reactome_mappings ADD COLUMN {field_name} {field_type}"
                    )
                    new_columns.append(field_name)

            if "assessment_version" not in columns:
                conn.execute(
                    "ALTER TABLE ke_reactome_mappings ADD COLUMN assessment_version "
                    "TEXT NOT NULL DEFAULT 'v1'"
                )
                new_columns.append("assessment_version")

            if new_columns:
                logger.info(
                    "Migrated ke_reactome_mappings table: added assessment fields: %s",
                    new_columns,
                )

        except Exception as e:
            logger.error(
                "Error migrating ke_reactome_mappings assessment fields: %s", e
            )
            raise

    def _migrate_mappings_source_versions(self, conn):
        """
        Add upstream source-version columns to the WP `mappings` table.

        Phase B of source-data versioning (DMP §7). Phase C will populate
        these at approval time from `data/source_versions.json`; Phase D
        will backfill historical rows from a hand-curated release calendar.
        Until then the columns are NULL on every row.

        Columns added:
            wp_release_date         TEXT (nullable, ISO YYYY-MM-DD) —
                WikiPathways snapshot the curator was working against.
            aopwiki_snapshot_date   TEXT (nullable, ISO YYYY-MM-DD) —
                AOP-Wiki snapshot the KE was sourced from.
        """
        self._add_columns_if_missing(
            conn,
            table="mappings",
            fields=[
                ("wp_release_date", "TEXT"),
                ("aopwiki_snapshot_date", "TEXT"),
            ],
            log_label="WP mappings source-version fields",
        )

    def _migrate_go_mappings_source_versions(self, conn):
        """
        Add upstream source-version columns to the `ke_go_mappings` table.

        Sibling of _migrate_mappings_source_versions. The `go_release_date`
        column stores the GO release date parsed from the OBO header
        (the manifest's `gene_ontology.release_date` field).
        """
        self._add_columns_if_missing(
            conn,
            table="ke_go_mappings",
            fields=[
                ("go_release_date", "TEXT"),
                ("aopwiki_snapshot_date", "TEXT"),
            ],
            log_label="GO mappings source-version fields",
        )

    def _migrate_reactome_mappings_source_versions(self, conn):
        """
        Add upstream source-version columns to the `ke_reactome_mappings` table.

        Reactome uses an integer release number (e.g. v96) in addition to a
        release date, so both are persisted: the version is the canonical
        upstream identifier and the date enables nearest-release lookups
        during backfill (Phase D).
        """
        self._add_columns_if_missing(
            conn,
            table="ke_reactome_mappings",
            fields=[
                ("reactome_release_version", "TEXT"),
                ("reactome_release_date", "TEXT"),
                ("aopwiki_snapshot_date", "TEXT"),
            ],
            log_label="Reactome mappings source-version fields",
        )

    # Tables whose timestamp columns flow into RDF/Turtle exports and must be
    # ISO-8601. Centralised so future tables can be added in one place. Each
    # entry lists only the columns that are (a) populated by SQLite
    # CURRENT_TIMESTAMP and (b) reachable from an exporter or public API.
    _ISO_DATETIME_BACKFILL_TARGETS = (
        ("mappings", ("created_at", "updated_at", "approved_at_curator")),
        ("ke_go_mappings", ("created_at", "updated_at", "approved_at_curator")),
        ("ke_reactome_mappings", ("created_at", "updated_at", "approved_at_curator")),
    )

    def _migrate_iso8601_datetime_backfill(self, conn):
        """
        Normalise legacy SQLite CURRENT_TIMESTAMP rows to ISO-8601.

        SQLite's CURRENT_TIMESTAMP produces "YYYY-MM-DD HH:MM:SS" — valid SQL
        but rejected by rdflib when emitted as an XSD.dateTime literal, which
        the RDF/Turtle exporter does for approved_at_curator. The user-visible
        symptom under #158 was the noisy "ISO 8601 time designator 'T' missing"
        WARNING during regenerate-exports. Fix is to replace the space with
        'T' on rows that match the SQLite default shape, leaving correctly
        ISO-formatted values (and any other shapes) untouched.

        Idempotent: re-running selects zero rows on the second pass.
        """
        try:
            total_updated = 0
            for table, columns in self._ISO_DATETIME_BACKFILL_TARGETS:
                # Skip tables that don't exist yet (fresh DB before sibling
                # migrations ran, or environments that haven't created them).
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if cursor.fetchone() is None:
                    continue

                existing_cols = {
                    r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                }
                for col in columns:
                    if col not in existing_cols:
                        continue
                    # GLOB matches "YYYY-MM-DD HH:MM:SS" (optional trailing
                    # fractional seconds / timezone). The space-at-position-11
                    # is the discriminator; ISO rows have 'T' there.
                    result = conn.execute(
                        f"""
                        UPDATE {table}
                        SET {col} = substr({col}, 1, 10) || 'T' || substr({col}, 12)
                        WHERE {col} IS NOT NULL
                          AND substr({col}, 11, 1) = ' '
                        """
                    )
                    if result.rowcount:
                        total_updated += result.rowcount
                        logger.info(
                            "ISO-8601 backfill: %s.%s normalised %d row(s)",
                            table, col, result.rowcount,
                        )
            if total_updated:
                logger.info(
                    "ISO-8601 backfill: %d total datetime value(s) normalised",
                    total_updated,
                )
        except Exception as e:
            logger.error("Error in _migrate_iso8601_datetime_backfill: %s", e)
            raise

    def _migrate_identity_check_constraint(self, conn):
        """
        Phase 35 (AUTH-04): Create BEFORE INSERT and BEFORE UPDATE triggers
        that enforce the provider-prefixed identity invariant at the DB layer.

        SQLite does not support ALTER TABLE ... ADD CONSTRAINT, so we use
        triggers (RESEARCH.md Pitfall 2). Each trigger aborts the write with
        RAISE(ABORT, ...) when an identity-bearing column is non-NULL and does
        not contain a ':' separator.

        Covered columns:
            proposals.provider_username
            mappings.created_by
            ke_go_mappings.created_by
            ke_reactome_mappings.created_by

        NULL values are allowed (guest/legacy rows where the column is genuinely
        empty). This is purely forward-looking: _migrate_provider_prefix
        (Phase 14) already backfills existing rows.

        Idempotent: uses CREATE TRIGGER IF NOT EXISTS on all trigger definitions.
        If a target table does not yet exist (partial DB) the error is logged as
        a WARNING rather than raising, matching the defensive style of sibling
        migrations.
        """
        # Each entry: (table_name, column_name)
        identity_columns = [
            ("proposals", "provider_username"),
            ("mappings", "created_by"),
            ("ke_go_mappings", "created_by"),
            ("ke_reactome_mappings", "created_by"),
        ]

        for table, col in identity_columns:
            # Check the table exists before creating triggers — skip with warning
            # if absent (partial DB / fresh environment mid-migration-chain).
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if cursor.fetchone() is None:
                logger.warning(
                    "_migrate_identity_check_constraint: table '%s' not found, "
                    "skipping trigger creation for %s.%s",
                    table, table, col,
                )
                continue

            trigger_ins = f"enforce_identity_prefix_{table}_{col}_ins"
            trigger_upd = f"enforce_identity_prefix_{table}_{col}_upd"

            try:
                conn.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_ins}
                    BEFORE INSERT ON {table}
                    WHEN NEW.{col} IS NOT NULL AND NEW.{col} NOT LIKE '%:%'
                    BEGIN
                        SELECT RAISE(ABORT,
                            '{col} must contain a provider: prefix (e.g. github:user)');
                    END
                """)
                conn.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_upd}
                    BEFORE UPDATE OF {col} ON {table}
                    WHEN NEW.{col} IS NOT NULL AND NEW.{col} NOT LIKE '%:%'
                    BEGIN
                        SELECT RAISE(ABORT,
                            '{col} must contain a provider: prefix (e.g. github:user)');
                    END
                """)
            except Exception as e:
                logger.warning(
                    "_migrate_identity_check_constraint: could not create triggers "
                    "for %s.%s: %s", table, col, e,
                )

        logger.info(
            "Phase 35 (AUTH-04): identity-prefix triggers ensured for %d column(s)",
            len(identity_columns),
        )

    def _add_columns_if_missing(self, conn, *, table, fields, log_label):
        """
        Idempotent helper: ADD COLUMN for any of `fields` not already present.

        `fields` is a list of (name, sqlite_type) tuples — all nullable. Used by
        the Phase B source-version migrations to keep the three sibling
        methods short and verifiable. Errors are logged and re-raised so the
        outer init_database() rollback fires.
        """
        try:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cursor.fetchall()}
            added = []
            for name, sql_type in fields:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    added.append(name)
            if added:
                logger.info("Migrated %s: added %s", log_label, added)
        except Exception as e:
            logger.error("Error migrating %s: %s", log_label, e)
            raise

    def _migrate_reactome_proposals_pending_unique_index(self, conn):
        """Phase 25 review H-2: enforce DB-level uniqueness on pending
        Reactome proposals.

        ke_reactome_mappings already has UNIQUE(ke_id, reactome_id) so
        approved-side duplicates are impossible, but ke_reactome_proposals
        had no equivalent — two concurrent submits could each pass the
        application-level check_reactome_mapping_exists_with_proposals
        TOCTOU window and create duplicate pending rows for the same pair.

        SQLite supports partial indexes, so scope the constraint to rows
        that are pending AND not linked to an existing mapping (i.e. the
        new-pair flow exercised by /submit_reactome_mapping). With this in
        place, the second concurrent INSERT raises sqlite3.IntegrityError,
        which the model layer catches and surfaces as a duplicate marker.
        """
        try:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_reactome_proposals_pending_pair
                ON ke_reactome_proposals (ke_id, reactome_id)
                WHERE status = 'pending' AND mapping_id IS NULL
                """
            )
            logger.info(
                "Migrated ke_reactome_proposals: added partial-unique "
                "index on (ke_id, reactome_id) for pending new-pair rows"
            )
        except Exception as e:
            logger.error(
                "Error creating partial-unique index on "
                "ke_reactome_proposals: %s", e
            )
            raise

    def _migrate_proposals_pending_unique_index(self, conn):
        """Phase 32 H-2 port: enforce DB-level uniqueness on pending
        new-pair WP proposals.

        Unlike ke_reactome_proposals which was brand-new when its H-2
        index landed in Phase 25, the `proposals` table predates this
        constraint by many phases — prod data may contain duplicate
        (ke_id, wp_id) rows where status='pending' AND mapping_id IS NULL.
        A naked CREATE UNIQUE INDEX would fail and crash startup.

        Run a cleanup pass first: keep the OLDEST pending+new-pair row
        per (ke_id, wp_id) sorted by `created_at ASC, id ASC` — created_at
        is the PRIMARY sort key, id is only the tiebreaker / NULL fallback
        (Phase 32 CONTEXT.md L27 locked decision). Auto-reject the
        losers with the EXACT migration strings, then create the
        partial-unique index. Wrap cleanup + index in one transaction
        so a partial failure rolls back cleanly.

        Idempotent: a second run finds zero duplicates → no-ops.
        No data is deleted; auto-rejected rows stay in-table, fully
        attributable to the migration via `rejected_by`.

        WARNING: do NOT use `MIN(p.id)` as a keeper-selection shortcut —
        production data may have id/created_at disagreement (manual
        fixes, restores, imports), and MIN(id) would pick the wrong row.
        """
        try:
            # 1. Cleanup pass: find duplicate pending+new-pair rows.
            #    Keeper per (ke_id, wp_id) = ORDER BY created_at ASC,
            #    id ASC LIMIT 1 (created_at primary, id fallback).
            losers = conn.execute(
                """
                SELECT p.id AS loser_id, p.ke_id, p.wp_id,
                       (SELECT p2.id
                        FROM proposals p2
                        WHERE p2.ke_id = p.ke_id
                          AND p2.wp_id = p.wp_id
                          AND p2.status = 'pending'
                          AND p2.mapping_id IS NULL
                        ORDER BY p2.created_at ASC, p2.id ASC
                        LIMIT 1) AS keeper_id
                FROM proposals p
                WHERE p.status = 'pending'
                  AND p.mapping_id IS NULL
                  AND p.id != (
                      SELECT p3.id
                      FROM proposals p3
                      WHERE p3.ke_id = p.ke_id
                        AND p3.wp_id = p.wp_id
                        AND p3.status = 'pending'
                        AND p3.mapping_id IS NULL
                      ORDER BY p3.created_at ASC, p3.id ASC
                      LIMIT 1
                  )
                """
            ).fetchall()
            for row in losers:
                conn.execute(
                    """
                    UPDATE proposals
                    SET status = 'rejected',
                        admin_notes = ?,
                        rejected_by = 'system:phase-32-migration',
                        rejected_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        f"Auto-resolved by Phase 32 H-2 migration: "
                        f"superseded by older pending proposal "
                        f"#{row['keeper_id']}",
                        row["loser_id"],
                    ),
                )
            # 2. Create the partial-unique index.
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_proposals_pending_pair
                ON proposals (ke_id, wp_id)
                WHERE status = 'pending' AND mapping_id IS NULL
                """
            )
            conn.commit()
            logger.info(
                "Migrated proposals: auto-rejected %d duplicate pending "
                "new-pair rows; added partial-unique index on "
                "(ke_id, wp_id)", len(losers),
            )
        except Exception as e:
            logger.error(
                "Error in Phase 32 H-2 migration on proposals: %s", e
            )
            conn.rollback()
            raise

    def _migrate_go_proposals_pending_unique_index(self, conn):
        """Phase 32 H-2 port (GO sibling): enforce DB-level uniqueness on
        pending new-pair GO proposals.

        Like the WP `proposals` table, `ke_go_proposals` predates this
        constraint by many phases — prod data may contain duplicate
        (ke_id, go_id) rows where status='pending' AND mapping_id IS NULL.
        A naked CREATE UNIQUE INDEX would fail and crash startup.

        Run a cleanup pass first: keep the OLDEST pending+new-pair row
        per (ke_id, go_id) sorted by `created_at ASC, id ASC` — created_at
        is the PRIMARY sort key, id is only the tiebreaker / NULL fallback
        (Phase 32 CONTEXT.md L27 locked decision). Auto-reject the
        losers with the EXACT migration strings, then create the
        partial-unique index. Wrap cleanup + index in one transaction
        so a partial failure rolls back cleanly.

        Idempotent: a second run finds zero duplicates → no-ops.
        No data is deleted; auto-rejected rows stay in-table, fully
        attributable to the migration via `rejected_by`.

        WARNING: do NOT use `MIN(p.id)` as a keeper-selection shortcut —
        production data may have id/created_at disagreement (manual
        fixes, restores, imports), and MIN(id) would pick the wrong row.
        """
        try:
            # 1. Cleanup pass: find duplicate pending+new-pair rows.
            #    Keeper per (ke_id, go_id) = ORDER BY created_at ASC,
            #    id ASC LIMIT 1 (created_at primary, id fallback).
            losers = conn.execute(
                """
                SELECT p.id AS loser_id, p.ke_id, p.go_id,
                       (SELECT p2.id
                        FROM ke_go_proposals p2
                        WHERE p2.ke_id = p.ke_id
                          AND p2.go_id = p.go_id
                          AND p2.status = 'pending'
                          AND p2.mapping_id IS NULL
                        ORDER BY p2.created_at ASC, p2.id ASC
                        LIMIT 1) AS keeper_id
                FROM ke_go_proposals p
                WHERE p.status = 'pending'
                  AND p.mapping_id IS NULL
                  AND p.id != (
                      SELECT p3.id
                      FROM ke_go_proposals p3
                      WHERE p3.ke_id = p.ke_id
                        AND p3.go_id = p.go_id
                        AND p3.status = 'pending'
                        AND p3.mapping_id IS NULL
                      ORDER BY p3.created_at ASC, p3.id ASC
                      LIMIT 1
                  )
                """
            ).fetchall()
            for row in losers:
                conn.execute(
                    """
                    UPDATE ke_go_proposals
                    SET status = 'rejected',
                        admin_notes = ?,
                        rejected_by = 'system:phase-32-migration',
                        rejected_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        f"Auto-resolved by Phase 32 H-2 migration: "
                        f"superseded by older pending proposal "
                        f"#{row['keeper_id']}",
                        row["loser_id"],
                    ),
                )
            # 2. Create the partial-unique index.
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_go_proposals_pending_pair
                ON ke_go_proposals (ke_id, go_id)
                WHERE status = 'pending' AND mapping_id IS NULL
                """
            )
            conn.commit()
            logger.info(
                "Migrated ke_go_proposals: auto-rejected %d duplicate "
                "pending new-pair rows; added partial-unique index on "
                "(ke_id, go_id)", len(losers),
            )
        except Exception as e:
            logger.error(
                "Error in Phase 32 H-2 migration on ke_go_proposals: %s", e
            )
            conn.rollback()
            raise


class MappingCountsMixin:
    """Row counting for the three mapping tables, in one place.

    Subclasses set TABLE and provide self.db.

    Why this exists: the landing-page stat cards and the public API used to
    count the same rows through two independent pieces of raw SQL — the cards
    via hand-written queries in src/blueprints/main.py, the API via the
    paginated model methods. They agreed only by coincidence. The API path
    already builds a WHERE from its filters while the stats path has no filter
    concept at all, so any default-on filter added there (status, soft-delete,
    assessment version) would have desynced the cards silently, which is
    precisely what #211 was reported as. Both paths now go through _count.
    """

    TABLE: str = ""

    def _count(self, conn, where: str = "", params=()) -> int:
        """The single COUNT statement for this table."""
        return conn.execute(
            f"SELECT COUNT(*) FROM {self.TABLE} {where}", params
        ).fetchone()[0]

    def count_all(self) -> int:
        conn = self.db.get_connection()
        try:
            return self._count(conn)
        finally:
            conn.close()

    def count_by_confidence(self) -> Dict[str, int]:
        conn = self.db.get_connection()
        try:
            return {
                row[0]: row[1]
                for row in conn.execute(
                    f"SELECT LOWER(confidence_level), COUNT(*) FROM {self.TABLE}"
                    " GROUP BY LOWER(confidence_level)"
                ).fetchall()
            }
        finally:
            conn.close()

    def revision(self) -> str:
        """Fingerprint of the table's current contents.

        Used to decide whether a cached export is stale. This was COUNT(*)
        paired with MAX(updated_at), which misses real edits on the live data:
        the mapping tables hold updated_at in two formats side by side, ISO
        '2026-07-22T14:20:46' from the application layer and SQLite's
        '2026-07-22 19:45:08' from CURRENT_TIMESTAMP, and 'T' sorts after ' ',
        so MAX() can sit on an older ISO row while a newer CURRENT_TIMESTAMP
        row is written underneath it. A count-plus-max pair also cannot see a
        delete-plus-insert, nor a column corrected by hand without touching
        updated_at.

        Hashing the rows removes the whole class of misses. These tables hold
        hundreds of rows, not millions, so the read costs less than the
        regeneration a false "unchanged" answer would wrongly skip.
        """
        conn = self.db.get_connection()
        try:
            digest = hashlib.sha256()
            for row in conn.execute(f"SELECT * FROM {self.TABLE} ORDER BY id"):
                digest.update(repr(tuple(row)).encode("utf-8"))
                digest.update(b"\x00")
            count = self._count(conn)
            return f"{count}:{digest.hexdigest()[:32]}"
        finally:
            conn.close()


class MappingModel(MappingCountsMixin):
    TABLE = "mappings"

    def __init__(self, db: Database):
        self.db = db

    def create_mapping(
        self,
        ke_id: str,
        ke_title: str,
        wp_id: str,
        wp_title: str,
        connection_type: str = "undefined",
        confidence_level: str = "low",
        created_by: str = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
        # Phase C — source-data versioning. Stamped from the deployed manifest
        # (data/source_versions.json) by the admin approval handler. NULL if
        # the manifest is missing or the upstream is currently 'unknown'.
        wp_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> Optional[int]:
        """Create a new KE-WP mapping"""
        mapping_uuid = str(uuid_lib.uuid4())
        # Phase 34 dual-write: proposed_relationship also populates connection_type
        effective_connection_type = proposed_relationship if proposed_relationship is not None else connection_type
        assessment_version = _classify_assessment_version(
            proposed_relationship, proposed_basis, proposed_specificity, proposed_coverage
        )
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO mappings (ke_id, ke_title, wp_id, wp_title, connection_type,
                                    confidence_level, created_by, uuid,
                                    proposed_relationship, proposed_basis,
                                    proposed_specificity, proposed_coverage,
                                    assessment_version,
                                    wp_release_date, aopwiki_snapshot_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    ke_id,
                    ke_title,
                    wp_id,
                    wp_title,
                    effective_connection_type,
                    confidence_level,
                    created_by,
                    mapping_uuid,
                    proposed_relationship,
                    proposed_basis,
                    proposed_specificity,
                    proposed_coverage,
                    assessment_version,
                    wp_release_date,
                    aopwiki_snapshot_date,
                ),
            )

            conn.commit()
            logger.info(
                "Created mapping: KE=%s, WP=%s, User=%s, UUID=%s",
                ke_id,
                wp_id,
                created_by,
                mapping_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning("Duplicate mapping attempted: KE=%s, WP=%s", ke_id, wp_id)
            return None
        except Exception as e:
            logger.error("Error creating mapping: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def get_all_mappings(self) -> List[Dict]:
        """Get all mappings"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, wp_id, wp_title, connection_type,
                       confidence_level, created_by, created_at, updated_at,
                       uuid, approved_by_curator, approved_at_curator,
                       -- Phase 34 ASMT-08: do NOT drop these or insert above;
                       -- positional consumers append at end (Pitfall 5)
                       proposed_relationship, proposed_basis,
                       proposed_specificity, proposed_coverage,
                       assessment_version,
                       -- Phase E.1 source-data versioning columns. Same
                       -- positional-append rule as the Phase 34 fields.
                       wp_release_date, aopwiki_snapshot_date
                FROM mappings
                ORDER BY created_at DESC
            """
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_mappings_by_ke(self, ke_id: str) -> List[Dict]:
        """Get all mappings for a specific Key Event"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, wp_id, wp_title, connection_type,
                       confidence_level, created_by, created_at, updated_at,
                       uuid, approved_by_curator, approved_at_curator
                FROM mappings
                WHERE ke_id = ?
                ORDER BY created_at DESC
                """,
                (ke_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_mapped_ke_ids(self) -> list:
        """Return distinct KE IDs that have at least one WP mapping."""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT DISTINCT ke_id FROM mappings"
            )
            return [row["ke_id"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_mappings_paginated(
        self,
        page: int = 1,
        per_page: int = 50,
        ke_id: str = None,
        pathway_id: str = None,
        confidence_level: str = None,
        ke_ids: list = None,
    ) -> tuple:
        """
        Return (List[Dict], total_count) for approved KE-WP mappings.

        Filters (all optional, combinable):
          ke_id            — exact match on mappings.ke_id
          pathway_id       — exact match on mappings.wp_id
          confidence_level — case-insensitive match on mappings.confidence_level
          ke_ids           — IN filter used when aop_id has been resolved to KE IDs;
                             pass [] to return ([], 0) immediately (valid AOP, no KEs mapped)

        Returns rows with columns:
          uuid, ke_id, ke_title, wp_id, wp_title, confidence_level,
          approved_by_curator, approved_at_curator, suggestion_score
        Ordered by created_at DESC.
        """
        conditions = []
        params = []

        if ke_id:
            conditions.append("ke_id = ?")
            params.append(ke_id)
        if pathway_id:
            conditions.append("wp_id = ?")
            params.append(pathway_id)
        if confidence_level:
            conditions.append("LOWER(confidence_level) = LOWER(?)")
            params.append(confidence_level)
        if ke_ids is not None:
            if not ke_ids:
                return [], 0
            placeholders = ",".join("?" * len(ke_ids))
            conditions.append(f"ke_id IN ({placeholders})")
            params.extend(ke_ids)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * per_page

        conn = self.db.get_connection()
        try:
            total = self._count(conn, where, params)
            rows = conn.execute(
                f"""SELECT uuid, ke_id, ke_title, wp_id, wp_title, confidence_level,
                           approved_by_curator, approved_at_curator, suggestion_score,
                           proposed_by, connection_type,
                           -- Phase 34 ASMT-08: assessment columns must flow through
                           -- the paginated SELECT so /api/v1/mappings emits them.
                           -- Positional consumers append at end (Pitfall 5).
                           proposed_relationship, proposed_basis,
                           proposed_specificity, proposed_coverage,
                           assessment_version
                    FROM mappings {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
            return [dict(r) for r in rows], total
        finally:
            conn.close()

    def check_mapping_exists(self, ke_id: str, wp_id: str) -> Dict:
        """Check if KE-WP pair exists"""
        conn = self.db.get_connection()
        try:
            # Check exact pair
            cursor = conn.execute(
                """
                SELECT * FROM mappings WHERE ke_id = ? AND wp_id = ?
            """,
                (ke_id, wp_id),
            )
            pair_match = cursor.fetchone()

            if pair_match:
                return {
                    "pair_exists": True,
                    "message": f"The KE-WP pair ({ke_id}, {wp_id}) already exists.",
                }

            # Check for existing KE
            cursor = conn.execute(
                """
                SELECT * FROM mappings WHERE ke_id = ?
            """,
                (ke_id,),
            )
            ke_matches = [dict(row) for row in cursor.fetchall()]

            if ke_matches:
                return {
                    "ke_exists": True,
                    "message": f"The KE ID {ke_id} exists but not with WP ID {wp_id}.",
                    "ke_matches": ke_matches,
                }

            return {
                "ke_exists": False,
                "pair_exists": False,
                "message": f"The KE ID {ke_id} and WP ID {wp_id} are new entries.",
            }
        finally:
            conn.close()

    def check_mapping_exists_with_proposals(self, ke_id: str, wp_id: str) -> Dict:
        """
        Enriched duplicate check that returns structured blocking payloads.

        Priority order:
        1. pending_proposal — an open proposal already covers this pair (most actionable)
        2. approved_mapping — approved mapping exists, user can submit_revision
        3. ke_exists — KE exists with a different WP (informational)
        4. nothing found

        Returns one of:
        - blocking_type='pending_proposal' if a pending proposal exists for the pair
        - blocking_type='approved_mapping' if an approved KE-WP pair exists (no pending proposal)
        - ke_exists info if the KE exists with a different WP
        - ke_exists=False, pair_exists=False if nothing found
        """
        conn = self.db.get_connection()
        try:
            # 0. Check for pending new-pair proposal (mapping_id IS NULL).
            # New-pair proposals aren't linked to a mapping row yet, so the
            # JOIN below cannot detect them. Query proposals directly by
            # ke_id/wp_id. (Phase 32 parity with GO's check_go_mapping_*
            # check_0 path; required so the /submit IntegrityError branch
            # can reuse this shape on duplicate-pending races.)
            cursor = conn.execute(
                """
                SELECT id, proposed_confidence, proposed_connection_type,
                       provider_username, created_at, ke_id, wp_id,
                       ke_title, wp_title
                FROM proposals
                WHERE ke_id = ? AND wp_id = ?
                  AND mapping_id IS NULL AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (ke_id, wp_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "pending_proposal",
                    "existing": {
                        "proposal_id": row["id"],
                        "ke_id": row["ke_id"],
                        "wp_id": row["wp_id"],
                        "ke_title": row["ke_title"],
                        "wp_title": row["wp_title"],
                        "proposed_confidence": row["proposed_confidence"],
                        "proposed_connection_type": row["proposed_connection_type"],
                        "submitted_by": row["provider_username"],
                        "submitted_at": row["created_at"],
                    },
                    "actions": ["flag_stale"],
                }

            # 1. Check for pending proposal on the KE-WP pair (highest priority blocking)
            cursor = conn.execute(
                """
                SELECT p.id, p.proposed_confidence, p.proposed_connection_type,
                       p.provider_username, p.created_at,
                       m.ke_id, m.wp_id, m.ke_title, m.wp_title
                FROM proposals p
                JOIN mappings m ON p.mapping_id = m.id
                WHERE m.ke_id = ? AND m.wp_id = ? AND p.status = 'pending'
                ORDER BY p.created_at DESC LIMIT 1
                """,
                (ke_id, wp_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "pending_proposal",
                    "existing": {
                        "proposal_id": row["id"],
                        "ke_id": row["ke_id"],
                        "wp_id": row["wp_id"],
                        "ke_title": row["ke_title"],
                        "wp_title": row["wp_title"],
                        "proposed_confidence": row["proposed_confidence"],
                        "proposed_connection_type": row["proposed_connection_type"],
                        "submitted_by": row["provider_username"],
                        "submitted_at": row["created_at"],
                    },
                    "actions": ["flag_stale"],
                }

            # 2. Check for approved mapping (exact ke_id + wp_id pair)
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, wp_id, wp_title, connection_type,
                       confidence_level, approved_by_curator, approved_at_curator, uuid
                FROM mappings WHERE ke_id = ? AND wp_id = ?
                """,
                (ke_id, wp_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "approved_mapping",
                    "existing": {
                        "ke_id": row["ke_id"],
                        "wp_id": row["wp_id"],
                        "ke_title": row["ke_title"],
                        "wp_title": row["wp_title"],
                        "confidence_level": row["confidence_level"],
                        "connection_type": row["connection_type"],
                        "approved_by_curator": row["approved_by_curator"],
                        "approved_at_curator": row["approved_at_curator"],
                        "uuid": row["uuid"],
                        "id": row["id"],
                    },
                    "actions": ["submit_revision"],
                }

            # 3. Check if KE exists with a different WP (backward-compat ke_exists path)
            cursor = conn.execute(
                "SELECT * FROM mappings WHERE ke_id = ?",
                (ke_id,),
            )
            ke_matches = [dict(row) for row in cursor.fetchall()]
            if ke_matches:
                return {
                    "ke_exists": True,
                    "message": f"The KE ID {ke_id} exists but not with WP ID {wp_id}.",
                    "ke_matches": ke_matches,
                }

            return {
                "ke_exists": False,
                "pair_exists": False,
                "message": f"The KE ID {ke_id} and WP ID {wp_id} are new entries.",
            }
        finally:
            conn.close()

    def update_mapping(
        self,
        mapping_id: int,
        connection_type: str = None,
        confidence_level: str = None,
        updated_by: str = None,
        approved_by_curator: str = None,
        approved_at_curator: str = None,
        suggestion_score: float = None,
        proposed_by: str = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
        # Phase C — source-data versioning kwargs; nullable, stamped at approval.
        wp_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> bool:
        """
        Update an existing mapping

        Args:
            mapping_id: ID of the mapping to update
            connection_type: New connection type (optional)
            confidence_level: New confidence level (optional)
            updated_by: Username of person making the update
            approved_by_curator: GitHub username of curator who approved (optional)
            approved_at_curator: ISO timestamp of curator approval (optional)
            suggestion_score: BioBERT hybrid score from the approved proposal (optional)
            proposed_by: GitHub username of the curator who originally submitted the proposal (optional)
            proposed_relationship: Phase 34 assessment answer — relationship type (optional)
            proposed_basis: Phase 34 assessment answer — biological basis (optional)
            proposed_specificity: Phase 34 assessment answer — mapping specificity (optional)
            proposed_coverage: Phase 34 assessment answer — pathway coverage (optional)

        Returns:
            True if successful, False otherwise
        """
        # Define allowed fields to prevent SQL injection
        ALLOWED_FIELDS = {
            "connection_type": "connection_type",
            "confidence_level": "confidence_level",
            "updated_by": "updated_by",
            "approved_by_curator": "approved_by_curator",
            "approved_at_curator": "approved_at_curator",
            "suggestion_score": "suggestion_score",
            "proposed_by": "proposed_by",
            "proposed_relationship": "proposed_relationship",
            "proposed_basis": "proposed_basis",
            "proposed_specificity": "proposed_specificity",
            "proposed_coverage": "proposed_coverage",
            "assessment_version": "assessment_version",
            # Phase C source-data versioning columns.
            "wp_release_date": "wp_release_date",
            "aopwiki_snapshot_date": "aopwiki_snapshot_date",
        }

        # Phase 34 dual-write: proposed_relationship also populates connection_type
        effective_connection_type = (
            proposed_relationship if proposed_relationship is not None else connection_type
        )
        # Compute assessment_version whenever any assessment answer is provided
        _has_assessment = any(v is not None for v in (
            proposed_relationship, proposed_basis, proposed_specificity, proposed_coverage
        ))
        assessment_version = _classify_assessment_version(
            proposed_relationship, proposed_basis, proposed_specificity, proposed_coverage
        ) if _has_assessment else None

        conn = self.db.get_connection()
        try:
            update_clauses = []
            params = []

            # Build update clauses using whitelisted field names
            update_data = {
                "connection_type": effective_connection_type,
                "confidence_level": confidence_level,
                "updated_by": updated_by,
                "approved_by_curator": approved_by_curator,
                "approved_at_curator": approved_at_curator,
                "suggestion_score": suggestion_score,
                "proposed_by": proposed_by,
                "proposed_relationship": proposed_relationship,
                "proposed_basis": proposed_basis,
                "proposed_specificity": proposed_specificity,
                "proposed_coverage": proposed_coverage,
                "assessment_version": assessment_version,
                # Phase C source-data versioning — None values are skipped by the
                # `field_value is not None` guard below so unspecified columns
                # remain untouched on revision updates.
                "wp_release_date": wp_release_date,
                "aopwiki_snapshot_date": aopwiki_snapshot_date,
            }

            for field_name, field_value in update_data.items():
                if field_value is not None and field_name in ALLOWED_FIELDS:
                    update_clauses.append(f"{ALLOWED_FIELDS[field_name]} = ?")
                    params.append(field_value)

            # Always update timestamp
            update_clauses.append("updated_at = CURRENT_TIMESTAMP")

            if not update_clauses:
                return False

            # Build safe query with whitelisted field names
            query = f"UPDATE mappings SET {', '.join(update_clauses)} WHERE id = ?"
            params.append(mapping_id)

            conn.execute(query, params)
            conn.commit()
            logger.info("Updated mapping %s by %s", mapping_id, updated_by)
            return True
        except Exception as e:
            logger.error("Error updating mapping: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def _approve_on_conn(
        self,
        conn,
        proposal: dict,
        approved_by_curator: str,
        approved_at_curator: str = None,
        # Phase C — source-data versioning kwargs; nullable, stamped from manifest.
        wp_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> str:
        """Approve a WP new-pair proposal on a caller-managed connection.

        Executes the create_mapping INSERT followed by the update_mapping provenance
        UPDATE, both on `conn`, without calling conn.commit() or conn.close().
        Returns the new mapping UUID string for the audit log.

        Only handles new-pair proposals (proposal["mapping_id"] is None and not
        proposal["proposed_delete"]). Raises on IntegrityError or any DB error so
        the bulk-approve caller's outer try/except can roll back the entire batch.

        Phase 38 (ADMIN-02): caller-managed transaction helper for bulk-approve.
        """
        if approved_at_curator is None:
            approved_at_curator = datetime.utcnow().isoformat()

        mapping_uuid = str(uuid_lib.uuid4())

        proposed_relationship = proposal.get("proposed_relationship")
        proposed_basis = proposal.get("proposed_basis")
        proposed_specificity = proposal.get("proposed_specificity")
        proposed_coverage = proposal.get("proposed_coverage")

        effective_connection_type = (
            proposed_relationship if proposed_relationship is not None
            else (proposal.get("new_pair_connection_type") or proposal.get("proposed_connection_type"))
        )
        assessment_version = _classify_assessment_version(
            proposed_relationship, proposed_basis, proposed_specificity, proposed_coverage
        )

        cursor = conn.execute(
            """
            INSERT INTO mappings (ke_id, ke_title, wp_id, wp_title, connection_type,
                                  confidence_level, created_by, uuid,
                                  proposed_relationship, proposed_basis,
                                  proposed_specificity, proposed_coverage,
                                  assessment_version,
                                  wp_release_date, aopwiki_snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                proposal["ke_id"],
                proposal["ke_title"],
                proposal["wp_id"],
                proposal["wp_title"],
                effective_connection_type,
                proposal.get("new_pair_confidence_level") or proposal.get("proposed_confidence"),
                proposal.get("provider_username") or approved_by_curator,
                mapping_uuid,
                proposed_relationship,
                proposed_basis,
                proposed_specificity,
                proposed_coverage,
                assessment_version,
                wp_release_date,
                aopwiki_snapshot_date,
            ),
        )
        new_mapping_id = cursor.lastrowid

        # Write provenance (approved_by, approved_at, suggestion_score, proposed_by)
        update_clauses = [
            "approved_by_curator = ?",
            "approved_at_curator = ?",
            "proposed_by = ?",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        params = [
            approved_by_curator,
            approved_at_curator,
            proposal.get("provider_username"),
        ]
        proposal_score = proposal.get("suggestion_score")
        if proposal_score is not None:
            update_clauses.append("suggestion_score = ?")
            params.append(proposal_score)
        # Re-thread assessment for defense-in-depth on the update path
        if proposed_relationship is not None:
            update_clauses.append("proposed_relationship = ?")
            params.append(proposed_relationship)
        if proposed_basis is not None:
            update_clauses.append("proposed_basis = ?")
            params.append(proposed_basis)
        if proposed_specificity is not None:
            update_clauses.append("proposed_specificity = ?")
            params.append(proposed_specificity)
        if proposed_coverage is not None:
            update_clauses.append("proposed_coverage = ?")
            params.append(proposed_coverage)
        if assessment_version is not None:
            update_clauses.append("assessment_version = ?")
            params.append(assessment_version)

        params.append(new_mapping_id)
        conn.execute(
            f"UPDATE mappings SET {', '.join(update_clauses)} WHERE id = ?",
            params,
        )
        logger.info(
            "WP _approve_on_conn: KE=%s, WP=%s, UUID=%s (caller-managed tx)",
            proposal["ke_id"], proposal["wp_id"], mapping_uuid,
        )
        return mapping_uuid

    def get_mapping_by_uuid(self, mapping_uuid: str) -> Optional[Dict]:
        """Get a mapping by its stable UUID"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, wp_id, wp_title, connection_type,
                       confidence_level, created_by, created_at, updated_at,
                       uuid, approved_by_curator, approved_at_curator, updated_by,
                       proposed_by, suggestion_score,
                       -- Phase 34 ASMT-08: assessment columns flow through to
                       -- /api/v1/mappings/<uuid> serializer (sibling parity with list).
                       proposed_relationship, proposed_basis,
                       proposed_specificity, proposed_coverage,
                       assessment_version
                FROM mappings
                WHERE uuid = ?
                """,
                (mapping_uuid,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def delete_mapping(self, mapping_id: int, deleted_by: str = None) -> bool:
        """
        Delete a mapping

        Args:
            mapping_id: ID of the mapping to delete
            deleted_by: Username of person deleting the mapping

        Returns:
            True if successful, False otherwise
        """
        conn = self.db.get_connection()
        try:
            conn.execute("DELETE FROM mappings WHERE id = ?", (mapping_id,))
            conn.commit()
            logger.info("Deleted mapping %s by %s", mapping_id, deleted_by)
            return True
        except Exception as e:
            logger.error("Error deleting mapping: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()


class ProposalModel:
    # Sentinel returned from create_new_pair_proposal when the
    # partial-unique index on proposals(ke_id, wp_id) WHERE
    # status='pending' AND mapping_id IS NULL rejects a concurrent
    # insert (Phase 32 H-2 port from Reactome). The route layer maps
    # this to a duplicate-pending 409 response reusing the existing
    # check_mapping_exists_with_proposals shape — see CONTEXT.md L34-39.
    DUPLICATE_PENDING = "duplicate_pending"

    def __init__(self, db: Database):
        self.db = db

    def create_proposal(
        self,
        mapping_id: int,
        user_name: str,
        user_email: str,
        user_affiliation: str,
        provider_username: str = None,
        proposed_delete: bool = False,
        proposed_confidence: str = None,
        proposed_connection_type: str = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
    ) -> Optional[int]:
        """Create a new proposal"""
        proposal_uuid = str(uuid_lib.uuid4())
        # Phase 34 dual-write: proposed_relationship also populates proposed_connection_type
        effective_connection_type = (
            proposed_relationship if proposed_relationship is not None else proposed_connection_type
        )
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO proposals (mapping_id, user_name, user_email, user_affiliation,
                                     provider_username, proposed_delete, proposed_confidence,
                                     proposed_connection_type, uuid,
                                     proposed_relationship, proposed_basis,
                                     proposed_specificity, proposed_coverage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    mapping_id,
                    user_name,
                    user_email,
                    user_affiliation,
                    provider_username,
                    proposed_delete,
                    proposed_confidence,
                    effective_connection_type,
                    proposal_uuid,
                    proposed_relationship,
                    proposed_basis,
                    proposed_specificity,
                    proposed_coverage,
                ),
            )

            conn.commit()
            logger.info(
                "Created proposal for mapping %s by %s, UUID=%s",
                mapping_id,
                provider_username,
                proposal_uuid,
            )
            return cursor.lastrowid
        except Exception as e:
            logger.error("Error creating proposal: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def create_new_pair_proposal(
        self,
        ke_id: str,
        ke_title: str,
        wp_id: str,
        wp_title: str,
        connection_type: str,
        confidence_level: str,
        provider_username: str = None,
        suggestion_score: float = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
    ) -> Optional[int]:
        """
        Create a new-pair proposal where no existing mapping_id exists yet.

        The mapping is created only after an admin explicitly approves this proposal.
        mapping_id is left NULL so approve_proposal() knows to call create_mapping()
        rather than update_mapping() at approval time.

        Args:
            ke_id: Key Event ID
            ke_title: Key Event title
            wp_id: WikiPathways ID
            wp_title: WikiPathways title
            connection_type: Proposed connection type
            confidence_level: Proposed confidence level
            provider_username: Provider-prefixed username of submitting curator
            suggestion_score: BioBERT hybrid score captured at suggestion time
            proposed_relationship: Phase 34 assessment answer — relationship type (optional)
            proposed_basis: Phase 34 assessment answer — biological basis (optional)
            proposed_specificity: Phase 34 assessment answer — mapping specificity (optional)
            proposed_coverage: Phase 34 assessment answer — pathway coverage (optional)

        Returns:
            New proposal row ID on success, None on exception
        """
        proposal_uuid = str(uuid_lib.uuid4())
        # Phase 34 dual-write: proposed_relationship also populates proposed_connection_type
        effective_connection_type = (
            proposed_relationship if proposed_relationship is not None else connection_type
        )
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO proposals (
                    mapping_id, user_name, user_email, user_affiliation,
                    provider_username, proposed_delete, proposed_confidence,
                    proposed_connection_type, uuid, suggestion_score,
                    ke_id, ke_title, wp_id, wp_title,
                    new_pair_connection_type, new_pair_confidence_level,
                    proposed_relationship, proposed_basis,
                    proposed_specificity, proposed_coverage
                )
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    provider_username or "curator",
                    "",
                    "",
                    provider_username,
                    False,
                    confidence_level,
                    effective_connection_type,
                    proposal_uuid,
                    suggestion_score,
                    ke_id,
                    ke_title,
                    wp_id,
                    wp_title,
                    effective_connection_type,
                    confidence_level,
                    proposed_relationship,
                    proposed_basis,
                    proposed_specificity,
                    proposed_coverage,
                ),
            )

            conn.commit()
            logger.info(
                "Created new-pair proposal for %s -> %s by %s, UUID=%s",
                ke_id,
                wp_id,
                provider_username,
                proposal_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError as e:
            # Phase 32 H-2 port: partial-unique index on
            # proposals(ke_id, wp_id) WHERE status='pending' AND
            # mapping_id IS NULL fired — a concurrent submit beat us to
            # the slot. Surface as the DUPLICATE_PENDING sentinel so the
            # route layer can return a 409 with the existing
            # check_mapping_exists_with_proposals shape.
            logger.warning(
                "Duplicate pending WP proposal blocked: "
                "KE=%s WP=%s (%s)", ke_id, wp_id, e,
            )
            conn.rollback()
            return self.DUPLICATE_PENDING
        except Exception as e:
            logger.error("Error creating new-pair proposal: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def get_all_proposals(self, status: str = None) -> List[Dict]:
        """
        Get all proposals, optionally filtered by status

        Args:
            status: Filter by proposal status ('pending', 'approved', 'rejected', or None for all)

        Returns:
            List of proposal dictionaries with mapping details
        """
        conn = self.db.get_connection()
        try:
            query = """
                SELECT p.*, m.ke_id as mapping_ke_id, m.ke_title as mapping_ke_title,
                       m.wp_id as mapping_wp_id, m.wp_title as mapping_wp_title,
                       m.connection_type as current_connection_type,
                       m.confidence_level as current_confidence_level
                FROM proposals p
                LEFT JOIN mappings m ON p.mapping_id = m.id
            """
            params = ()

            if status:
                query += " WHERE p.status = ?"
                params = (status,)

            query += " ORDER BY p.created_at DESC"

            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_proposal_by_id(self, proposal_id: int) -> Optional[Dict]:
        """
        Get a specific proposal by ID with mapping details

        Args:
            proposal_id: The proposal ID to retrieve

        Returns:
            Dictionary containing proposal and mapping details, or None if not found
        """
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT p.*, m.ke_id as mapping_ke_id, m.ke_title as mapping_ke_title,
                       m.wp_id as mapping_wp_id, m.wp_title as mapping_wp_title,
                       m.connection_type as current_connection_type,
                       m.confidence_level as current_confidence_level
                FROM proposals p
                LEFT JOIN mappings m ON p.mapping_id = m.id
                WHERE p.id = ?
            """,
                (proposal_id,),
            )

            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_proposal_status(
        self,
        proposal_id: int,
        status: str,
        admin_username: str = None,
        admin_notes: str = None,
    ) -> bool:
        """
        Update proposal status and admin information

        Args:
            proposal_id: The proposal ID to update
            status: New status ('approved', 'rejected')
            admin_username: GitHub username of admin performing action
            admin_notes: Optional notes from admin

        Returns:
            True if successful, False otherwise
        """
        # Validate status to prevent SQL injection
        if status not in ["approved", "rejected"]:
            logger.error("Invalid status value: %s", status)
            return False

        conn = self.db.get_connection()
        try:
            # Use safe field mapping based on validated status
            if status == "approved":
                query = """
                    UPDATE proposals 
                    SET status = ?, approved_by = ?, admin_notes = ?, approved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
            else:  # status == 'rejected'
                query = """
                    UPDATE proposals 
                    SET status = ?, rejected_by = ?, admin_notes = ?, rejected_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """

            conn.execute(query, (status, admin_username, admin_notes, proposal_id))

            conn.commit()
            logger.info(
                "Updated proposal %s to %s by %s", proposal_id, status, admin_username
            )
            return True
        except Exception as e:
            logger.error("Error updating proposal status: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def _update_status_on_conn(
        self,
        conn,
        proposal_id: int,
        status: str,
        admin_username: str,
        admin_notes: str,
    ) -> None:
        """Update WP proposal status on a caller-managed connection.

        Raises ValueError for invalid status. Executes the UPDATE on `conn`
        without calling conn.commit() or conn.close(). Any DB error propagates
        to the caller for rollback.

        Phase 38 (ADMIN-02): caller-managed transaction helper for bulk-approve.
        """
        if status not in ["approved", "rejected"]:
            raise ValueError(f"Invalid status: {status}")
        if status == "approved":
            query = """
                UPDATE proposals
                SET status = ?, approved_by = ?, admin_notes = ?, approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
        else:
            query = """
                UPDATE proposals
                SET status = ?, rejected_by = ?, admin_notes = ?, rejected_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
        conn.execute(query, (status, admin_username, admin_notes, proposal_id))

    def flag_proposal_stale(self, proposal_id: int, flagged_by: str) -> bool:
        """
        Flag a pending proposal as stale for admin review.

        Args:
            proposal_id: ID of the proposal to flag
            flagged_by: Username of curator flagging the proposal

        Returns:
            True if successful, False otherwise
        """
        conn = self.db.get_connection()
        try:
            conn.execute(
                "UPDATE proposals SET is_stale = 1 WHERE id = ?",
                (proposal_id,),
            )
            conn.commit()
            logger.info(
                "Proposal %s flagged as stale by %s", proposal_id, flagged_by
            )
            return True
        except Exception as e:
            logger.error("Error flagging proposal %s as stale: %s", proposal_id, e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def find_mapping_by_details(self, ke_id: str, wp_id: str) -> Optional[int]:
        """
        Find mapping ID by KE and WP IDs

        Args:
            ke_id: Key Event ID
            wp_id: WikiPathway ID

        Returns:
            Mapping ID if found, None otherwise
        """
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id FROM mappings WHERE ke_id = ? AND wp_id = ?
            """,
                (ke_id, wp_id),
            )

            row = cursor.fetchone()
            return row["id"] if row else None
        finally:
            conn.close()


class GoMappingModel(MappingCountsMixin):
    TABLE = "ke_go_mappings"

    def __init__(self, db: Database):
        self.db = db

    def create_mapping(
        self,
        ke_id: str,
        ke_title: str,
        go_id: str,
        go_name: str,
        connection_type: str = "related",
        confidence_level: str = "low",
        evidence_code: str = None,
        created_by: str = None,
        go_direction: str = None,
        connection_score: int = None,
        specificity_score: int = None,
        evidence_score: int = None,
        assessment_version: str = "v1",
        go_namespace: str = "biological_process",
        # Phase C — source-data versioning kwargs; nullable, stamped at approval.
        go_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> Optional[int]:
        """Create a new KE-GO mapping"""
        mapping_uuid = str(uuid_lib.uuid4())

        # Determine go_direction from go_name if not explicitly provided
        if go_direction is None and go_name:
            detected = detect_go_direction(go_name)
            go_direction = detected if detected != "unspecified" else None

        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ke_go_mappings (ke_id, ke_title, go_id, go_name, connection_type,
                                           confidence_level, evidence_code, created_by, uuid,
                                           go_direction, connection_score, specificity_score,
                                           evidence_score, assessment_version, go_namespace,
                                           go_release_date, aopwiki_snapshot_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    ke_id,
                    ke_title,
                    go_id,
                    go_name,
                    connection_type,
                    confidence_level,
                    evidence_code,
                    created_by,
                    mapping_uuid,
                    go_direction,
                    connection_score,
                    specificity_score,
                    evidence_score,
                    assessment_version,
                    go_namespace,
                    go_release_date,
                    aopwiki_snapshot_date,
                ),
            )

            conn.commit()
            logger.info(
                "Created GO mapping: KE=%s, GO=%s, User=%s, UUID=%s",
                ke_id,
                go_id,
                created_by,
                mapping_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning("Duplicate GO mapping attempted: KE=%s, GO=%s", ke_id, go_id)
            return None
        except Exception as e:
            logger.error("Error creating GO mapping: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def get_all_mappings(self) -> List[Dict]:
        """Get all KE-GO mappings"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, go_id, go_name, go_namespace, go_direction,
                       connection_type, confidence_level, evidence_code, created_by, created_at, updated_at,
                       uuid, approved_by_curator, approved_at_curator, proposed_by,
                       connection_score, specificity_score, evidence_score, assessment_version,
                       suggestion_score,
                       -- Phase E.1 source-data versioning columns.
                       go_release_date, aopwiki_snapshot_date
                FROM ke_go_mappings
                ORDER BY created_at DESC
            """
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_mappings_by_ke(self, ke_id: str) -> List[Dict]:
        """Get all GO mappings for a specific Key Event"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, go_id, go_name, connection_type,
                       confidence_level, evidence_code, created_by, created_at, updated_at,
                       uuid, approved_by_curator, approved_at_curator
                FROM ke_go_mappings
                WHERE ke_id = ?
                ORDER BY created_at DESC
                """,
                (ke_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_mapped_ke_ids(self) -> list:
        """Return distinct KE IDs that have at least one GO mapping."""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT DISTINCT ke_id FROM ke_go_mappings"
            )
            return [row["ke_id"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def check_mapping_exists(self, ke_id: str, go_id: str) -> Dict:
        """Check if KE-GO pair exists"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM ke_go_mappings WHERE ke_id = ? AND go_id = ?",
                (ke_id, go_id),
            )
            pair_match = cursor.fetchone()

            if pair_match:
                return {
                    "pair_exists": True,
                    "message": f"The KE-GO pair ({ke_id}, {go_id}) already exists.",
                }

            cursor = conn.execute(
                "SELECT * FROM ke_go_mappings WHERE ke_id = ?",
                (ke_id,),
            )
            ke_matches = [dict(row) for row in cursor.fetchall()]

            if ke_matches:
                return {
                    "ke_exists": True,
                    "message": f"The KE ID {ke_id} exists but not with GO ID {go_id}.",
                    "ke_matches": ke_matches,
                }

            return {
                "ke_exists": False,
                "pair_exists": False,
                "message": f"The KE ID {ke_id} and GO ID {go_id} are new entries.",
            }
        finally:
            conn.close()

    def check_go_mapping_exists_with_proposals(self, ke_id: str, go_id: str) -> Dict:
        """
        Enriched duplicate check for KE-GO pairs that returns structured blocking payloads.

        Priority order:
        1. pending_proposal — an open proposal already covers this pair (most actionable)
        2. approved_mapping — approved mapping exists, user can submit_revision
        3. ke_exists — KE exists with a different GO term (informational)
        4. nothing found

        Returns one of:
        - blocking_type='pending_proposal' if a pending proposal exists for the pair
        - blocking_type='approved_mapping' if an approved KE-GO pair exists (no pending proposal)
        - ke_exists info if the KE exists with a different GO term
        - ke_exists=False, pair_exists=False if nothing found
        """
        conn = self.db.get_connection()
        try:
            # 0. Check for pending new-pair proposal (mapping_id IS NULL)
            # New-pair proposals are not linked to a mapping row yet, so the JOIN
            # below cannot detect them. Query ke_go_proposals directly by ke_id/go_id.
            cursor = conn.execute(
                """
                SELECT id, proposed_confidence, proposed_connection_type,
                       provider_username, created_at, ke_id, go_id, ke_title, go_name
                FROM ke_go_proposals
                WHERE ke_id = ? AND go_id = ? AND mapping_id IS NULL AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (ke_id, go_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "pending_proposal",
                    "existing": {
                        "proposal_id": row["id"],
                        "ke_id": row["ke_id"],
                        "go_id": row["go_id"],
                        "ke_title": row["ke_title"],
                        "go_name": row["go_name"],
                        "proposed_confidence": row["proposed_confidence"],
                        "proposed_connection_type": row["proposed_connection_type"],
                        "submitted_by": row["provider_username"],
                        "submitted_at": row["created_at"],
                    },
                    "actions": ["flag_stale"],
                }

            # 1. Check for pending proposal on the KE-GO pair (highest priority blocking)
            cursor = conn.execute(
                """
                SELECT p.id, p.proposed_confidence, p.proposed_connection_type,
                       p.provider_username, p.created_at,
                       m.ke_id, m.go_id, m.ke_title, m.go_name
                FROM ke_go_proposals p
                JOIN ke_go_mappings m ON p.mapping_id = m.id
                WHERE m.ke_id = ? AND m.go_id = ? AND p.status = 'pending'
                ORDER BY p.created_at DESC LIMIT 1
                """,
                (ke_id, go_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "pending_proposal",
                    "existing": {
                        "proposal_id": row["id"],
                        "ke_id": row["ke_id"],
                        "go_id": row["go_id"],
                        "ke_title": row["ke_title"],
                        "go_name": row["go_name"],
                        "proposed_confidence": row["proposed_confidence"],
                        "proposed_connection_type": row["proposed_connection_type"],
                        "submitted_by": row["provider_username"],
                        "submitted_at": row["created_at"],
                    },
                    "actions": ["flag_stale"],
                }

            # 2. Check for approved mapping (exact ke_id + go_id pair)
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, go_id, go_name, connection_type,
                       confidence_level, approved_by_curator, approved_at_curator, uuid
                FROM ke_go_mappings WHERE ke_id = ? AND go_id = ?
                """,
                (ke_id, go_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "approved_mapping",
                    "existing": {
                        "ke_id": row["ke_id"],
                        "go_id": row["go_id"],
                        "ke_title": row["ke_title"],
                        "go_name": row["go_name"],
                        "confidence_level": row["confidence_level"],
                        "connection_type": row["connection_type"],
                        "approved_by_curator": row["approved_by_curator"],
                        "approved_at_curator": row["approved_at_curator"],
                        "uuid": row["uuid"],
                        "id": row["id"],
                    },
                    "actions": ["submit_revision"],
                }

            # 3. Check if KE exists with a different GO term (backward-compat ke_exists path)
            cursor = conn.execute(
                "SELECT * FROM ke_go_mappings WHERE ke_id = ?",
                (ke_id,),
            )
            ke_matches = [dict(row) for row in cursor.fetchall()]
            if ke_matches:
                return {
                    "ke_exists": True,
                    "message": f"The KE ID {ke_id} exists but not with GO ID {go_id}.",
                    "ke_matches": ke_matches,
                }

            return {
                "ke_exists": False,
                "pair_exists": False,
                "message": f"The KE ID {ke_id} and GO ID {go_id} are new entries.",
            }
        finally:
            conn.close()


    def get_go_mapping_by_uuid(self, mapping_uuid: str) -> Optional[Dict]:
        """Get a GO mapping by its stable UUID"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, go_id, go_name, connection_type,
                       confidence_level, evidence_code, created_by, created_at, updated_at,
                       uuid, approved_by_curator, approved_at_curator,
                       proposed_by, suggestion_score, go_namespace, go_direction,
                       connection_score, specificity_score, evidence_score, assessment_version
                FROM ke_go_mappings
                WHERE uuid = ?
                """,
                (mapping_uuid,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_go_mapping(
        self,
        mapping_id: int,
        connection_type: str = None,
        confidence_level: str = None,
        updated_by: str = None,
        approved_by_curator: str = None,
        approved_at_curator: str = None,
        suggestion_score: float = None,
        proposed_by: str = None,
        # Phase C — source-data versioning kwargs; nullable, stamped at approval.
        go_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> bool:
        """
        Update an existing KE-GO mapping.

        Uses a whitelist to prevent SQL injection.
        Called at GO proposal approval time to write provenance fields.
        """
        ALLOWED_FIELDS = {
            "connection_type": "connection_type",
            "confidence_level": "confidence_level",
            "updated_by": "updated_by",
            "approved_by_curator": "approved_by_curator",
            "approved_at_curator": "approved_at_curator",
            "suggestion_score": "suggestion_score",
            "proposed_by": "proposed_by",
            "go_release_date": "go_release_date",
            "aopwiki_snapshot_date": "aopwiki_snapshot_date",
        }

        conn = self.db.get_connection()
        try:
            update_clauses = []
            params = []

            update_data = {
                "connection_type": connection_type,
                "confidence_level": confidence_level,
                "updated_by": updated_by,
                "approved_by_curator": approved_by_curator,
                "approved_at_curator": approved_at_curator,
                "suggestion_score": suggestion_score,
                "proposed_by": proposed_by,
                "go_release_date": go_release_date,
                "aopwiki_snapshot_date": aopwiki_snapshot_date,
            }

            for field_name, field_value in update_data.items():
                if field_value is not None and field_name in ALLOWED_FIELDS:
                    update_clauses.append(f"{ALLOWED_FIELDS[field_name]} = ?")
                    params.append(field_value)

            update_clauses.append("updated_at = CURRENT_TIMESTAMP")

            if len(update_clauses) <= 1:  # only the timestamp clause
                return False

            query = f"UPDATE ke_go_mappings SET {', '.join(update_clauses)} WHERE id = ?"
            params.append(mapping_id)

            conn.execute(query, params)
            conn.commit()
            logger.info("Updated GO mapping %s by %s", mapping_id, updated_by)
            return True
        except Exception as e:
            logger.error("Error updating GO mapping: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def _approve_on_conn(
        self,
        conn,
        proposal: dict,
        approved_by_curator: str,
        approved_at_curator: str = None,
        # Phase C — source-data versioning kwargs; nullable, stamped from manifest.
        go_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> str:
        """Approve a GO new-pair proposal on a caller-managed connection.

        Uses the proposal's stored new_pair_confidence_level / proposed_confidence
        directly (skips admin re-score widget per D-15/Pitfall 4). assessment_version
        is always "v1" for the bulk path.

        Executes the create_mapping INSERT and then the update_go_mapping provenance
        UPDATE both on `conn` without calling conn.commit() or conn.close(). Returns
        the new mapping UUID string for the audit log. Raises on IntegrityError or
        any DB error so the bulk caller's outer try/except can roll back the batch.

        Phase 38 (ADMIN-02): caller-managed transaction helper for bulk-approve.
        """
        if approved_at_curator is None:
            approved_at_curator = datetime.utcnow().isoformat()

        mapping_uuid = str(uuid_lib.uuid4())

        # D-15/Pitfall 4: use stored confidence fallback; do NOT read dimension scores.
        confidence_level = (
            proposal.get("new_pair_confidence_level")
            or proposal.get("proposed_confidence")
        )
        # Bulk path always uses v1 (no admin re-score widget)
        assessment_version = "v1"

        go_name = proposal.get("go_name", "")
        go_direction = None
        if go_name:
            detected = detect_go_direction(go_name)
            go_direction = detected if detected != "unspecified" else None

        cursor = conn.execute(
            """
            INSERT INTO ke_go_mappings (ke_id, ke_title, go_id, go_name, connection_type,
                                       confidence_level, evidence_code, created_by, uuid,
                                       go_direction, connection_score, specificity_score,
                                       evidence_score, assessment_version, go_namespace,
                                       go_release_date, aopwiki_snapshot_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                proposal["ke_id"],
                proposal["ke_title"],
                proposal["go_id"],
                go_name,
                proposal.get("new_pair_connection_type") or proposal.get("proposed_connection_type"),
                confidence_level,
                None,   # evidence_code not carried for bulk path
                proposal.get("provider_username") or approved_by_curator,
                mapping_uuid,
                go_direction,
                None,   # connection_score — bulk skips admin re-score (D-15)
                None,   # specificity_score
                None,   # evidence_score
                assessment_version,
                proposal.get("go_namespace", "biological_process"),
                go_release_date,
                aopwiki_snapshot_date,
            ),
        )
        new_mapping_id = cursor.lastrowid

        # Write provenance (approved_by, approved_at, suggestion_score, proposed_by)
        update_clauses = [
            "approved_by_curator = ?",
            "approved_at_curator = ?",
            "proposed_by = ?",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        params = [
            approved_by_curator,
            approved_at_curator,
            proposal.get("provider_username"),
        ]
        proposal_score = proposal.get("suggestion_score")
        if proposal_score is not None:
            update_clauses.append("suggestion_score = ?")
            params.append(proposal_score)

        params.append(new_mapping_id)
        conn.execute(
            f"UPDATE ke_go_mappings SET {', '.join(update_clauses)} WHERE id = ?",
            params,
        )
        logger.info(
            "GO _approve_on_conn: KE=%s, GO=%s, UUID=%s (caller-managed tx)",
            proposal["ke_id"], proposal["go_id"], mapping_uuid,
        )
        return mapping_uuid

    def get_go_mappings_paginated(
        self,
        page: int = 1,
        per_page: int = 50,
        ke_id: str = None,
        go_term_id: str = None,
        confidence_level: str = None,
        direction: str = None,
    ) -> tuple:
        """
        Return (List[Dict], total_count) for approved KE-GO mappings.

        Filters (all optional, combinable):
          ke_id            — exact match on ke_go_mappings.ke_id
          go_term_id       — exact match on ke_go_mappings.go_id
          confidence_level — case-insensitive match on ke_go_mappings.confidence_level
          direction        — exact match on ke_go_mappings.go_direction ("positive" or "negative")

        Returns rows with columns:
          uuid, ke_id, ke_title, go_id, go_name, go_namespace,
          confidence_level, go_direction, approved_by_curator, approved_at_curator, suggestion_score
        Ordered by created_at DESC.
        """
        conditions = []
        params = []

        if ke_id:
            conditions.append("ke_id = ?")
            params.append(ke_id)
        if go_term_id:
            conditions.append("go_id = ?")
            params.append(go_term_id)
        if confidence_level:
            conditions.append("LOWER(confidence_level) = LOWER(?)")
            params.append(confidence_level)
        if direction:
            conditions.append("go_direction = ?")
            params.append(direction)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * per_page

        conn = self.db.get_connection()
        try:
            total = self._count(conn, where, params)
            rows = conn.execute(
                f"""SELECT uuid, ke_id, ke_title, go_id, go_name, go_namespace,
                           confidence_level, go_direction, approved_by_curator, approved_at_curator,
                           suggestion_score, proposed_by, connection_type,
                           connection_score, specificity_score, evidence_score, assessment_version
                    FROM ke_go_mappings {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
            return [dict(r) for r in rows], total
        finally:
            conn.close()

    def delete_mapping(self, mapping_id: int, deleted_by: str = None) -> bool:
        """
        Delete a KE-GO mapping by id.

        Called at GO proposal-approval time when a curator approves a
        deletion proposal (proposed_delete=True) against an existing
        approved mapping. Mirrors MappingModel.delete_mapping (WP).
        """
        conn = self.db.get_connection()
        try:
            conn.execute("DELETE FROM ke_go_mappings WHERE id = ?", (mapping_id,))
            conn.commit()
            logger.info("Deleted GO mapping %s by %s", mapping_id, deleted_by)
            return True
        except Exception as e:
            logger.error("Error deleting GO mapping: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()


class GoProposalModel:
    # Sentinel returned from create_new_pair_go_proposal when the
    # partial-unique index on (ke_id, go_id) WHERE status='pending'
    # AND mapping_id IS NULL rejects a concurrent insert (Phase 32
    # H-2 port from ReactomeProposalModel / ProposalModel).
    DUPLICATE_PENDING = "duplicate_pending"

    def __init__(self, db: Database):
        self.db = db

    def create_proposal(
        self,
        mapping_id: int,
        user_name: str,
        user_email: str,
        user_affiliation: str,
        provider_username: str = None,
        proposed_delete: bool = False,
        proposed_confidence: str = None,
        proposed_connection_type: str = None,
        ke_id: str = None,
        ke_title: str = None,
        go_id: str = None,
        go_name: str = None,
    ) -> Optional[int]:
        """Create a change/deletion proposal against an existing GO mapping.

        mapping_id is set (unlike create_new_pair_go_proposal), so
        approve_go_proposal() applies the change to that mapping rather than
        creating a new one. The ke_id/go_id display fields are stored so the
        admin review queue can render the proposal without re-joining.
        """
        proposal_uuid = str(uuid_lib.uuid4())
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ke_go_proposals (mapping_id, user_name, user_email, user_affiliation,
                                            provider_username, proposed_delete, proposed_confidence,
                                            proposed_connection_type, uuid,
                                            ke_id, ke_title, go_id, go_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    mapping_id,
                    user_name,
                    user_email,
                    user_affiliation,
                    provider_username,
                    proposed_delete,
                    proposed_confidence,
                    proposed_connection_type,
                    proposal_uuid,
                    ke_id,
                    ke_title,
                    go_id,
                    go_name,
                ),
            )

            conn.commit()
            logger.info(
                "Created GO proposal for mapping %s by %s, UUID=%s",
                mapping_id,
                provider_username,
                proposal_uuid,
            )
            return cursor.lastrowid
        except Exception as e:
            logger.error("Error creating GO proposal: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def create_new_pair_go_proposal(
        self,
        ke_id: str,
        ke_title: str,
        go_id: str,
        go_name: str,
        connection_type: str,
        confidence_level: str,
        provider_username: str = None,
        suggestion_score: float = None,
        connection_score: int = None,
        specificity_score: int = None,
        evidence_score: int = None,
        go_namespace: str = "biological_process",
    ) -> Optional[int]:
        """
        Create a new-pair GO proposal where no existing mapping_id exists yet.

        The mapping is created only after an admin explicitly approves this proposal.
        mapping_id is left NULL so approve_go_proposal() knows to call create_mapping()
        at approval time.
        """
        proposal_uuid = str(uuid_lib.uuid4())
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ke_go_proposals (
                    mapping_id, user_name, user_email, user_affiliation,
                    provider_username, proposed_delete, proposed_confidence,
                    proposed_connection_type, uuid, suggestion_score,
                    ke_id, ke_title, go_id, go_name,
                    new_pair_connection_type, new_pair_confidence_level,
                    proposed_connection_score, proposed_specificity_score, proposed_evidence_score,
                    go_namespace
                )
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_username or "curator",
                    "",
                    "",
                    provider_username,
                    False,
                    confidence_level,
                    connection_type,
                    proposal_uuid,
                    suggestion_score,
                    ke_id,
                    ke_title,
                    go_id,
                    go_name,
                    connection_type,
                    confidence_level,
                    connection_score,
                    specificity_score,
                    evidence_score,
                    go_namespace,
                ),
            )
            conn.commit()
            logger.info(
                "Created new-pair GO proposal for %s -> %s by %s, UUID=%s",
                ke_id,
                go_id,
                provider_username,
                proposal_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError as e:
            # Partial-unique index on (ke_id, go_id) WHERE status='pending'
            # AND mapping_id IS NULL rejected a concurrent duplicate. Surface
            # the race via the DUPLICATE_PENDING sentinel; the /submit_go_mapping
            # route maps it to a 409 using check_go_mapping_exists_with_proposals
            # response shape (Phase 32 H-2 port).
            logger.warning(
                "Duplicate pending GO proposal blocked: KE=%s GO=%s (%s)",
                ke_id, go_id, e,
            )
            conn.rollback()
            return self.DUPLICATE_PENDING
        except Exception as e:
            logger.error("Error creating new-pair GO proposal: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def get_all_go_proposals(self, status: str = None) -> List[Dict]:
        """Get all GO proposals, optionally filtered by status."""
        conn = self.db.get_connection()
        try:
            query = """
                SELECT p.*,
                       m.ke_id as mapping_ke_id, m.ke_title as mapping_ke_title,
                       m.go_id as mapping_go_id, m.go_name as mapping_go_name,
                       m.connection_type as current_connection_type,
                       m.confidence_level as current_confidence_level
                FROM ke_go_proposals p
                LEFT JOIN ke_go_mappings m ON p.mapping_id = m.id
            """
            params = ()
            if status:
                query += " WHERE p.status = ?"
                params = (status,)
            query += " ORDER BY p.created_at DESC"
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_go_proposal_by_id(self, proposal_id: int) -> Optional[Dict]:
        """Get a specific GO proposal by ID with mapping details."""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT p.*,
                       p.proposed_connection_score, p.proposed_specificity_score, p.proposed_evidence_score,
                       m.ke_id as mapping_ke_id, m.ke_title as mapping_ke_title,
                       m.go_id as mapping_go_id, m.go_name as mapping_go_name,
                       m.connection_type as current_connection_type,
                       m.confidence_level as current_confidence_level
                FROM ke_go_proposals p
                LEFT JOIN ke_go_mappings m ON p.mapping_id = m.id
                WHERE p.id = ?
                """,
                (proposal_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_go_proposal_status(
        self,
        proposal_id: int,
        status: str,
        admin_username: str = None,
        admin_notes: str = None,
    ) -> bool:
        """Update GO proposal status and admin information."""
        if status not in ["approved", "rejected"]:
            logger.error("Invalid GO proposal status: %s", status)
            return False

        conn = self.db.get_connection()
        try:
            if status == "approved":
                query = """
                    UPDATE ke_go_proposals
                    SET status = ?, approved_by = ?, admin_notes = ?, approved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
            else:
                query = """
                    UPDATE ke_go_proposals
                    SET status = ?, rejected_by = ?, admin_notes = ?, rejected_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
            conn.execute(query, (status, admin_username, admin_notes, proposal_id))
            conn.commit()
            logger.info(
                "Updated GO proposal %s to %s by %s", proposal_id, status, admin_username
            )
            return True
        except Exception as e:
            logger.error("Error updating GO proposal %s: %s", proposal_id, e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def _update_status_on_conn(
        self,
        conn,
        proposal_id: int,
        status: str,
        admin_username: str,
        admin_notes: str,
    ) -> None:
        """Update GO proposal status on a caller-managed connection.

        Raises ValueError for invalid status. Executes the UPDATE on `conn`
        without calling conn.commit() or conn.close(). Any DB error propagates
        to the caller for rollback.

        Phase 38 (ADMIN-02): caller-managed transaction helper for bulk-approve.
        """
        if status not in ["approved", "rejected"]:
            raise ValueError(f"Invalid status: {status}")
        if status == "approved":
            query = """
                UPDATE ke_go_proposals
                SET status = ?, approved_by = ?, admin_notes = ?, approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
        else:
            query = """
                UPDATE ke_go_proposals
                SET status = ?, rejected_by = ?, admin_notes = ?, rejected_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
        conn.execute(query, (status, admin_username, admin_notes, proposal_id))

    def flag_go_proposal_stale(self, proposal_id: int, flagged_by: str) -> bool:
        """
        Flag a pending GO proposal as stale for admin review.

        Args:
            proposal_id: ID of the GO proposal to flag
            flagged_by: Username of curator flagging the proposal

        Returns:
            True if successful, False otherwise
        """
        conn = self.db.get_connection()
        try:
            conn.execute(
                "UPDATE ke_go_proposals SET is_stale = 1 WHERE id = ?",
                (proposal_id,),
            )
            conn.commit()
            logger.info(
                "GO proposal %s flagged as stale by %s", proposal_id, flagged_by
            )
            return True
        except Exception as e:
            logger.error("Error flagging GO proposal %s as stale: %s", proposal_id, e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def find_mapping_by_details(self, ke_id: str, go_id: str) -> Optional[int]:
        """Find GO mapping ID by KE and GO IDs"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id FROM ke_go_mappings WHERE ke_id = ? AND go_id = ?",
                (ke_id, go_id),
            )
            row = cursor.fetchone()
            return row["id"] if row else None
        finally:
            conn.close()


class CacheModel:
    def __init__(self, db: Database):
        self.db = db

    def get_cached_response(self, endpoint: str, query_hash: str) -> Optional[str]:
        """Get cached SPARQL response if valid"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT response_data FROM sparql_cache 
                WHERE endpoint = ? AND query_hash = ? AND expires_at > CURRENT_TIMESTAMP
            """,
                (endpoint, query_hash),
            )

            row = cursor.fetchone()
            return row["response_data"] if row else None
        finally:
            conn.close()

    def cache_response(
        self, endpoint: str, query_hash: str, response_data: str, expiry_hours: int = 24
    ) -> bool:
        """Cache SPARQL response"""
        # Validate expiry_hours to prevent SQL injection
        if (
            not isinstance(expiry_hours, int) or expiry_hours < 1 or expiry_hours > 168
        ):  # Max 1 week
            expiry_hours = 24

        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO sparql_cache (endpoint, query_hash, response_data, expires_at)
                VALUES (?, ?, ?, datetime('now', '+' || ? || ' hours'))
            """,
                (endpoint, query_hash, response_data, str(expiry_hours)),
            )

            conn.commit()
            return True
        except Exception as e:
            logger.error("Error caching response: %s", e)
            return False
        finally:
            conn.close()

    def cleanup_expired_cache(self):
        """Remove expired cache entries"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                DELETE FROM sparql_cache WHERE expires_at <= CURRENT_TIMESTAMP
            """
            )
            deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info("Cleaned up %d expired cache entries", deleted_count)
        except Exception as e:
            logger.error("Error cleaning cache: %s", e)
        finally:
            conn.close()


class GuestCodeModel:
    def __init__(self, db: Database):
        self.db = db

    def create_code(
        self, label: str, created_by: str, expires_at: str, max_uses: int = 1
    ) -> Optional[str]:
        """
        Create a new guest access code

        Args:
            label: Descriptive label for this code (e.g. 'workshop-2025')
            created_by: Admin username who created the code
            expires_at: ISO timestamp when the code expires
            max_uses: Maximum number of times the code can be used

        Returns:
            The generated code string, or None on failure
        """
        code = secrets.token_urlsafe(6)
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO guest_codes (code, label, created_by, expires_at, max_uses)
                VALUES (?, ?, ?, ?, ?)
            """,
                (code, label, created_by, expires_at, max_uses),
            )
            conn.commit()
            logger.info(
                "Created guest code for label=%s by %s (max_uses=%d)",
                label,
                created_by,
                max_uses,
            )
            return code
        except sqlite3.IntegrityError:
            logger.warning("Duplicate guest code generated, retrying")
            conn.rollback()
            # Retry once with a new code
            code = secrets.token_urlsafe(6)
            try:
                conn.execute(
                    """
                    INSERT INTO guest_codes (code, label, created_by, expires_at, max_uses)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (code, label, created_by, expires_at, max_uses),
                )
                conn.commit()
                return code
            except Exception:
                conn.rollback()
                return None
        except Exception as e:
            logger.error("Error creating guest code: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def validate_code(self, code: str) -> Optional[Dict]:
        """
        Validate a guest access code and increment its use count

        Args:
            code: The access code to validate

        Returns:
            Dict with code details if valid, None if invalid/expired/revoked/exhausted
        """
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, code, label, expires_at, max_uses, use_count, is_revoked
                FROM guest_codes
                WHERE code = ?
            """,
                (code,),
            )
            row = cursor.fetchone()

            if not row:
                return None

            row_dict = dict(row)

            # Check revoked
            if row_dict["is_revoked"]:
                return None

            # Check expired
            try:
                expires = datetime.fromisoformat(row_dict["expires_at"])
                if expires < datetime.utcnow():
                    return None
            except (ValueError, TypeError):
                return None

            # Check usage limit
            if row_dict["use_count"] >= row_dict["max_uses"]:
                return None

            # Increment use count
            conn.execute(
                "UPDATE guest_codes SET use_count = use_count + 1 WHERE id = ?",
                (row_dict["id"],),
            )
            conn.commit()

            logger.info("Guest code validated for label=%s", row_dict["label"])
            return row_dict

        except Exception as e:
            logger.error("Error validating guest code: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def get_all_codes(self) -> List[Dict]:
        """
        Get all guest codes with computed status

        Returns:
            List of code dicts with added 'status' field
        """
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, code, label, created_by, created_at, expires_at,
                       max_uses, use_count, is_revoked, revoked_at, revoked_by
                FROM guest_codes
                ORDER BY created_at DESC
            """
            )
            codes = [dict(row) for row in cursor.fetchall()]

            now = datetime.utcnow()
            for code in codes:
                if code["is_revoked"]:
                    code["status"] = "revoked"
                elif code["use_count"] >= code["max_uses"]:
                    code["status"] = "exhausted"
                else:
                    try:
                        expires = datetime.fromisoformat(code["expires_at"])
                        code["status"] = "expired" if expires < now else "active"
                    except (ValueError, TypeError):
                        code["status"] = "unknown"

            return codes
        finally:
            conn.close()

    def revoke_code(self, code_id: int, revoked_by: str) -> bool:
        """
        Revoke a guest code

        Args:
            code_id: Database ID of the code to revoke
            revoked_by: Admin username revoking the code

        Returns:
            True if successful
        """
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                UPDATE guest_codes
                SET is_revoked = TRUE, revoked_at = CURRENT_TIMESTAMP, revoked_by = ?
                WHERE id = ?
            """,
                (revoked_by, code_id),
            )
            conn.commit()
            logger.info("Guest code %d revoked by %s", code_id, revoked_by)
            return True
        except Exception as e:
            logger.error("Error revoking guest code: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def delete_code(self, code_id: int) -> bool:
        """
        Delete a guest code

        Args:
            code_id: Database ID of the code to delete

        Returns:
            True if successful
        """
        conn = self.db.get_connection()
        try:
            conn.execute("DELETE FROM guest_codes WHERE id = ?", (code_id,))
            conn.commit()
            logger.info("Guest code %d deleted", code_id)
            return True
        except Exception as e:
            logger.error("Error deleting guest code: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()


class KeDescriptionOverrideModel:
    """Model for managing per-KE description override toggles."""

    def __init__(self, db: Database):
        self.db = db

    def get_disabled_ke_ids(self) -> set:
        """Return set of ke_ids where description is disabled."""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT ke_id FROM ke_description_overrides WHERE description_disabled = 1"
            )
            return {row["ke_id"] for row in cursor.fetchall()}
        except Exception as e:
            logger.error("Error fetching disabled KE IDs: %s", e)
            return set()
        finally:
            conn.close()

    def toggle_override(self, ke_id: str, disabled: bool, updated_by: str) -> bool:
        """Insert or replace a per-KE description override.

        Args:
            ke_id: Key Event ID (e.g., "KE 55")
            disabled: True to disable description for this KE
            updated_by: Admin username who made the change

        Returns:
            True if successful
        """
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO ke_description_overrides
                    (ke_id, description_disabled, updated_by, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (ke_id, 1 if disabled else 0, updated_by),
            )
            conn.commit()
            logger.info("KE description override set: %s disabled=%s by %s", ke_id, disabled, updated_by)
            return True
        except Exception as e:
            logger.error("Error toggling KE description override: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def get_all_overrides(self) -> Dict[str, bool]:
        """Return {ke_id: disabled_bool} for all overrides."""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT ke_id, description_disabled FROM ke_description_overrides"
            )
            return {row["ke_id"]: bool(row["description_disabled"]) for row in cursor.fetchall()}
        except Exception as e:
            logger.error("Error fetching KE description overrides: %s", e)
            return {}
        finally:
            conn.close()


class ReactomeMappingModel(MappingCountsMixin):
    """Model for KE-Reactome pathway mappings"""

    TABLE = "ke_reactome_mappings"

    def __init__(self, db: Database):
        self.db = db

    def create_mapping(
        self,
        ke_id: str,
        ke_title: str,
        reactome_id: str,
        pathway_name: str,
        species: str = 'Homo sapiens',
        confidence_level: str = 'low',
        suggestion_score: float = None,
        created_by: str = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
        # Phase C — source-data versioning kwargs; nullable, stamped from manifest.
        reactome_release_version: Optional[str] = None,
        reactome_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> Optional[int]:
        """Create a new KE-Reactome mapping"""
        mapping_uuid = str(uuid_lib.uuid4())
        assessment_version = _classify_assessment_version(
            proposed_relationship, proposed_basis, proposed_specificity, proposed_coverage
        )
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ke_reactome_mappings
                    (ke_id, ke_title, reactome_id, pathway_name, species,
                     confidence_level, suggestion_score, created_by, uuid,
                     proposed_relationship, proposed_basis,
                     proposed_specificity, proposed_coverage,
                     assessment_version,
                     reactome_release_version, reactome_release_date,
                     aopwiki_snapshot_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ke_id, ke_title, reactome_id, pathway_name, species,
                 confidence_level, suggestion_score, created_by, mapping_uuid,
                 proposed_relationship, proposed_basis,
                 proposed_specificity, proposed_coverage,
                 assessment_version,
                 reactome_release_version, reactome_release_date,
                 aopwiki_snapshot_date),
            )
            conn.commit()
            logger.info(
                "Created Reactome mapping: KE=%s, Reactome=%s, User=%s, UUID=%s",
                ke_id, reactome_id, created_by, mapping_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning(
                "Duplicate Reactome mapping: KE=%s, Reactome=%s", ke_id, reactome_id
            )
            return None
        except Exception as e:
            logger.error("Error creating Reactome mapping: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def create_approved_mapping(
        self,
        proposal_id: int,
        approved_by_curator: str,
        approved_at_curator: str = None,
        # Phase C — source-data versioning kwargs; nullable, stamped from manifest.
        reactome_release_version: Optional[str] = None,
        reactome_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> Optional[int]:
        """Create an approved KE-Reactome mapping in a single INSERT.

        Phase 25 review H-1: single-INSERT carry — all carry-fields written
        up front so partial state is impossible.

        Phase 34 (ASMT-10): the INSERT column list is now driven by the
        module-level constant REACTOME_PROPOSAL_CARRY_FIELDS; extending that
        constant automatically widens this INSERT, and the round-trip test
        asserts every constant element is actually written.

        Args:
            proposal_id: ID of the pending ke_reactome_proposals row to approve.
            approved_by_curator: Provider-prefixed username of the approving admin.
            approved_at_curator: ISO timestamp of approval (defaults to utcnow).

        Returns the new mapping id, or None on IntegrityError (UNIQUE on
        (ke_id, reactome_id)) or other DB error.
        """
        if approved_at_curator is None:
            approved_at_curator = datetime.utcnow().isoformat()

        mapping_uuid = str(uuid_lib.uuid4())
        conn = self.db.get_connection()
        try:
            # Load the proposal row — all carry-field values live here.
            proposal_row = conn.execute(
                "SELECT * FROM ke_reactome_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if proposal_row is None:
                logger.error(
                    "create_approved_mapping: proposal %s not found", proposal_id
                )
                return None
            proposal_row = dict(proposal_row)

            # Phase 34 (ASMT-10): carry-field column list is constant-driven.
            # Extending REACTOME_PROPOSAL_CARRY_FIELDS automatically widens
            # the INSERT; the round-trip test guards against silent drops.
            #
            # Column name alias: ke_reactome_proposals stores confidence as
            # 'new_pair_confidence_level' (or 'proposed_confidence' for revision
            # proposals), while the mapping table expects 'confidence_level'.
            # All other REACTOME_PROPOSAL_CARRY_FIELDS names match directly.
            _proposal_col_alias = {
                'confidence_level': (
                    proposal_row.get("new_pair_confidence_level")
                    or proposal_row.get("proposed_confidence")
                    or proposal_row.get("confidence_level")
                ),
            }
            carry_cols = list(REACTOME_PROPOSAL_CARRY_FIELDS)
            carry_values = tuple(
                _proposal_col_alias[col] if col in _proposal_col_alias
                else proposal_row.get(col)
                for col in carry_cols
            )

            # Derive ke_id, reactome_id from proposal for fixed cols
            ke_id = proposal_row.get("ke_id")
            ke_title = proposal_row.get("ke_title")
            reactome_id = proposal_row.get("reactome_id")
            proposed_by = proposal_row.get("provider_username")

            # Compute assessment_version from carried assessment fields
            assessment_version = _classify_assessment_version(
                proposal_row.get("proposed_relationship"),
                proposal_row.get("proposed_basis"),
                proposal_row.get("proposed_specificity"),
                proposal_row.get("proposed_coverage"),
            )

            fixed_cols = (
                'ke_id', 'ke_title', 'reactome_id',
                'created_by', 'uuid',
                'approved_by_curator', 'approved_at_curator', 'proposed_by',
                'assessment_version',
                # Phase C source-data versioning columns. Stamped from the
                # deployed manifest, NOT carried from the proposal — so they
                # ride the fixed-cols path even though they're nullable.
                'reactome_release_version', 'reactome_release_date',
                'aopwiki_snapshot_date',
            )
            fixed_values = (
                ke_id, ke_title, reactome_id,
                proposed_by or approved_by_curator,
                mapping_uuid,
                approved_by_curator, approved_at_curator, proposed_by,
                assessment_version,
                reactome_release_version, reactome_release_date,
                aopwiki_snapshot_date,
            )

            all_cols = fixed_cols + tuple(carry_cols)
            placeholders = ', '.join('?' for _ in all_cols)
            cursor = conn.execute(
                f"INSERT INTO ke_reactome_mappings ({', '.join(all_cols)}) "
                f"VALUES ({placeholders})",
                fixed_values + carry_values,
            )
            conn.commit()
            logger.info(
                "Created approved Reactome mapping: KE=%s, Reactome=%s, "
                "approved_by=%s, proposed_by=%s, UUID=%s",
                ke_id, reactome_id, approved_by_curator, proposed_by,
                mapping_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning(
                "Duplicate Reactome mapping on approve: proposal=%s",
                proposal_id,
            )
            return None
        except Exception as e:
            logger.error("Error creating approved Reactome mapping: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def _create_approved_on_conn(
        self,
        conn,
        proposal_id: int,
        approved_by_curator: str,
        approved_at_curator: str = None,
        # Phase C — source-data versioning kwargs; nullable, stamped from manifest.
        reactome_release_version: Optional[str] = None,
        reactome_release_date: Optional[str] = None,
        aopwiki_snapshot_date: Optional[str] = None,
    ) -> str:
        """Single-INSERT approved Reactome mapping on a caller-managed connection.

        Body is identical to create_approved_mapping (REACTOME_PROPOSAL_CARRY_FIELDS-
        driven INSERT, new_pair_confidence_level alias map, _classify_assessment_version
        call) minus conn management. Returns the new mapping UUID string for the audit
        log. Raises on IntegrityError or any DB error — caller is responsible for
        rollback; no conn.commit() or conn.close() is called.

        Phase 38 (ADMIN-02): caller-managed transaction helper for bulk-approve.
        """
        if approved_at_curator is None:
            approved_at_curator = datetime.utcnow().isoformat()

        mapping_uuid = str(uuid_lib.uuid4())

        # Load the proposal row — all carry-field values live here.
        proposal_row = conn.execute(
            "SELECT * FROM ke_reactome_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if proposal_row is None:
            raise ValueError(
                f"_create_approved_on_conn: proposal {proposal_id} not found"
            )
        proposal_row = dict(proposal_row)

        # Phase 34 (ASMT-10): carry-field column list is constant-driven.
        # Column name alias: ke_reactome_proposals stores confidence as
        # 'new_pair_confidence_level' while the mapping table expects 'confidence_level'.
        _proposal_col_alias = {
            'confidence_level': (
                proposal_row.get("new_pair_confidence_level")
                or proposal_row.get("proposed_confidence")
                or proposal_row.get("confidence_level")
            ),
        }
        carry_cols = list(REACTOME_PROPOSAL_CARRY_FIELDS)
        carry_values = tuple(
            _proposal_col_alias[col] if col in _proposal_col_alias
            else proposal_row.get(col)
            for col in carry_cols
        )

        ke_id = proposal_row.get("ke_id")
        ke_title = proposal_row.get("ke_title")
        reactome_id = proposal_row.get("reactome_id")
        proposed_by = proposal_row.get("provider_username")

        assessment_version = _classify_assessment_version(
            proposal_row.get("proposed_relationship"),
            proposal_row.get("proposed_basis"),
            proposal_row.get("proposed_specificity"),
            proposal_row.get("proposed_coverage"),
        )

        fixed_cols = (
            'ke_id', 'ke_title', 'reactome_id',
            'created_by', 'uuid',
            'approved_by_curator', 'approved_at_curator', 'proposed_by',
            'assessment_version',
            'reactome_release_version', 'reactome_release_date',
            'aopwiki_snapshot_date',
        )
        fixed_values = (
            ke_id, ke_title, reactome_id,
            proposed_by or approved_by_curator,
            mapping_uuid,
            approved_by_curator, approved_at_curator, proposed_by,
            assessment_version,
            reactome_release_version, reactome_release_date,
            aopwiki_snapshot_date,
        )

        all_cols = fixed_cols + tuple(carry_cols)
        placeholders = ', '.join('?' for _ in all_cols)
        conn.execute(
            f"INSERT INTO ke_reactome_mappings ({', '.join(all_cols)}) "
            f"VALUES ({placeholders})",
            fixed_values + carry_values,
        )
        logger.info(
            "Reactome _create_approved_on_conn: KE=%s, Reactome=%s, "
            "approved_by=%s, UUID=%s (caller-managed tx)",
            ke_id, reactome_id, approved_by_curator, mapping_uuid,
        )
        return mapping_uuid

    def delete_mapping(self, mapping_id: int) -> bool:
        """Delete a mapping row by id. Used to roll back on partial failure
        of the approve flow if the proposal-status update fails after the
        mapping has been created (Phase 25 review H-1).
        """
        conn = self.db.get_connection()
        try:
            conn.execute(
                "DELETE FROM ke_reactome_mappings WHERE id = ?", (mapping_id,)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(
                "Error deleting Reactome mapping %s during rollback: %s",
                mapping_id, e,
            )
            conn.rollback()
            return False
        finally:
            conn.close()

    def get_all_mappings(self) -> List[Dict]:
        """Get all KE-Reactome mappings"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, reactome_id, pathway_name, species,
                       confidence_level, suggestion_score, proposed_by, created_by,
                       uuid, approved_by_curator, approved_at_curator,
                       created_at, updated_at,
                       -- Phase 34 ASMT-08: do NOT drop these or insert above;
                       -- positional consumers append at end (Pitfall 5)
                       proposed_relationship, proposed_basis,
                       proposed_specificity, proposed_coverage,
                       assessment_version,
                       -- Phase E.1 source-data versioning columns.
                       reactome_release_version, reactome_release_date,
                       aopwiki_snapshot_date
                FROM ke_reactome_mappings
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_reactome_mappings_paginated(
        self,
        page: int = 1,
        per_page: int = 50,
        ke_id: str = None,
        reactome_id: str = None,
        confidence_level: str = None,
        ke_ids: list = None,
    ) -> tuple:
        """Return (List[Dict], total_count) for approved KE-Reactome mappings.

        Mirrors GoMappingModel.get_go_mappings_paginated (src/core/models.py:2133)
        with `direction` dropped (Reactome has no direction field) and `ke_ids`
        added (Phase 26 D-08: AOP-resolved KE filter).

        Filters (all optional, combinable):
          ke_id            — exact match on ke_reactome_mappings.ke_id
          reactome_id      — exact match on ke_reactome_mappings.reactome_id
          confidence_level — case-insensitive match on ke_reactome_mappings.confidence_level
          ke_ids           — list-or-None. None = no AOP filter; [] = AOP resolved
                             but no KEs found, returns ([], 0) without SQL;
                             [ids...] = parametrised IN clause AND-combined with
                             other filters.

        Returns rows with columns:
          uuid, ke_id, ke_title, reactome_id, pathway_name, species,
          confidence_level, approved_by_curator, approved_at_curator,
          suggestion_score, proposed_by
        Ordered by created_at DESC.
        """
        # Short-circuit: AOP resolved but no KEs => empty result without SQL.
        if ke_ids is not None and not ke_ids:
            return [], 0

        conditions = []
        params = []

        if ke_id:
            conditions.append("ke_id = ?")
            params.append(ke_id)
        if ke_ids:
            placeholders = ",".join("?" * len(ke_ids))
            conditions.append(f"ke_id IN ({placeholders})")
            params.extend(ke_ids)
        if reactome_id:
            conditions.append("reactome_id = ?")
            params.append(reactome_id)
        if confidence_level:
            conditions.append("LOWER(confidence_level) = LOWER(?)")
            params.append(confidence_level)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = (page - 1) * per_page

        conn = self.db.get_connection()
        try:
            total = self._count(conn, where, params)
            rows = conn.execute(
                f"""SELECT uuid, ke_id, ke_title, reactome_id, pathway_name, species,
                           confidence_level, approved_by_curator, approved_at_curator,
                           suggestion_score, proposed_by,
                           -- Phase 34 ASMT-08: assessment columns flow through to
                           -- /api/v1/reactome-mappings serializer. NOTE: no
                           -- `connection_type` column on ke_reactome_mappings.
                           proposed_relationship, proposed_basis,
                           proposed_specificity, proposed_coverage,
                           assessment_version
                    FROM ke_reactome_mappings {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
            return [dict(r) for r in rows], total
        finally:
            conn.close()

    def get_reactome_mapping_by_uuid(self, mapping_uuid: str) -> Optional[Dict]:
        """Return a single approved KE-Reactome mapping dict, or None.

        SELECT column list mirrors get_reactome_mappings_paginated so the
        v1_api serialiser (Phase 26 D-05) sees the same shape from both
        list and detail routes.
        """
        conn = self.db.get_connection()
        try:
            row = conn.execute(
                """SELECT uuid, ke_id, ke_title, reactome_id, pathway_name, species,
                          confidence_level, approved_by_curator, approved_at_curator,
                          suggestion_score, proposed_by,
                          -- Phase 34 ASMT-08: assessment columns flow through to
                          -- /api/v1/reactome-mappings/<uuid> serializer. NOTE:
                          -- no `connection_type` column on ke_reactome_mappings.
                          proposed_relationship, proposed_basis,
                          proposed_specificity, proposed_coverage,
                          assessment_version
                   FROM ke_reactome_mappings
                   WHERE uuid = ?""",
                (mapping_uuid,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_mappings_by_ke(self, ke_id: str) -> List[Dict]:
        """Get all Reactome mappings for a specific Key Event"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, reactome_id, pathway_name, species,
                       confidence_level, suggestion_score, proposed_by, created_by,
                       uuid, approved_by_curator, approved_at_curator,
                       created_at, updated_at
                FROM ke_reactome_mappings
                WHERE ke_id = ?
                ORDER BY created_at DESC
                """,
                (ke_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def check_mapping_exists(self, ke_id: str, reactome_id: str) -> Dict:
        """Check if KE-Reactome pair exists"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM ke_reactome_mappings WHERE ke_id = ? AND reactome_id = ?",
                (ke_id, reactome_id),
            )
            pair_match = cursor.fetchone()
            if pair_match:
                return {
                    "pair_exists": True,
                    "message": f"The KE-Reactome pair ({ke_id}, {reactome_id}) already exists.",
                }
            cursor = conn.execute(
                "SELECT * FROM ke_reactome_mappings WHERE ke_id = ?",
                (ke_id,),
            )
            ke_matches = [dict(row) for row in cursor.fetchall()]
            if ke_matches:
                return {
                    "ke_exists": True,
                    "message": f"The KE ID {ke_id} exists but not with Reactome ID {reactome_id}.",
                    "ke_matches": ke_matches,
                }
            return {
                "ke_exists": False,
                "pair_exists": False,
                "message": f"The KE ID {ke_id} and Reactome ID {reactome_id} are new entries.",
            }
        finally:
            conn.close()

    def get_mapped_ke_ids(self) -> list:
        """Return distinct KE IDs that have at least one Reactome mapping."""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT DISTINCT ke_id FROM ke_reactome_mappings"
            )
            return [row["ke_id"] for row in cursor.fetchall()]
        finally:
            conn.close()

    def update_reactome_mapping(
        self,
        mapping_id: int,
        approved_by_curator: str = None,
        approved_at_curator: str = None,
        suggestion_score: float = None,
        proposed_by: str = None,
    ) -> bool:
        """Update a KE-Reactome mapping at proposal-approval time.

        Drops connection_type and confidence_level from the GO equivalent —
        Reactome has no connection_type, and confidence is locked at proposal
        creation (Phase 25 CONTEXT D-02). Also drops updated_by: the
        ke_reactome_mappings schema (Phase 24, models.py:204-226) intentionally
        omits an updated_by column, so attribution lives only in
        approved_by_curator + proposed_by. Uses an ALLOWED_FIELDS whitelist
        to prevent SQL injection.
        """
        ALLOWED_FIELDS = {
            "approved_by_curator": "approved_by_curator",
            "approved_at_curator": "approved_at_curator",
            "suggestion_score": "suggestion_score",
            "proposed_by": "proposed_by",
        }
        conn = self.db.get_connection()
        try:
            update_clauses = []
            params = []
            update_data = {
                "approved_by_curator": approved_by_curator,
                "approved_at_curator": approved_at_curator,
                "suggestion_score": suggestion_score,
                "proposed_by": proposed_by,
            }
            for field_name, field_value in update_data.items():
                if field_value is not None and field_name in ALLOWED_FIELDS:
                    update_clauses.append(f"{ALLOWED_FIELDS[field_name]} = ?")
                    params.append(field_value)
            update_clauses.append("updated_at = CURRENT_TIMESTAMP")
            if len(update_clauses) <= 1:  # only the timestamp clause
                return False
            query = (
                f"UPDATE ke_reactome_mappings SET {', '.join(update_clauses)} "
                "WHERE id = ?"
            )
            params.append(mapping_id)
            conn.execute(query, params)
            conn.commit()
            logger.info(
                "Updated Reactome mapping %s by curator %s",
                mapping_id, approved_by_curator,
            )
            return True
        except Exception as e:
            logger.error("Error updating Reactome mapping: %s", e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def check_reactome_mapping_exists_with_proposals(
        self, ke_id: str, reactome_id: str
    ) -> Dict:
        """Enriched duplicate check for KE-Reactome pairs.

        Mirrors GoMappingModel.check_go_mapping_exists_with_proposals but
        omits connection_type from the existing dict (Reactome has none).
        Returned `existing` payload for pending_proposal intentionally
        excludes admin_notes (info-disclosure mitigation, Phase 25
        threat_model).

        Priority order:
        1. pending_proposal — open proposal already covers this pair
        2. approved_mapping — approved mapping exists
        3. ke_exists — KE exists with a different Reactome pathway
        4. nothing found
        """
        conn = self.db.get_connection()
        try:
            # 1. Check for pending new-pair proposal
            cursor = conn.execute(
                """
                SELECT id, new_pair_confidence_level, proposed_confidence,
                       provider_username, created_at, ke_id, reactome_id,
                       ke_title, pathway_name
                FROM ke_reactome_proposals
                WHERE ke_id = ? AND reactome_id = ?
                  AND mapping_id IS NULL AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (ke_id, reactome_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "pending_proposal",
                    "existing": {
                        "proposal_id": row["id"],
                        "ke_id": row["ke_id"],
                        "reactome_id": row["reactome_id"],
                        "ke_title": row["ke_title"],
                        "pathway_name": row["pathway_name"],
                        "proposed_confidence": (
                            row["new_pair_confidence_level"]
                            or row["proposed_confidence"]
                        ),
                        "submitted_by": row["provider_username"],
                        "submitted_at": row["created_at"],
                    },
                    "actions": ["flag_stale"],
                }

            # 2. Check for pending proposal linked to an existing mapping
            cursor = conn.execute(
                """
                SELECT p.id, p.new_pair_confidence_level, p.proposed_confidence,
                       p.provider_username, p.created_at,
                       m.ke_id, m.reactome_id, m.ke_title, m.pathway_name
                FROM ke_reactome_proposals p
                JOIN ke_reactome_mappings m ON p.mapping_id = m.id
                WHERE m.ke_id = ? AND m.reactome_id = ? AND p.status = 'pending'
                ORDER BY p.created_at DESC LIMIT 1
                """,
                (ke_id, reactome_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "pending_proposal",
                    "existing": {
                        "proposal_id": row["id"],
                        "ke_id": row["ke_id"],
                        "reactome_id": row["reactome_id"],
                        "ke_title": row["ke_title"],
                        "pathway_name": row["pathway_name"],
                        "proposed_confidence": (
                            row["new_pair_confidence_level"]
                            or row["proposed_confidence"]
                        ),
                        "submitted_by": row["provider_username"],
                        "submitted_at": row["created_at"],
                    },
                    "actions": ["flag_stale"],
                }

            # 3. Check for approved mapping (exact ke_id + reactome_id pair)
            cursor = conn.execute(
                """
                SELECT id, ke_id, ke_title, reactome_id, pathway_name,
                       species, confidence_level,
                       approved_by_curator, approved_at_curator, uuid
                FROM ke_reactome_mappings
                WHERE ke_id = ? AND reactome_id = ?
                """,
                (ke_id, reactome_id),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "pair_exists": True,
                    "blocking_type": "approved_mapping",
                    "existing": {
                        "ke_id": row["ke_id"],
                        "reactome_id": row["reactome_id"],
                        "ke_title": row["ke_title"],
                        "pathway_name": row["pathway_name"],
                        "species": row["species"],
                        "confidence_level": row["confidence_level"],
                        "approved_by_curator": row["approved_by_curator"],
                        "approved_at_curator": row["approved_at_curator"],
                        "uuid": row["uuid"],
                        "id": row["id"],
                    },
                    "actions": ["submit_revision"],
                }

            # 4. Check if KE exists with a different Reactome pathway
            cursor = conn.execute(
                "SELECT * FROM ke_reactome_mappings WHERE ke_id = ?",
                (ke_id,),
            )
            ke_matches = [dict(row) for row in cursor.fetchall()]
            if ke_matches:
                return {
                    "pair_exists": False,
                    "blocking_type": None,
                    "existing": None,
                    "ke_exists": True,
                    "ke_matches": ke_matches,
                    "actions": [],
                }

            return {
                "pair_exists": False,
                "blocking_type": None,
                "existing": None,
                "ke_exists": False,
                "actions": [],
            }
        finally:
            conn.close()


class ReactomeProposalModel:
    """Model for KE-Reactome mapping proposals"""

    def __init__(self, db: Database):
        self.db = db

    def create_proposal(
        self,
        mapping_id: int,
        user_name: str,
        user_email: str,
        user_affiliation: str,
        provider_username: str = None,
        proposed_delete: bool = False,
        proposed_confidence: str = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
        ke_id: str = None,
        ke_title: str = None,
        reactome_id: str = None,
        pathway_name: str = None,
    ) -> Optional[int]:
        """Create a change/deletion proposal against an existing Reactome mapping.

        mapping_id is set (unlike create_new_pair_reactome_proposal), so
        approve_reactome_proposal() applies the change to that mapping. The
        ke_id/reactome_id display fields are stored so the admin review queue
        can render the proposal.
        """
        proposal_uuid = str(uuid_lib.uuid4())
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ke_reactome_proposals (mapping_id, user_name, user_email, user_affiliation,
                                                   provider_username, proposed_delete, proposed_confidence,
                                                   uuid, proposed_relationship, proposed_basis,
                                                   proposed_specificity, proposed_coverage,
                                                   ke_id, ke_title, reactome_id, pathway_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mapping_id, user_name, user_email, user_affiliation,
                 provider_username, proposed_delete, proposed_confidence,
                 proposal_uuid, proposed_relationship, proposed_basis,
                 proposed_specificity, proposed_coverage,
                 ke_id, ke_title, reactome_id, pathway_name),
            )
            conn.commit()
            logger.info(
                "Created Reactome proposal for mapping %s by %s, UUID=%s",
                mapping_id, provider_username, proposal_uuid,
            )
            return cursor.lastrowid
        except Exception as e:
            logger.error("Error creating Reactome proposal: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def find_mapping_by_details(self, ke_id: str, reactome_id: str) -> Optional[int]:
        """Find a Reactome mapping id by KE and Reactome IDs.

        Mirrors GoProposalModel.find_mapping_by_details; used by the
        /submit_reactome_proposal route to resolve the mapping a change or
        deletion proposal targets.
        """
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id FROM ke_reactome_mappings WHERE ke_id = ? AND reactome_id = ?",
                (ke_id, reactome_id),
            )
            row = cursor.fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    # Sentinel returned from create_new_pair_reactome_proposal when the
    # partial-unique index on (ke_id, reactome_id) WHERE status='pending'
    # rejects a concurrent insert (Phase 25 review H-2). The route layer
    # maps this to a "duplicate pending" 409-style response so the user
    # gets a clear error instead of a generic 500.
    DUPLICATE_PENDING = "duplicate_pending"

    def create_new_pair_reactome_proposal(
        self,
        ke_id: str,
        ke_title: str,
        reactome_id: str,
        pathway_name: str,
        confidence_level: str,
        species: str = 'Homo sapiens',
        provider_username: str = None,
        suggestion_score: float = None,
        proposed_relationship: Optional[str] = None,
        proposed_basis: Optional[str] = None,
        proposed_specificity: Optional[str] = None,
        proposed_coverage: Optional[str] = None,
    ):
        """
        Create a new-pair Reactome proposal where no existing mapping_id exists yet.
        The mapping is created only after admin approval. mapping_id is NULL.

        Returns:
            int          — the newly inserted proposal id on success
            "duplicate_pending" — IntegrityError on the partial-unique index
                          (Phase 25 H-2): another pending proposal already
                          covers this (ke_id, reactome_id) pair
            None         — any other failure
        """
        proposal_uuid = str(uuid_lib.uuid4())
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO ke_reactome_proposals (
                    mapping_id, user_name, user_email, user_affiliation,
                    provider_username, proposed_delete, proposed_confidence,
                    uuid, suggestion_score,
                    ke_id, ke_title, reactome_id, pathway_name, species,
                    new_pair_confidence_level,
                    proposed_relationship, proposed_basis,
                    proposed_specificity, proposed_coverage
                )
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_username or "curator", "", "",
                    provider_username, False, confidence_level,
                    proposal_uuid, suggestion_score,
                    ke_id, ke_title, reactome_id, pathway_name, species,
                    confidence_level,
                    proposed_relationship, proposed_basis,
                    proposed_specificity, proposed_coverage,
                ),
            )
            conn.commit()
            logger.info(
                "Created new-pair Reactome proposal for %s -> %s by %s, UUID=%s",
                ke_id, reactome_id, provider_username, proposal_uuid,
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError as e:
            # Partial-unique index on (ke_id, reactome_id) WHERE
            # status='pending' AND mapping_id IS NULL fired — concurrent
            # submit beat us. Return a sentinel the route layer can map
            # to the same blocking-pending response /check_reactome_entry
            # serves (Phase 25 review H-2).
            logger.warning(
                "Duplicate pending Reactome proposal blocked: "
                "KE=%s Reactome=%s (%s)", ke_id, reactome_id, e,
            )
            conn.rollback()
            return self.DUPLICATE_PENDING
        except Exception as e:
            logger.error("Error creating Reactome proposal: %s", e)
            conn.rollback()
            return None
        finally:
            conn.close()

    def get_pending_proposals(self) -> List[Dict]:
        """Get all pending Reactome proposals"""
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, mapping_id, user_name, user_email, user_affiliation,
                       provider_username, proposed_delete, proposed_confidence,
                       status, admin_notes, approved_by, approved_at,
                       rejected_by, rejected_at, uuid, suggestion_score,
                       ke_id, ke_title, reactome_id, pathway_name, species,
                       new_pair_confidence_level, created_at
                FROM ke_reactome_proposals
                WHERE status = 'pending'
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_proposal_by_id(self, proposal_id: int) -> Optional[Dict]:
        """Get a specific Reactome proposal by ID.

        Phase 37 ASMT-04: extended SELECT to include proposed_relationship,
        proposed_basis, proposed_specificity, proposed_coverage so the admin
        detail route (Plan 03) can surface the four step-answer columns. The
        original explicit column list omitted them (Pitfall 5 in 37-RESEARCH.md).
        """
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, mapping_id, user_name, user_email, user_affiliation,
                       provider_username, proposed_delete, proposed_confidence,
                       status, admin_notes, approved_by, approved_at,
                       rejected_by, rejected_at, uuid, suggestion_score,
                       ke_id, ke_title, reactome_id, pathway_name, species,
                       new_pair_confidence_level, created_at,
                       proposed_relationship, proposed_basis,
                       proposed_specificity, proposed_coverage
                FROM ke_reactome_proposals
                WHERE id = ?
                """,
                (proposal_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_all_proposals(self, status: str = None) -> List[Dict]:
        """Get all Reactome proposals, optionally filtered by status."""
        conn = self.db.get_connection()
        try:
            query = """
                SELECT p.*,
                       m.ke_id as mapping_ke_id,
                       m.ke_title as mapping_ke_title,
                       m.reactome_id as mapping_reactome_id,
                       m.pathway_name as mapping_pathway_name,
                       m.confidence_level as current_confidence_level,
                       m.species as current_species
                FROM ke_reactome_proposals p
                LEFT JOIN ke_reactome_mappings m ON p.mapping_id = m.id
            """
            params = ()
            if status:
                query += " WHERE p.status = ?"
                params = (status,)
            query += " ORDER BY p.created_at DESC"
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def update_proposal_status(
        self,
        proposal_id: int,
        status: str,
        admin_username: str = None,
        admin_notes: str = None,
    ) -> bool:
        """Update Reactome proposal status and admin information."""
        if status not in ["approved", "rejected"]:
            logger.error("Invalid Reactome proposal status: %s", status)
            return False

        conn = self.db.get_connection()
        try:
            if status == "approved":
                query = """
                    UPDATE ke_reactome_proposals
                    SET status = ?, approved_by = ?, admin_notes = ?, approved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
            else:
                query = """
                    UPDATE ke_reactome_proposals
                    SET status = ?, rejected_by = ?, admin_notes = ?, rejected_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
            conn.execute(query, (status, admin_username, admin_notes, proposal_id))
            conn.commit()
            logger.info(
                "Updated Reactome proposal %s to %s by %s",
                proposal_id, status, admin_username,
            )
            return True
        except Exception as e:
            logger.error("Error updating Reactome proposal %s: %s", proposal_id, e)
            conn.rollback()
            return False
        finally:
            conn.close()

    def _update_status_on_conn(
        self,
        conn,
        proposal_id: int,
        status: str,
        admin_username: str,
        admin_notes: str,
    ) -> None:
        """Update Reactome proposal status on a caller-managed connection.

        Raises ValueError for invalid status. Executes the UPDATE on `conn`
        without calling conn.commit() or conn.close(). Any DB error propagates
        to the caller for rollback.

        Phase 38 (ADMIN-02): caller-managed transaction helper for bulk-approve.
        """
        if status not in ["approved", "rejected"]:
            raise ValueError(f"Invalid status: {status}")
        if status == "approved":
            query = """
                UPDATE ke_reactome_proposals
                SET status = ?, approved_by = ?, admin_notes = ?, approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
        else:
            query = """
                UPDATE ke_reactome_proposals
                SET status = ?, rejected_by = ?, admin_notes = ?, rejected_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """
        conn.execute(query, (status, admin_username, admin_notes, proposal_id))
