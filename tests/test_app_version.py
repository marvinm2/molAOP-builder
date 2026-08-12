"""`/health` must report the version the project actually released.

The route carried its own `"version": "2.7.0"` literal. The project went on to
release 2.7.1, 2.7.2 and 2.8.0, and nothing connected the literal to any of
them, so `/health` under-reported by three releases for months. It is exactly
the class of drift that #204 and #211 were: two places holding the same fact,
agreeing only by coincidence.

`src.__version__` is now the single source, and the first test below pins it to
the newest released heading in CHANGELOG.md — the place a release is actually
declared. A release that forgets to bump `__version__` fails here.

`build` is separate and deliberately so: a semantic version cannot distinguish
two deployments of the same release, which is what "is the latest code live?"
asks. CI passes the commit SHA as a build arg.
"""
import os
import re

import pytest

from src import __version__, get_build_ref

HERE = os.path.dirname(__file__)
CHANGELOG = os.path.join(HERE, "..", "CHANGELOG.md")

# "## [2.8.0] - 2026-05-14" — deliberately excludes "## [Unreleased]".
RELEASE_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.M)


def _newest_released_version():
    with open(CHANGELOG, "r", encoding="utf-8") as fh:
        versions = RELEASE_HEADING.findall(fh.read())
    assert versions, "no released version headings found in CHANGELOG.md"
    return versions[0]


def test_version_matches_newest_changelog_release():
    assert __version__ == _newest_released_version(), (
        f"src.__version__ is {__version__} but the newest release in CHANGELOG.md "
        f"is {_newest_released_version()}. Bump __version__ when cutting a release "
        "— /health reports it, and it silently drifted three releases behind before."
    )


def test_health_reports_the_canonical_version_not_a_literal():
    """Guard against a literal being reintroduced into the route."""
    import inspect

    import app as app_module

    source = inspect.getsource(app_module)
    assert '"version": __version__' in source, (
        "/health must report src.__version__, not a hardcoded string"
    )


def test_build_ref_is_unknown_when_not_built_by_ci(monkeypatch):
    """No SHA in the environment must report "unknown", never a stand-in.

    A fabricated or blank build ref is worse than an honest "unknown" — it makes
    an unidentifiable deployment look identified.
    """
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert get_build_ref() == "unknown"

    monkeypatch.setenv("GIT_SHA", "")
    assert get_build_ref() == "unknown"


def test_build_ref_reports_the_injected_sha(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "f4d8c977f2b757791276f423071d73c3b0eec3da")
    assert get_build_ref() == "f4d8c977f2b757791276f423071d73c3b0eec3da"


@pytest.fixture
def client():
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key")
    from app import app as flask_app

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_health_payload_carries_version_and_build(client):
    body = client.get("/health").get_json()

    assert body["version"] == __version__
    assert "build" in body, (
        "/health must identify the running build, not only the release version"
    )


def test_dockerfile_and_ci_pass_the_commit_through():
    """The build arg is useless unless CI actually supplies it."""
    with open(os.path.join(HERE, "..", "Dockerfile"), encoding="utf-8") as fh:
        dockerfile = fh.read()
    assert "ARG GIT_SHA" in dockerfile and "ENV GIT_SHA=$GIT_SHA" in dockerfile

    with open(os.path.join(HERE, "..", ".github", "workflows", "docker.yml"), encoding="utf-8") as fh:
        workflow = fh.read()
    assert "GIT_SHA=${{ github.sha }}" in workflow, (
        "the image push step must pass the commit SHA as a build arg, or /health "
        "reports 'unknown' on every deployed image"
    )


def test_citation_metadata_agrees_with_the_code_version():
    """CITATION.cff and .zenodo.json are frozen by the tag; they cannot drift.

    Zenodo reads `.zenodo.json` from the tagged commit and ignores CITATION.cff
    entirely whenever it is present, so the two files are maintained by hand and
    have no mechanism keeping them honest except this test. A DOI cannot be
    re-minted to correct a version that disagreed with the code.

    The JSON is also parsed rather than merely read: an invalid `.zenodo.json`
    makes Zenodo skip the archive **silently** — no error at release time, just no
    record — so a syntax error here is a release that quietly does not happen.
    """
    import json

    root = os.path.join(HERE, "..")

    with open(os.path.join(root, "CITATION.cff"), encoding="utf-8") as fh:
        cff = fh.read()
    match = re.search(r"^version:\s*['\"]?([0-9][^'\"\s]*)", cff, re.M)
    assert match, "CITATION.cff has no version field"
    assert match.group(1) == __version__, (
        f"CITATION.cff says {match.group(1)}, src.__version__ says {__version__}"
    )

    with open(os.path.join(root, ".zenodo.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["version"] == __version__, (
        f".zenodo.json says {payload['version']}, src.__version__ says {__version__}"
    )
    # Values Zenodo validates against a controlled vocabulary. A wrong one is not
    # rejected loudly — it surfaces only in Zenodo's Errors tab, after the tag.
    assert payload["license"] == "gpl-2.0-only"
    assert payload["upload_type"] == "software"
    assert payload["creators"], "a record with no creators lists the GitHub account"

    # The VHP4Safety grant, so this record joins the same funding thread as the
    # project's other Zenodo deposits.
    #
    # The identifier is not the NWA-ORC award number. Searching Zenodo for
    # "1292.19.272" returns zero hits, which reads as "NWO grants are not
    # supported" and is wrong: OpenAIRE indexes this project under NWO grant code
    # **36952** ("The Virtual Human Platform for Safety Assessment"), which is
    # what the AOP-Wiki RDF deposits already use. An unresolvable grant id is a
    # documented cause of archiving failing silently, so the human-readable
    # NWA-ORC number stays in the description and the resolvable code goes here.
    grant_ids = [g.get("id") for g in payload.get("grants", [])]
    assert "10.13039/501100003246::36952" in grant_ids, (
        "the VHP4Safety NWO grant must be declared; use funder DOI "
        "10.13039/501100003246 (NWO) with code 36952, not the NWA-ORC number"
    )
    assert {c.get("identifier") for c in payload.get("communities", [])} >= {"vhp4safety"}
