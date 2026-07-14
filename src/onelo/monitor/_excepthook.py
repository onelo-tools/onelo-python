"""Install handlers for uncaught exceptions.

Three places an exception can escape user code without a framework error
handler catching it first:

  1. ``sys.excepthook`` — anything thrown from the main thread that bubbles
     all the way up. Mostly script / CLI / startup errors. Most web frameworks
     catch first.
  2. ``threading.excepthook`` — Python 3.8+, errors in spawned threads.
     Often background workers / Celery in eager mode / scheduled jobs.
  3. ``loop.set_exception_handler`` — asyncio task errors that nobody
     ``await``ed. The "Task was destroyed but it is pending" class.

We chain to whatever handler was installed before us (Sentry, custom logging)
so multiple SDKs coexist. Removing our handler restores the prior chain
exactly.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from types import TracebackType
from typing import Any

from onelo.monitor._capture import capture_exception, capture_message


# Saved references to the previous handlers so ``uninstall`` restores cleanly.
_prev_sys_excepthook: Any = None
_prev_thread_excepthook: Any = None
_loop_handlers_installed: list[asyncio.AbstractEventLoop] = []
# Per-loop saved previous handler so we can restore exactly what was there
# before us — instead of blindly clearing to ``None`` and wiping out a
# Sentry / OTel / custom handler that registered after us.
_prev_loop_handlers: dict[int, Any] = {}
_installed = False


def install() -> None:
    """Idempotent — calling twice is a no-op."""
    global _installed, _prev_sys_excepthook, _prev_thread_excepthook
    if _installed:
        return

    _prev_sys_excepthook = sys.excepthook
    sys.excepthook = _onelo_sys_excepthook

    _prev_thread_excepthook = threading.excepthook
    threading.excepthook = _onelo_thread_excepthook

    # asyncio loop handlers are per-loop. We install on the running loop if
    # there is one; integrations (ASGI middleware) install on theirs.
    try:
        loop = asyncio.get_running_loop()
        install_for_loop(loop)
    except RuntimeError:
        pass  # no running loop — fine, integrations will register theirs

    _installed = True


def uninstall() -> None:
    """Restore previous handlers. Used for tests + ``monitor.close()``.

    Critical chaining rule: only restore if our hook is still the active
    one. If another SDK (Sentry, OTel, application logger) installed its
    own handler **after** us, blindly assigning the saved value would wipe
    that SDK's handler — silently breaking it. We therefore check identity
    before restoring.
    """
    global _installed, _prev_sys_excepthook, _prev_thread_excepthook
    if not _installed:
        return

    # sys.excepthook — only restore if ours is still installed.
    if sys.excepthook is _onelo_sys_excepthook and _prev_sys_excepthook is not None:
        sys.excepthook = _prev_sys_excepthook
    _prev_sys_excepthook = None

    # threading.excepthook — same identity check.
    if (
        threading.excepthook is _onelo_thread_excepthook
        and _prev_thread_excepthook is not None
    ):
        threading.excepthook = _prev_thread_excepthook
    _prev_thread_excepthook = None

    for loop in list(_loop_handlers_installed):
        if loop.is_closed():
            continue
        prev = _prev_loop_handlers.get(id(loop))
        # Only restore if the active handler is still ours; otherwise leave
        # whoever replaced us in charge.
        try:
            current = loop.get_exception_handler()
        except RuntimeError:
            # Loop torn down between checks.
            continue
        if current is _onelo_loop_exception_handler:
            loop.set_exception_handler(prev)
    _loop_handlers_installed.clear()
    _prev_loop_handlers.clear()

    _installed = False


def install_for_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Register our asyncio exception handler on a specific loop.

    Useful when a framework spins up its own event loop after our ``init``
    (e.g. uvicorn worker) — call this from the integration's lifespan hook.

    Saves the previously-installed handler so ``uninstall()`` can restore
    it exactly instead of wiping it to the default.
    """
    if loop in _loop_handlers_installed:
        return
    try:
        prev = loop.get_exception_handler()
    except RuntimeError:
        prev = None
    _prev_loop_handlers[id(loop)] = prev
    loop.set_exception_handler(_onelo_loop_exception_handler)
    _loop_handlers_installed.append(loop)


# ─── handlers ───────────────────────────────────────────────────────────────


def _onelo_sys_excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    # KeyboardInterrupt / SystemExit are intentional — never report.
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        if _prev_sys_excepthook is not None:
            _prev_sys_excepthook(exc_type, exc, tb)
        return

    # `capture_exception` uses sys.exc_info() if no exc passed; pass explicitly.
    try:
        # Re-attach traceback because the exception object may have lost it
        # by the time we get here (rare, but harmless to do).
        if tb is not None and exc.__traceback__ is None:
            exc = exc.with_traceback(tb)
        capture_exception(exc, feature_name="uncaught")
    except Exception:  # noqa: BLE001
        pass  # excepthook must never raise

    if _prev_sys_excepthook is not None:
        _prev_sys_excepthook(exc_type, exc, tb)


def _onelo_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    # Python 3.8+: signature is `args: ExceptHookArgs` with .exc_value etc.
    exc = args.exc_value
    if exc is None:
        return
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        if _prev_thread_excepthook is not None:
            _prev_thread_excepthook(args)
        return

    try:
        if args.exc_traceback is not None and exc.__traceback__ is None:
            exc = exc.with_traceback(args.exc_traceback)
        capture_exception(exc, feature_name="thread_uncaught")
    except Exception:  # noqa: BLE001
        pass

    if _prev_thread_excepthook is not None:
        _prev_thread_excepthook(args)


def _onelo_loop_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    """asyncio context hook. Called for "task exception was never retrieved"
    and similar conditions where the loop has no waiter.
    """
    exc = context.get("exception")
    message = context.get("message", "asyncio: unhandled exception")
    try:
        if isinstance(exc, BaseException):
            capture_exception(exc, feature_name="asyncio_uncaught")
        else:
            capture_message(message, level="error", feature_name="asyncio_uncaught")
    except Exception:  # noqa: BLE001
        pass

    # Defer to default handler so the user still sees the warning.
    loop.default_exception_handler(context)


__all__ = ["install", "install_for_loop", "uninstall"]
