"""Issue #264 — the assessment->connection_type dual-write must translate.

`step1` (relationship) and `connection_type` are different vocabularies:

    step1           causative, responsive, bidirectional, unclear
    connection_type causative, responsive, other,         undefined

The Phase 34 dual-write copied the former straight into the latter, so the
model layer wrote values `MappingSchema` rejects on input — 34 of 200
production rows when this was found. These tests pin the translation at every
writer, and pin the property that actually matters: whatever a curator answers,
the stored connection_type stays inside its own whitelist.
"""
import os
import tempfile

import pytest

from src.core.schemas import (
    KE_WP_CONNECTION_TYPES,
    KE_WP_RELATIONSHIP_OPTIONS,
    connection_type_for_relationship,
)


@pytest.fixture
def db():
    from src.core.models import Database

    fd, db_path = tempfile.mkstemp()
    yield Database(db_path)
    os.close(fd)
    os.unlink(db_path)


# --------------------------------------------------------------------------
# The map itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "relationship,expected",
    [
        ("causative", "causative"),
        ("responsive", "responsive"),
        ("bidirectional", "other"),
        ("unclear", "undefined"),
    ],
)
def test_map_matches_the_client(relationship, expected):
    """Mirrors mapConnectionTypeForServer in static/js/main.js."""
    assert connection_type_for_relationship(relationship) == expected


def test_map_covers_every_relationship_option():
    """No step1 answer may fall through to the unknown-value default."""
    for relationship in KE_WP_RELATIONSHIP_OPTIONS:
        mapped = connection_type_for_relationship(relationship)
        assert mapped in KE_WP_CONNECTION_TYPES


def test_none_passes_through_so_callers_can_fall_back():
    assert connection_type_for_relationship(None) is None


def test_unknown_relationship_does_not_widen_the_vocabulary():
    """An unrecognised answer must not reach the column verbatim."""
    assert connection_type_for_relationship("sideways") == "undefined"


# --------------------------------------------------------------------------
# The four writers
# --------------------------------------------------------------------------

def test_create_mapping_translates(db):
    from src.core.models import MappingModel

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 1", ke_title="T", wp_id="WP1", wp_title="P",
        connection_type="causative", confidence_level="high",
        created_by="github:tester", proposed_relationship="bidirectional",
    )
    row = mm.get_all_mappings()[0]
    assert row["connection_type"] == "other"
    # The original answer is not lost — it stays in the assessment column.
    assert row["proposed_relationship"] == "bidirectional"


def test_update_mapping_translates(db):
    from src.core.models import MappingModel

    mm = MappingModel(db)
    mapping_id = mm.create_mapping(
        ke_id="KE 2", ke_title="T", wp_id="WP2", wp_title="P",
        connection_type="causative", confidence_level="high", created_by="github:tester",
    )
    mm.update_mapping(mapping_id=mapping_id, proposed_relationship="unclear")
    row = mm.get_all_mappings()[0]
    assert row["connection_type"] == "undefined"
    assert row["proposed_relationship"] == "unclear"


def test_create_proposal_translates(db):
    from src.core.models import MappingModel, ProposalModel

    mm, pm = MappingModel(db), ProposalModel(db)
    mapping_id = mm.create_mapping(
        ke_id="KE 3", ke_title="T", wp_id="WP3", wp_title="P",
        connection_type="causative", confidence_level="high", created_by="github:tester",
    )
    proposal_id = pm.create_proposal(
        mapping_id=mapping_id, user_name="U", user_email="u@example.com",
        user_affiliation="A", proposed_relationship="bidirectional",
    )
    proposal = pm.get_proposal_by_id(proposal_id)
    assert proposal["proposed_connection_type"] == "other"


def test_create_new_pair_proposal_translates(db):
    from src.core.models import ProposalModel

    pm = ProposalModel(db)
    proposal_id = pm.create_new_pair_proposal(
        ke_id="KE 4", ke_title="T", wp_id="WP4", wp_title="P",
        connection_type="other", confidence_level="high",
        provider_username="github:tester",
        proposed_relationship="bidirectional",
    )
    proposal = pm.get_proposal_by_id(proposal_id)
    assert proposal["proposed_connection_type"] == "other"


@pytest.mark.parametrize("relationship", KE_WP_RELATIONSHIP_OPTIONS)
def test_stored_connection_type_is_always_in_vocabulary(db, relationship):
    """The property the corpus actually needs, for every possible answer.

    This is the check whose absence let #264 run for months: no test read the
    column back after submitting a step1 value.
    """
    from src.core.models import MappingModel

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id=f"KE {relationship}", ke_title="T", wp_id="WP9", wp_title="P",
        connection_type="undefined", confidence_level="low",
        created_by="github:tester", proposed_relationship=relationship,
    )
    row = mm.get_all_mappings()[0]
    assert row["connection_type"] in KE_WP_CONNECTION_TYPES


def test_explicit_connection_type_survives_when_no_relationship(db):
    """A legacy v1 submission has no relationship; its value must pass through."""
    from src.core.models import MappingModel

    mm = MappingModel(db)
    mm.create_mapping(
        ke_id="KE 5", ke_title="T", wp_id="WP5", wp_title="P",
        connection_type="other", confidence_level="low", created_by="github:tester",
    )
    row = mm.get_all_mappings()[0]
    assert row["connection_type"] == "other"
    assert row["assessment_version"] == "v1"


# --------------------------------------------------------------------------
# The backfill
# --------------------------------------------------------------------------

def test_migration_repairs_legacy_rows_and_is_idempotent(db):
    """Rows written before the fix are normalised on the next startup."""
    from src.core.models import MappingModel

    mm = MappingModel(db)
    for i, (stale, expected) in enumerate(
        (("bidirectional", "other"), ("unclear", "undefined"))
    ):
        mm.create_mapping(
            ke_id=f"KE 1{i}", ke_title="T", wp_id=f"WP1{i}", wp_title="P",
            connection_type="causative", confidence_level="high", created_by="github:t",
        )
        # Reproduce the pre-fix state by writing the raw answer directly.
        conn = db.get_connection()
        conn.execute(
            "UPDATE mappings SET connection_type = ? WHERE wp_id = ?",
            (stale, f"WP1{i}"),
        )
        conn.commit()
        conn.close()

    conn = db.get_connection()
    db._migrate_normalise_connection_type_vocabulary(conn)
    conn.commit()
    conn.close()

    stored = {r["wp_id"]: r["connection_type"] for r in mm.get_all_mappings()}
    assert stored["WP10"] == "other"
    assert stored["WP11"] == "undefined"

    # Second run finds nothing to do and changes nothing.
    conn = db.get_connection()
    db._migrate_normalise_connection_type_vocabulary(conn)
    conn.commit()
    conn.close()
    again = {r["wp_id"]: r["connection_type"] for r in mm.get_all_mappings()}
    assert again == stored


def test_migration_leaves_valid_values_alone(db):
    from src.core.models import MappingModel

    mm = MappingModel(db)
    for i, valid in enumerate(KE_WP_CONNECTION_TYPES):
        mm.create_mapping(
            ke_id=f"KE 2{i}", ke_title="T", wp_id=f"WP2{i}", wp_title="P",
            connection_type=valid, confidence_level="low", created_by="github:t",
        )

    conn = db.get_connection()
    db._migrate_normalise_connection_type_vocabulary(conn)
    conn.commit()
    conn.close()

    stored = {r["wp_id"]: r["connection_type"] for r in mm.get_all_mappings()}
    for i, valid in enumerate(KE_WP_CONNECTION_TYPES):
        assert stored[f"WP2{i}"] == valid
