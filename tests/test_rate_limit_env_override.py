"""
Tests for the env-configurable rate-limit ceilings.

The submission ceiling was hardcoded at 20/hour with no override, which made a
legitimate curation sprint impractical — a few hundred proposals took most of a day,
and the limiter counts rejected requests too, so backing off did not help. The
ceilings are now read from the environment, with the previous values as defaults and
a bad value falling back rather than silently disabling the limiter.
"""
import importlib

import pytest

import src.services.rate_limiter as rl


def _env_limit(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SUBMISSION_RATE_LIMIT_PER_HOUR", raising=False)
    else:
        monkeypatch.setenv("SUBMISSION_RATE_LIMIT_PER_HOUR", value)
    return rl._env_limit("SUBMISSION_RATE_LIMIT_PER_HOUR", 20)


def test_defaults_are_unchanged():
    """Nothing set in the environment must behave exactly as before."""
    assert rl._env_limit("A_VAR_THAT_IS_NOT_SET", 20) == 20
    assert rl._env_limit("A_VAR_THAT_IS_NOT_SET", 500) == 500
    assert rl._env_limit("A_VAR_THAT_IS_NOT_SET", 1000) == 1000


def test_override_is_applied(monkeypatch):
    assert _env_limit(monkeypatch, "400") == 400


@pytest.mark.parametrize("bad", ["", "abc", "20.5", "0", "-5"])
def test_invalid_values_fall_back_to_the_default(monkeypatch, bad):
    """A typo must never disable the limiter or set a nonsensical ceiling."""
    assert _env_limit(monkeypatch, bad) == 20


def test_module_constants_wire_the_defaults():
    """Import-time constants keep the historical ceilings when unset."""
    monkey = pytest.MonkeyPatch()
    for var in (
        "SUBMISSION_RATE_LIMIT_PER_HOUR",
        "SPARQL_RATE_LIMIT_PER_HOUR",
        "GENERAL_RATE_LIMIT_PER_HOUR",
    ):
        monkey.delenv(var, raising=False)
    try:
        reloaded = importlib.reload(rl)
        assert reloaded.SUBMISSION_RATE_LIMIT == 20
        assert reloaded.SPARQL_RATE_LIMIT == 500
        assert reloaded.GENERAL_RATE_LIMIT == 1000
    finally:
        monkey.undo()
        importlib.reload(rl)
