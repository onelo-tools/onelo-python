"""Three-scope state model — global, isolation, current.

Mirrors the Sentry-Python 2.x design (which deprecated the older Hub model)
because it solves the right problem with stdlib primitives:

    global       — one per process (release, environment, server_name)
    isolation    — one per request / coroutine / worker thread
                   (user, breadcrumbs, tags scoped to one request)
    current      — innermost ``with new_scope():`` block (transaction span)

When an event is captured, the three scopes are merged in order:
``global ⊕ isolation ⊕ current``. Later scopes override earlier ones, but
breadcrumbs are concatenated, not replaced.

Storage uses ``contextvars.ContextVar`` so:
  - ``asyncio.create_task()`` propagates scope automatically (PEP 567)
  - synchronous code in the same OS thread shares scope until reset
  - ``ThreadPoolExecutor`` does **not** propagate without ``copy_context()`` —
    the executor integration (later) takes care of that.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from copy import copy
from typing import Any, Iterator

from onelo.monitor._breadcrumbs import DEFAULT_CAPACITY, BreadcrumbBuffer
from onelo.monitor._types import Breadcrumb, EventLevel, MonitorEvent


class Scope:
    """Mutable per-layer state.

    Three instances per request flow:
      - one process-global (created once)
      - one isolation (forked per request by middleware)
      - zero+ current (pushed by ``with monitor.new_scope():`` blocks)

    The same Scope class is reused at every layer; ``apply_to`` defines how
    its fields layer onto an outgoing event.
    """

    __slots__ = (
        "user",
        "tags",
        "contexts",
        "extra",
        "level",
        "transaction",
        "breadcrumbs",
        "_event_processors",
    )

    def __init__(self, breadcrumb_capacity: int = DEFAULT_CAPACITY) -> None:
        self.user: dict[str, Any] | None = None
        self.tags: dict[str, str] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.extra: dict[str, Any] = {}
        self.level: EventLevel | None = None
        self.transaction: str | None = None
        self.breadcrumbs = BreadcrumbBuffer(capacity=breadcrumb_capacity)
        self._event_processors: list[Any] = []

    # ─── mutation helpers ───────────────────────────────────────────────

    def set_user(self, user: dict[str, Any] | None) -> None:
        self.user = dict(user) if user else None

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value

    def set_context(self, key: str, value: dict[str, Any]) -> None:
        self.contexts[key] = dict(value)

    def set_extra(self, key: str, value: Any) -> None:
        self.extra[key] = value

    def set_level(self, level: EventLevel) -> None:
        self.level = level

    def set_transaction(self, name: str | None) -> None:
        self.transaction = name

    def add_breadcrumb(self, crumb: Breadcrumb) -> None:
        self.breadcrumbs.add(crumb)

    def clear_breadcrumbs(self) -> None:
        self.breadcrumbs.clear()

    def clone(self) -> "Scope":
        """Shallow-copy with a fresh breadcrumb buffer.

        Used at request boundaries (middleware forks isolation scope from
        the global one) — we want to inherit user/tags/contexts but not
        leak breadcrumbs from a previous request.
        """
        new = Scope(breadcrumb_capacity=self.breadcrumbs.capacity)
        new.user = copy(self.user) if self.user else None
        new.tags = dict(self.tags)
        new.contexts = {k: dict(v) for k, v in self.contexts.items()}
        new.extra = dict(self.extra)
        new.level = self.level
        new.transaction = self.transaction
        # Fresh buffer — explicitly do not copy breadcrumbs.
        return new

    # ─── application to outgoing event ──────────────────────────────────

    def apply_to(self, event: MonitorEvent) -> None:
        """Layer this scope's data onto an outgoing event.

        Called once per scope (global → isolation → current) by ``capture``.
        Order matters: later scopes override earlier ones for scalar fields,
        but breadcrumbs are appended (concat, not replace).
        """
        if self.user and event.user_id is None:
            uid = self.user.get("id")
            # Use ``is not None`` rather than truthiness: a legitimate
            # user identifier can be 0, "", or False (rare but valid for
            # numeric / opaque IDs). Falsy filtering would silently drop
            # those events from per-user analytics.
            if uid is not None:
                event.user_id = str(uid)
        if self.tags:
            event.meta.setdefault("tags", {}).update(self.tags)
        if self.contexts:
            ctx_bucket = event.meta.setdefault("contexts", {})
            for k, v in self.contexts.items():
                ctx_bucket[k] = dict(v)
        if self.extra:
            event.meta.setdefault("extra", {}).update(self.extra)
        if self.transaction and "transaction" not in event.meta:
            event.meta["transaction"] = self.transaction
        if self.breadcrumbs:
            existing = event.meta.setdefault("breadcrumbs", [])
            existing.extend(self.breadcrumbs.to_event_data())


# ─── ContextVar storage ─────────────────────────────────────────────────────
#
# Three separate ContextVars so each layer is independently swappable. We
# default to None and lazy-init the global scope on first access — keeps
# import-time cost zero and lets tests reset state cleanly.

_global_scope: Scope | None = None
"""Process-global scope. Plain attribute (not ContextVar) because it really
is shared across all coroutines / threads."""

_isolation_scope_var: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "onelo_isolation_scope",
    default=None,
)

_current_scope_var: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "onelo_current_scope",
    default=None,
)


def get_global_scope() -> Scope:
    """Return (and lazy-init) the process-global scope."""
    global _global_scope
    if _global_scope is None:
        _global_scope = Scope()
    return _global_scope


def get_isolation_scope() -> Scope:
    """Return the active isolation scope, lazy-initialising from global on miss.

    Most user code touches this scope (``set_user``, ``add_breadcrumb``).
    Outside an explicit middleware fork the isolation scope is a clone of
    the global one — so set_user from a CLI script behaves as expected.
    """
    scope = _isolation_scope_var.get()
    if scope is None:
        scope = get_global_scope().clone()
        _isolation_scope_var.set(scope)
    return scope


def get_current_scope() -> Scope:
    """Return the innermost ``with new_scope()`` scope, falling back to isolation."""
    scope = _current_scope_var.get()
    if scope is None:
        return get_isolation_scope()
    return scope


@contextmanager
def isolation_scope() -> Iterator[Scope]:
    """Fork a fresh isolation scope for the duration of the ``with`` block.

    Used by ASGI/WSGI/Django/FastAPI/Flask middleware: each incoming request
    enters this block so its ``set_user`` / breadcrumbs cannot bleed into
    other concurrent requests.
    """
    parent = get_isolation_scope()
    forked = parent.clone()
    token = _isolation_scope_var.set(forked)
    try:
        yield forked
    finally:
        _isolation_scope_var.reset(token)


@contextmanager
def identified_scope(user_id: str | None) -> Iterator[Scope]:
    """Fork an isolation scope and attach ``user_id`` for its duration.

    The ergonomic counterpart to the HTTP middleware for places the
    request middleware can't reach: **WebSocket handlers** and
    **background tasks**. The HTTP ASGI middleware forks a per-request
    isolation scope automatically, but WebSocket and ``lifespan`` scopes
    are passed through untouched — so a long-lived socket has no per-user
    scope and its events ship as ``anonymous`` even when the handler knows
    who connected.

    Wrap the connection (or job) body in this manager once you've resolved
    the user, e.g. after ``verify_token``::

        onelo_user = await verify_token(onelo, token)
        with monitor.identified_scope(onelo_user.id):
            await websocket.accept()
            ...  # every event captured here carries user_id

    Because it forks a fresh isolation scope (one ``ContextVar`` slot per
    ``with`` block, inherited by child ``asyncio`` tasks via PEP 567),
    concurrent connections each get their own user — no cross-socket bleed.
    Passing ``None`` still forks an isolated scope but leaves the user
    unset (useful for unauthenticated connections so they don't inherit a
    stale identity).
    """
    with isolation_scope() as scope:
        if user_id is not None:
            scope.set_user({"id": str(user_id)})
        yield scope


@contextmanager
def new_scope() -> Iterator[Scope]:
    """Push a temporary current scope. Used by ``with monitor.new_scope()``
    for transaction-style nesting (set_tag inside the block doesn't leak out).
    """
    parent = get_current_scope()
    forked = parent.clone()
    token = _current_scope_var.set(forked)
    try:
        yield forked
    finally:
        _current_scope_var.reset(token)


def apply_scopes_to_event(event: MonitorEvent) -> None:
    """Layer global, isolation, current onto the event in that order."""
    get_global_scope().apply_to(event)
    iso = _isolation_scope_var.get()
    if iso is not None:
        iso.apply_to(event)
    cur = _current_scope_var.get()
    if cur is not None and cur is not iso:
        cur.apply_to(event)


def reset_for_tests() -> None:
    """Tear down all scope state. Called from test fixtures only —
    production code never resets the global scope.
    """
    global _global_scope
    _global_scope = None
    _isolation_scope_var.set(None)
    _current_scope_var.set(None)


__all__ = [
    "Scope",
    "apply_scopes_to_event",
    "get_current_scope",
    "get_global_scope",
    "get_isolation_scope",
    "isolation_scope",
    "new_scope",
    "reset_for_tests",
]
