"""Tests for ReactomeMappingSchema, ReactomeCheckEntrySchema, and
ReactomeSuggestionService.search_reactome_terms (Phase 25-01)."""
import pytest
from marshmallow import ValidationError


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TestReactomeMappingSchema:
    @pytest.fixture
    def schema(self):
        from src.core.schemas import ReactomeMappingSchema
        return ReactomeMappingSchema()

    def test_valid_payload(self, schema):
        data = {
            "ke_id": "KE 12345",
            "ke_title": "Oxidative stress",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "MAPK signaling",
            "species": "Homo sapiens",
            "confidence_level": "high",
        }
        loaded = schema.load(data)
        assert loaded["ke_id"] == "KE 12345"
        assert loaded["reactome_id"] == "R-HSA-1234"
        assert loaded["confidence_level"] == "high"

    def test_rejects_reactome_id_missing_r_prefix(self, schema):
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "HSA-1234",
            "pathway_name": "x",
            "species": "Homo sapiens",
            "confidence_level": "high",
        }
        with pytest.raises(ValidationError) as exc:
            schema.load(data)
        assert "reactome_id" in exc.value.messages

    def test_rejects_non_human_species_id(self, schema):
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-MMU-1234",
            "pathway_name": "x",
            "species": "Mus musculus",
            "confidence_level": "high",
        }
        with pytest.raises(ValidationError) as exc:
            schema.load(data)
        assert "reactome_id" in exc.value.messages

    def test_rejects_invalid_confidence(self, schema):
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "species": "Homo sapiens",
            "confidence_level": "extreme",
        }
        with pytest.raises(ValidationError) as exc:
            schema.load(data)
        assert "confidence_level" in exc.value.messages

    def test_species_default(self, schema):
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "confidence_level": "high",
        }
        loaded = schema.load(data)
        assert loaded["species"] == "Homo sapiens"

    # Phase 37 ASMT-04: step1-4 + connection_type optional fields
    def test_accepts_step1_to_step4(self, schema):
        """ReactomeMappingSchema should accept step1-4 with valid option keys."""
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "confidence_level": "high",
            "step1": "causative",
            "step2": "known",
            "step3": "specific",
            "step4": "complete",
        }
        loaded = schema.load(data)
        assert loaded["step1"] == "causative"
        assert loaded["step2"] == "known"
        assert loaded["step3"] == "specific"
        assert loaded["step4"] == "complete"

    def test_rejects_connection_type(self, schema):
        """Reactome has no connection type (#248).

        The field used to be declared here and validated, then discarded —
        `ke_reactome_mappings` has no such column and the proposal model no such
        parameter. Accepting it only created the impression that the value went
        somewhere. Rejecting it is the honest behaviour: a client sending one is
        told, rather than watching it vanish.

        The relationship answer is `step1`, stored as `proposed_relationship`.
        """
        from marshmallow import ValidationError

        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "confidence_level": "high",
            "connection_type": "causative",
        }
        with pytest.raises(ValidationError) as excinfo:
            schema.load(data)
        assert "connection_type" in excinfo.value.messages

    def test_rejects_invalid_step1(self, schema):
        """Step1 out-of-whitelist value should raise ValidationError."""
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "confidence_level": "high",
            "step1": "banana",
        }
        with pytest.raises(ValidationError) as exc:
            schema.load(data)
        assert "step1" in exc.value.messages

    def test_rejects_invalid_step2(self, schema):
        """Step2 out-of-whitelist value should raise ValidationError."""
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "confidence_level": "high",
            "step2": "invalid_basis",
        }
        with pytest.raises(ValidationError) as exc:
            schema.load(data)
        assert "step2" in exc.value.messages

    def test_step1_to_step4_are_optional(self, schema):
        """Payload without step1-4 should still validate (back-compat v1)."""
        data = {
            "ke_id": "KE 12345",
            "ke_title": "x",
            "reactome_id": "R-HSA-1234",
            "pathway_name": "x",
            "confidence_level": "high",
        }
        loaded = schema.load(data)
        # step fields should not appear if not sent
        assert "step1" not in loaded or loaded.get("step1") is None


