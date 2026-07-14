"""Tests for strategy detection — resolves "auto" and validates input."""
import pytest

from onelo._detection import resolve_strategy


def test_auto_resolves_to_sse():
    assert resolve_strategy("auto") == "sse"


def test_explicit_sse_pass_through():
    assert resolve_strategy("sse") == "sse"


def test_explicit_polling_pass_through():
    assert resolve_strategy("polling") == "polling"


def test_invalid_raises():
    with pytest.raises(ValueError, match="strategy must be one of"):
        resolve_strategy("nonsense")


def test_redis_not_supported_in_v1():
    """Redis was scoped out of v1. Reject explicitly so users don't think it
    silently fell back to something else."""
    with pytest.raises(ValueError, match="not supported"):
        resolve_strategy("redis")
