"""#266: a workshop guest could not submit a proposal at all.

Two independent faults on the one path guests are invited to use:

1. `_start_session` gave a guest the literal email "workshop-guest", which
   `autofillUserData` wrote into a required `fields.Email` — so the form opened
   pre-filled with a value that could never validate, and the guest saw a
   generic failure on a field they had not touched.
2. The identity was `guest-<label>`, with no `provider:` prefix, so the trigger
   on `proposals.provider_username` aborted the insert. `create_proposal`
   swallowed the exception and returned None, which the route turned into a
   bare 500.

The end-to-end cases here run against a real file-backed database. The default
`client` fixture binds the blueprint models to `:memory:`, which is
per-connection in SQLite, so the proposals table the fixture migrates is not the
one the route writes to — that is what limited the original guest test in
test_app.py to asserting `!= 401`, which is how both faults shipped.
"""
import pathlib

import pytest

from src.core.models import Database
from src.core.schemas import SecurityValidation

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"


def _read_template(name):
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


class _StubGuestCodes:
    """Accepts one code, returning the label /guest-login builds an identity from."""

    def validate_code(self, code):
        return {"label": "workshop-2026"} if code == "good-code" else None


@pytest.fixture
def guest_client_filedb(client, tmp_path, monkeypatch):
    """Guest session against file-backed WP proposal + mapping models.

    The session is obtained by actually logging in through /guest-login rather
    than hand-written into the session dict. Writing it by hand would bake the
    fixed identity into the fixture and the test would pass against the bug it
    exists to catch.
    """
    from src.blueprints import api as api_module
    from src.blueprints import auth as auth_module

    real_db = Database(str(tmp_path / "guest_route_test.db"))
    monkeypatch.setattr(api_module.mapping_model, "db", real_db)
    monkeypatch.setattr(api_module.proposal_model, "db", real_db)
    monkeypatch.setattr(auth_module, "guest_code_model", _StubGuestCodes())

    response = client.post("/guest-login", data={"code": "good-code"})
    assert response.status_code == 302, "guest login must succeed for this fixture"
    return client


@pytest.fixture
def unprefixed_client(client, tmp_path, monkeypatch):
    """A session identity with no provider prefix — what a guest used to get."""
    from src.blueprints import api as api_module

    real_db = Database(str(tmp_path / "unprefixed_route_test.db"))
    monkeypatch.setattr(api_module.mapping_model, "db", real_db)
    monkeypatch.setattr(api_module.proposal_model, "db", real_db)

    with client.session_transaction() as sess:
        sess["user"] = {"username": "guest-workshop-2026", "email": ""}
    return client


NEW_PAIR = {
    "ke_id": "KE 9266",
    "ke_title": "Guest submission test",
    "wp_id": "WP9266",
    "wp_title": "Guest submission pathway",
    "confidence_level": "low",
    "connection_type": "undefined",
}


class TestGuestIdentityShape:
    def test_guest_session_carries_a_provider_prefix(self):
        """The trigger requires a prefix; "guest:" is one, "guest-" is not."""
        assert SecurityValidation.validate_username("guest:workshop-2026")
        assert ":" in "guest:workshop-2026"

    def test_legacy_guest_form_still_validates(self):
        """A session issued before the fix keeps working until it expires."""
        assert SecurityValidation.validate_username("guest-workshop-2026")

    def test_guest_login_issues_a_prefixed_identity_and_no_email(self, client, monkeypatch):
        """The two values the faults came from, asserted at the source."""
        from src.blueprints import auth as auth_module

        monkeypatch.setattr(auth_module, "guest_code_model", _StubGuestCodes())
        response = client.post("/guest-login", data={"code": "good-code"})
        assert response.status_code == 302
        with client.session_transaction() as sess:
            user = sess["user"]

        assert user["username"] == "guest:workshop-2026"
        assert user["is_guest"] is True
        # Not "workshop-guest": the modal autofills this into a required Email
        # field, and a placeholder there is a validation error on a field the
        # guest never touched.
        assert user["email"] == ""


class TestGuestCanSubmit:
    def test_guest_creates_a_new_pair_proposal(self, guest_client_filedb):
        response = guest_client_filedb.post("/submit", data=NEW_PAIR)
        assert response.status_code == 200, (
            f"Guest submit must succeed; got {response.status_code} "
            f"{response.get_data(as_text=True)}"
        )
        assert response.get_json().get("proposal_id")

    def test_the_proposal_is_attributed_to_the_guest(self, guest_client_filedb):
        """The identity survives the insert rather than being dropped to NULL."""
        from src.blueprints import api as api_module

        guest_client_filedb.post("/submit", data=NEW_PAIR)
        conn = api_module.proposal_model.db.get_connection()
        try:
            row = conn.execute(
                "SELECT provider_username FROM proposals WHERE ke_id = ?",
                (NEW_PAIR["ke_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["provider_username"] == "guest:workshop-2026"


class TestUnprefixedIdentityIsStated:
    def test_unprefixed_identity_is_a_401_not_a_500(self, unprefixed_client):
        """An identity the database will refuse is refused at the boundary.

        Previously this reached the INSERT, the trigger aborted it, and the
        route reported "Failed to create proposal" with a 500 — naming neither
        the cause nor anything the user could do.
        """
        response = unprefixed_client.post("/submit", data=NEW_PAIR)
        assert response.status_code == 401, response.get_data(as_text=True)
        assert "sign in again" in response.get_json()["error"]


class TestValidationErrorsNameTheirField:
    def test_bad_email_response_carries_field_level_details(self, guest_client_filedb):
        """The modal renders `details`; the server must supply them.

        The guest's first failure was reported as "Error: Invalid input data"
        because only `error` was shown, though `details` named `userEmail`.
        """
        response = guest_client_filedb.post(
            "/submit_proposal",
            data={
                "entry": '{"ke_id": "KE 9266", "wp_id": "WP9266"}',
                "userName": "Workshop Participant",
                "userEmail": "workshop-guest",
                "userAffiliation": "Maastricht University",
                "step1": "known",
                "step2": "direct",
                "step3": "most",
                "step4": "yes",
            },
        )
        assert response.status_code == 400
        body = response.get_json()
        assert body["error"] == "Invalid input data"
        assert "userEmail" in body["details"]

    def test_modal_renders_the_details_it_is_sent(self):
        """Static grep: the submit handler must not drop `details` on the floor.

        The server named the offending field all along; the modal showed only
        `error`, so every validation failure read as "Invalid input data".
        """
        template = _read_template("explore.html")
        assert "formatErrorDetails" in template, (
            "explore.html must format the server's field-level `details`"
        )
        assert "json.details" in template

    def test_modal_does_not_autofill_a_non_address_into_the_email_field(self):
        """Static grep: a value with no '@' must not be prefilled.

        A guest session carries no address and an OAuth provider may withhold
        one; prefilling a placeholder put a validation error on a field the
        user never touched.
        """
        template = _read_template("explore.html")
        assert "userInfo.email.indexOf('@') > -1" in template
