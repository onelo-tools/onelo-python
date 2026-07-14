"""Tests for stack trace capture and OTel-shaped serialisation."""
from __future__ import annotations

import pytest

from onelo.monitor._stacktrace import (
    capture_current_stack,
    capture_exception_info,
    captured_exception_to_dict,
    stackframe_to_dict,
)


def _raise_inner() -> None:
    raise ValueError("boom")


def _raise_outer() -> None:
    try:
        _raise_inner()
    except ValueError as e:
        raise RuntimeError("wrapped") from e


# ── capture_exception_info ─────────────────────────────────────────────────

def test_capture_exception_uses_current_exc_info() -> None:
    try:
        _raise_inner()
    except ValueError:
        captured = capture_exception_info()

    assert captured is not None
    assert captured.type == "ValueError"
    assert captured.message == "boom"
    assert len(captured.frames) >= 1
    # Last frame should be the one that raised (Python convention).
    assert captured.frames[-1].function and "_raise_inner" in captured.frames[-1].function


def test_capture_exception_returns_none_outside_except() -> None:
    captured = capture_exception_info()
    assert captured is None


def test_capture_exception_walks_chain_from_cause() -> None:
    try:
        _raise_outer()
    except RuntimeError as e:
        captured = capture_exception_info(e)

    assert captured is not None
    assert captured.type == "RuntimeError"
    assert captured.cause is not None
    assert captured.cause.type == "ValueError"
    assert captured.cause.message == "boom"


def test_capture_exception_respects_suppress_context() -> None:
    """`raise X from None` should suppress __context__."""
    try:
        try:
            _raise_inner()
        except ValueError:
            raise RuntimeError("plain") from None
    except RuntimeError as e:
        captured = capture_exception_info(e)

    assert captured is not None
    assert captured.cause is None


def test_in_app_classifier_marks_test_code() -> None:
    """Frames in this test file are part of 'developer code', not stdlib."""
    try:
        _raise_inner()
    except ValueError:
        captured = capture_exception_info()

    assert captured is not None
    test_frames = [f for f in captured.frames if f.filename and __file__ in f.filename]
    assert test_frames
    assert all(f.in_app for f in test_frames)


# ── capture_current_stack ──────────────────────────────────────────────────

def test_capture_current_stack_returns_frames() -> None:
    frames = capture_current_stack()
    assert frames
    # The most recent frame is this test function.
    assert any(f.function and "test_capture_current_stack_returns_frames" in f.function for f in frames)


def test_capture_current_stack_respects_max_depth() -> None:
    frames = capture_current_stack(max_depth=2)
    assert len(frames) <= 2


# ── serialisation ──────────────────────────────────────────────────────────

def test_stackframe_to_dict_omits_empty_fields() -> None:
    from onelo.monitor._types import StackFrame
    frame = StackFrame(
        function="my_fn",
        module="my_mod",
        filename="/p/my_mod.py",
        lineno=42,
        in_app=True,
    )
    out = stackframe_to_dict(frame)
    assert out == {
        "function": "my_fn",
        "module": "my_mod",
        "filename": "/p/my_mod.py",
        "lineno": 42,
        "in_app": True,
    }


def test_stackframe_to_dict_skips_none_fields() -> None:
    from onelo.monitor._types import StackFrame
    frame = StackFrame(
        function=None, module=None, filename=None, lineno=None, in_app=False,
    )
    out = stackframe_to_dict(frame)
    assert out == {"in_app": False}


def test_captured_exception_to_dict_includes_cause() -> None:
    try:
        _raise_outer()
    except RuntimeError as e:
        captured = capture_exception_info(e)

    assert captured is not None
    out = captured_exception_to_dict(captured)
    assert out["type"] == "RuntimeError"
    assert "cause" in out
    assert out["cause"]["type"] == "ValueError"  # type: ignore[index]
    assert isinstance(out["frames"], list)
