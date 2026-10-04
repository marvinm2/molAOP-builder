"""Regression tests for #214: preview modals must show the real submitter.

The WP and GO preview modals scraped the username out of a header "Welcome,"
paragraph that no longer exists, so a logged-in curator was shown "Anonymous"
(GO) or "GitHub user (logged in)" (WP). The mapper page now passes the
provider-prefixed identity on <body data-username>, and main.js reads it
through a single helper.
"""
import os

import pytest

MAIN_JS = os.path.join(os.path.dirname(__file__), "..", "static", "js", "main.js")


def _read_main_js():
    with open(MAIN_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def _login(client, username):
    with client.session_transaction() as sess:
        sess["user"] = {"username": username, "email": ""}


@pytest.mark.parametrize(
    "username",
    ["orcid:0000-0003-2230-0840", "guest:workshop-nl", "github:marvinm2"],
)
def test_mapper_body_carries_provider_prefixed_username(client, username):
    """A logged-in session renders its full identity, not a guessed provider."""
    _login(client, username)
    response = client.get("/mapper")
    assert response.status_code == 200
    assert f'data-username="{username}"'.encode() in response.data


def test_mapper_body_has_no_username_when_anonymous(client):
    response = client.get("/mapper")
    assert response.status_code == 200
    assert b"data-username=" not in response.data
    assert b'data-is-logged-in="false"' in response.data


def test_mapper_body_escapes_username_attribute(client):
    """Guest labels come from admin-created codes, so they must not break out."""
    _login(client, 'guest:a"b<c&d')
    response = client.get("/mapper")
    assert response.status_code == 200
    assert b'data-username="guest:a&#34;b&lt;c&amp;d"' in response.data
    assert b'guest:a"b' not in response.data


def test_preview_modals_use_shared_username_helper():
    """All three preview sites read the body attribute, none scrape the header."""
    body = _read_main_js()
    assert "Welcome," not in body, "a preview modal still scrapes the header"
    assert "GitHub user (logged in)" not in body
    assert body.count("this.getSubmitterLabel()") == 3


def test_helper_reads_the_attribute_the_template_renders():
    """A rename on either side (template or main.js) must fail the suite."""
    body = _read_main_js()
    assert '$("body").attr("data-username")' in body
    index = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
    with open(index, "r", encoding="utf-8") as fh:
        assert 'data-username="' in fh.read()
