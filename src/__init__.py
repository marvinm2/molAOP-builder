import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The one place the application version is written down. `/health` used to carry
# its own literal, which sat at 2.7.0 while the project shipped 2.7.1, 2.7.2 and
# 2.8.0 — nothing connected the two, so nothing caught the drift.
# tests/test_app_version.py pins this to the newest released heading in
# CHANGELOG.md, which is where a release is actually declared.
__version__ = "2.8.0"


def get_build_ref() -> str:
    """Identify the running build, not just its semantic version.

    A released version cannot distinguish two deployments of the same release,
    which is what an operator asking "is the latest code live?" needs to know.
    The CI Docker build passes the commit SHA in as ``GIT_SHA``; outside a built
    image there is none, hence "unknown" rather than a fabricated value.
    """
    return os.environ.get("GIT_SHA") or "unknown"
