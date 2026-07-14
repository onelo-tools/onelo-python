"""Helpers for propagating monitor scope across thread / executor boundaries.

Python's ``contextvars`` propagate cleanly through ``await`` and
``asyncio.create_task`` (PEP 567), but they do **not** automatically follow
work submitted to:

  - ``concurrent.futures.ThreadPoolExecutor.submit``
  - ``loop.run_in_executor``
  - ``threading.Thread(target=...).start()``

Without explicit propagation, code like this silently leaks request scope:

    @app.get("/checkout")
    async def checkout(req):
        monitor.set_user({"id": req.user.id})
        # !!! background work runs without the user's scope
        executor.submit(send_receipt_email, req.user.email)

The fix is to copy the current ``Context`` and run the worker inside it.
This module exposes two thin wrappers that make that the default behaviour.

Pattern A — wrap the callable:

    from onelo.monitor import scope_aware

    executor.submit(scope_aware(send_receipt_email), req.user.email)

Pattern B — wrap the executor (rarely needed):

    from onelo.monitor import ScopeAwareExecutor

    pool = ScopeAwareExecutor(ThreadPoolExecutor(max_workers=4))
    pool.submit(send_receipt_email, req.user.email)
    # all submitted callables inherit the caller's scope automatically
"""
from __future__ import annotations

import contextvars
import functools
from concurrent.futures import Executor, Future
from typing import Any, Callable, ParamSpec, TypeVar


P = ParamSpec("P")
R = TypeVar("R")


def scope_aware(func: Callable[P, R]) -> Callable[P, R]:
    """**Call-site helper** — wraps ``func`` so it runs in the calling
    thread's ContextVars context.

    IMPORTANT: not a module-level decorator. The context is captured when
    ``scope_aware(func)`` is *called*, so call it inside the request
    handler (or wherever the right scope is active), then pass the result
    to your executor:

        # ❌ wrong — captures at module import time, scope is empty
        @scope_aware
        def send_email(addr): ...

        # ✓ right — capture inside the handler, submit the wrapper
        def send_email(addr): ...
        executor.submit(scope_aware(send_email), req.user.email)

    For the more common case of "I just want my pool to always inherit
    scope", use ``ScopeAwareExecutor`` which captures on every ``submit``
    automatically.
    """
    ctx = contextvars.copy_context()

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return ctx.run(func, *args, **kwargs)
    return wrapper


class ScopeAwareExecutor:
    """Thin wrapper around any ``concurrent.futures.Executor`` that captures
    the caller's ContextVars on every ``submit`` so the worker sees the
    same monitor scope (and any other ContextVar-based state).

    ``map`` is also wrapped. Other methods (``shutdown``) pass through.
    Implements ``Executor`` informally — duck-typed, no inheritance, so
    you can wrap a real ``ThreadPoolExecutor`` or any compatible class.
    """

    def __init__(self, inner: Executor) -> None:
        self._inner = inner

    def submit(self, fn: Callable[..., R], /, *args: Any, **kwargs: Any) -> Future[R]:
        ctx = contextvars.copy_context()
        return self._inner.submit(ctx.run, fn, *args, **kwargs)

    def map(self, fn: Callable[..., R], *iterables: Any, **kwargs: Any) -> Any:
        # ``map`` doesn't pre-resolve args, so wrap the callable instead of
        # capturing once and risking a stale ctx for later iterations.
        return self._inner.map(scope_aware(fn), *iterables, **kwargs)

    def shutdown(self, *args: Any, **kwargs: Any) -> None:
        self._inner.shutdown(*args, **kwargs)

    def __enter__(self) -> "ScopeAwareExecutor":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)


__all__ = ["ScopeAwareExecutor", "scope_aware"]