class TestReactomeCheckEntrySchema:
    def test_valid_payload(self):
        from src.core.schemas import ReactomeCheckEntrySchema
        schema = ReactomeCheckEntrySchema()
        loaded = schema.load({"ke_id": "KE 12345", "reactome_id": "R-HSA-1234"})
        assert loaded == {"ke_id": "KE 12345", "reactome_id": "R-HSA-1234"}

    def test_rejects_bad_ke_id(self):
        from src.core.schemas import ReactomeCheckEntrySchema
        schema = ReactomeCheckEntrySchema()
        with pytest.raises(ValidationError) as exc:
            schema.load({"ke_id": "Event:12345", "reactome_id": "R-HSA-1234"})
        assert "ke_id" in exc.value.messages

    def test_rejects_bad_reactome_id(self):
        from src.core.schemas import ReactomeCheckEntrySchema
        schema = ReactomeCheckEntrySchema()
        with pytest.raises(ValidationError) as exc:
            schema.load({"ke_id": "KE 12345", "reactome_id": "REACT_1234"})
        assert "reactome_id" in exc.value.messages


# ---------------------------------------------------------------------------
# search_reactome_terms
# ---------------------------------------------------------------------------


@pytest.fixture
def reactome_service_with_metadata(monkeypatch, tmp_path):
    """Build a ReactomeSuggestionService with a tiny in-memory metadata fixture.

    Avoids loading the on-disk Reactome NPZ/JSON by pointing the service at
    non-existent paths (the loaders log a warning and leave dicts empty).
    """
    from src.suggestions.reactome import ReactomeSuggestionService

    missing = str(tmp_path / "does-not-exist.json")
    service = ReactomeSuggestionService(
        cache_model=None,
        config=None,
        embedding_service=None,
        ke_override_model=None,
        reactome_embeddings_path=str(tmp_path / "missing.npz"),
        reactome_name_embeddings_path=str(tmp_path / "missing-name.npz"),
        reactome_metadata_path=missing,
        reactome_annotations_path=missing,
    )
    # Inject a small in-memory metadata dict with name + description.
    service.reactome_metadata = {
        "R-HSA-1234": {
            "name": "MAPK signaling",
            "description": "Mitogen-activated protein kinase cascade",
        },
        "R-HSA-9999": {
            "name": "Apoptosis",
            "description": "Programmed cell death pathway",
        },
        "R-HSA-5555": {
            "name": "Oxidative stress response",
            "description": "Cellular response to reactive oxygen species",
        },
    }
    return service


class TestSearchReactomeTerms:
    def test_keyword_search_returns_ranked_dicts(
        self, reactome_service_with_metadata
    ):
        results = reactome_service_with_metadata.search_reactome_terms(
            "MAPK", threshold=0.4, limit=5
        )
        assert isinstance(results, list)
        assert len(results) >= 1
        assert len(results) <= 5
        # Required keys present on every result
        for r in results:
            assert "reactome_id" in r
            assert "pathway_name" in r
            assert "relevance_score" in r
        # Sorted descending
        scores = [r["relevance_score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        # Top hit is the MAPK pathway
        assert results[0]["reactome_id"] == "R-HSA-1234"

    def test_empty_query_returns_empty_list(
        self, reactome_service_with_metadata
    ):
        assert reactome_service_with_metadata.search_reactome_terms("") == []
        assert reactome_service_with_metadata.search_reactome_terms("   ") == []

    def test_id_lookup_returns_at_most_one_result(
        self, reactome_service_with_metadata
    ):
        results = reactome_service_with_metadata.search_reactome_terms(
            "R-HSA-1234"
        )
        assert len(results) == 1
        assert results[0]["reactome_id"] == "R-HSA-1234"
        assert results[0]["relevance_score"] == 1.0

    def test_id_lookup_for_unknown_id_returns_empty(
        self, reactome_service_with_metadata
    ):
        results = reactome_service_with_metadata.search_reactome_terms(
            "R-HSA-99999999"
        )
        assert results == []
