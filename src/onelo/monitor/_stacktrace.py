"""Capture and format Python tracebacks into transport-safe StackFrame lists.

Output uses OTel-compatible field names so backends can group / display
events from any Onelo SDK uniformly. Most-recent frame is **last** in the
list (Python convention; the opposite of JS).

We do not symbolicate or fetch source lines from disk — that's a backend
responsibility (ditto for Swift). The SDK only forwards what's already in
memory: function name, module, filename, lineno, and the in-app classifier.
"""
from __future__ import annotations

import linecache
import os
import sys
import sysconfig
import traceback
from types import FrameType, TracebackType

from onelo.monitor._types import CapturedException, StackFrame


# Resolve once on import. ``stdlib`` covers the standard library install
# path and ``purelib`` / ``platlib`` cover site-packages. Anything outside
# all of these we treat as "in-app" code.
_STDLIB_PATHS: tuple[str, ...] = tuple(
    p for p in (
        sysconfig.get_paths().get("stdlib"),
        sysconfig.get_paths().get("purelib"),
        sysconfig.get_paths().get("platlib"),
    )
    if p
)


def _is_in_app(filename: str | None) -> bool:
    """Heuristic: code outside stdlib + site-packages is the developer's own."""
    if not filename:
        return True
    real = os.path.realpath(filename)
    return not any(real.startswith(p) for p in _STDLIB_PATHS)


def _frame_to_stackframe(
    filename: str,
    lineno: int,
    name: str,
    *,
    include_context: bool = True,
) -> StackFrame:
    context_line: str | None = None
    if include_context:
        try:
            raw = linecache.getline(filename, lineno).strip() or None
        except OSError:
            raw = None
        # Scrub the source line: dev sandboxes commonly contain literal
        # secrets in code (e.g. ``api_key = "sk_live_…"``, hardcoded
        # tokens, JWT examples). Without this, those literals would ride
        # the stack trace into the dashboard verbatim.
        if raw is not None:
            from onelo.monitor._scrub import scrub_text  # avoid cycle at import time
            context_line = scrub_text(raw) or raw
        else:
            context_line = None
    return StackFrame(
        function=name or None,
        module=_module_for(filename),
        filename=filename or None,
        lineno=lineno or None,
        in_app=_is_in_app(filename),
        context_line=context_line,
    )


def _module_for(filename: str) -> str | None:
    """Best-effort module name from a filename. Falls back to basename."""
    if not filename:
        return None
    base = os.path.basename(filename)
    name, _ = os.path.splitext(base)
    return name or None


def capture_exception_info(
    exc: BaseException | None = None,
    *,
    include_context_lines: bool = True,
) -> CapturedException | None:
    """Convert an exception into a transport-safe ``CapturedException``.

    Pass ``None`` (default) to use ``sys.exc_info()`` — i.e. inside an
    ``except`` block with no explicit handle.
    """
    if exc is None:
        _, exc, _ = sys.exc_info()
    if exc is None:
        return None
    return _walk_exception(exc, include_context_lines)


def _walk_exception(exc: BaseException, include_context: bool) -> CapturedException:
    frames: list[StackFrame] = []
    tb: TracebackType | None = exc.__traceback__
    while tb is not None:
        frames.append(
            _frame_to_stackframe(
                filename=tb.tb_frame.f_code.co_filename,
                lineno=tb.tb_lineno,
                name=tb.tb_frame.f_code.co_qualname,
                include_context=include_context,
            )
        )
        tb = tb.tb_next

    cause: CapturedException | None = None
    # Python's `raise X from Y` sets __cause__; an implicit chain inside
    # `except` sets __context__. Prefer __cause__; fall back to __context__
    # only if it wasn't suppressed (PEP 415).
    chained: BaseException | None = exc.__cause__
    if chained is None and not getattr(exc, "__suppress_context__", False):
        chained = exc.__context__
    if chained is not None:
        cause = _walk_exception(chained, include_context)

    return CapturedException(
        type=type(exc).__name__,
        message=str(exc),
        frames=frames,
        cause=cause,
    )


def capture_current_stack(
    *,
    skip: int = 1,
    max_depth: int = 64,
    include_context_lines: bool = False,
) -> list[StackFrame]:
    """Capture the current call stack (no exception required).

    Useful for ``capture_message`` when ``attach_stacktrace`` is enabled.
    Most-recent frame last (matches OTel and traceback module).
    """
    frames: list[StackFrame] = []
    extracted = traceback.extract_stack(limit=max_depth + skip)
    # extract_stack returns oldest first. Drop the trailing `skip` frames
    # (which are inside the SDK itself) so the dev's own frame is on top.
    if skip > 0:
        extracted = extracted[:-skip]
    for fs in extracted:
        frames.append(
            _frame_to_stackframe(
                filename=fs.filename,
                lineno=fs.lineno or 0,
                name=fs.name,
                include_context=include_context_lines,
            )
        )
    return frames


def stackframe_to_dict(frame: StackFrame) -> dict[str, object]:
    """Serialise a ``StackFrame`` to the wire shape used in event meta."""
    out: dict[str, object] = {}
    if frame.function:
        out["function"] = frame.function
    if frame.module:
        out["module"] = frame.module
    if frame.filename:
        out["filename"] = frame.filename
    if frame.lineno:
        out["lineno"] = frame.lineno
    out["in_app"] = frame.in_app
    if frame.context_line:
        out["context_line"] = frame.context_line
    return out


def captured_exception_to_dict(exc: CapturedException) -> dict[str, object]:
    """Wire shape for ``meta.exception`` (OTel-compatible)."""
    out: dict[str, object] = {
        "type": exc.type,
        "message": exc.message,
        "frames": [stackframe_to_dict(f) for f in exc.frames],
    }
    if exc.cause is not None:
        out["cause"] = captured_exception_to_dict(exc.cause)
    return out


__all__ = [
    "capture_current_stack",
    "capture_exception_info",
    "captured_exception_to_dict",
    "stackframe_to_dict",
]
