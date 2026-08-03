"""#270: /submit_go_mapping accepted any integer as a GO dimension score.

The three dimensions were read straight off the form with a bare int cast and
were not declared on `GoMappingSchema`, so Marshmallow never saw them. They
reached `create_new_pair_go_proposal` as whatever was posted, and
`_compute_confidence_from_dimensions` takes a weighted average and buckets it —
so an out-of-range value does not error, it silently mints a tier. A single
`connection_score=99` produced a *high*-confidence mapping, and the value is
published on `ke_go_mappings.connection_score` and exported.

#245 added `GO_DIMENSION_SCORES` and validated the *revision* path against it;
this closes the sibling new-pair path so the two cannot drift again.

Range: the live data holds only 1-3 (plus NULL on the pre-assessment legacy
rows), so 1-3 is enforced and NULL stays representable by omitting the field.
"""
import pytest

from src.core.models import Database
from src.core.schemas import GO_DIMENSION_SCORES, GoMappingSchema

BASE_FORM = {
    "ke_id": "KE 9270",
    "ke_title": "Dimension score validation",
    "go_id": "GO:9270001",
    "go_name": "test biological process",
    "connection_type": "describes",
    "confidence_level": "medium",
    "go_namespace": "biological_process",
}


@pytest.fixture
def auth_client_filedb(client, tmp_path, monkeypatch):
    """GO models pinned to a file-backed DB — see test_go_proposal_models.py."""
    from src.blueprints import api as api_module

    real_db = Database(str(tmp_path / "go_dimension_route_test.db"))
    monkeypatch.setattr(api_module.go_mapping_model, "db", real_db)
    monkeypatch.setattr(api_module.go_proposal_model, "db", real_db)

    with client.session_transaction() as sess:
        sess["user"] = {"username": "github:testuser", "email": "test@example.com"}
    return client


def _stored_scores(ke_id):
    from src.blueprints import api as api_module

    conn = api_module.go_proposal_model.db.get_connection()
    try:
        return conn.execute(
            "SELECT proposed_connection_score, proposed_specificity_score, "
            "proposed_evidence_score FROM ke_go_proposals WHERE ke_id = ?",
            (ke_id,),
        ).fetchone()
    finally:
        conn.close()


class TestSchemaDeclaresTheDimensions:
    def test_range_is_one_to_three(self):
        assert GO_DIMENSION_SCORES == (1, 2, 3)

    @pytest.mark.parametrize("field", [
        "connection_score", "specificity_score", "evidence_score",
    ])
    def test_field_is_declared_and_bounded(self, field):
        """Undeclared is the whole defect: Marshmallow cannot reject what it
        does not know about."""
        assert field in GoMappingSchema().fields
        errors = GoMappingSchema().validate({**BASE_FORM, field: 99})
        assert field in errors


class TestOutOfRangeIsRejected:
    @pytest.mark.parametrize("value", [99, 0, -1, 4])
    def test_out_of_range_score_is_a_400(self, auth_client_filedb, value):
        response = auth_client_filedb.post(
            "/submit_go_mapping", data={**BASE_FORM, "connection_score": value}
        )
        assert response.status_code == 400, (
            f"connection_score={value} must be refused; got "
            f"{response.status_code} {response.get_data(as_text=True)}"
        )
        assert "connection_score" in response.get_json()["details"]

    def test_nothing_is_stored_for_a_refused_submission(self, auth_client_filedb):
        """The old path did not just accept 99 — it persisted it."""
        auth_client_filedb.post(
            "/submit_go_mapping", data={**BASE_FORM, "connection_score": 99}
        )
        assert _stored_scores(BASE_FORM["ke_id"]) is None

    def test_a_non_integer_is_a_400_rather_than_a_silent_null(self, auth_client_filedb):
        """The bare int cast turned "high" into None and carried on."""
        response = auth_client_filedb.post(
            "/submit_go_mapping", data={**BASE_FORM, "evidence_score": "high"}
        )
        assert response.status_code == 400
        assert "evidence_score" in response.get_json()["details"]


class TestValidSubmissionsStillWork:
    def test_in_range_scores_are_accepted_and_stored(self, auth_client_filedb):
        response = auth_client_filedb.post(
            "/submit_go_mapping",
            data={
                **BASE_FORM,
                "connection_score": 3,
                "specificity_score": 2,
                "evidence_score": 1,
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        assert tuple(_stored_scores(BASE_FORM["ke_id"])) == (3, 2, 1)

    def test_omitted_scores_stay_null(self, auth_client_filedb):
        """A submission carrying no assessment is not the same as one scoring 0
        — the legacy rows are NULL and must remain representable."""
        response = auth_client_filedb.post("/submit_go_mapping", data=BASE_FORM)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert tuple(_stored_scores(BASE_FORM["ke_id"])) == (None, None, None)

    def test_empty_strings_are_treated_as_omitted(self, auth_client_filedb):
        """An unanswered radio group posts "", which must not fail OneOf."""
        response = auth_client_filedb.post(
            "/submit_go_mapping",
            data={
                **BASE_FORM,
                "connection_score": "",
                "specificity_score": "",
                "evidence_score": "",
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        assert tuple(_stored_scores(BASE_FORM["ke_id"])) == (None, None, None)
