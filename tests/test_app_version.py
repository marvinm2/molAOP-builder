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
