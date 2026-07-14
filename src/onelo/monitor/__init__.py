"""Onelo Monitor — server-side error / event tracking for Python.

This module gives Python servers the same monitoring surface as the Swift
SDK: capture exceptions with full stack traces, attach per-request
breadcrumbs, correlate errors with active feature flags, and stream events
to the Onelo dashboard.

Three scope layers (global / isolation / current) live on ``contextvars``
so concurrent requests don't bleed user IDs or breadcrumbs into each other.
Middleware integrations fork an isolation scope per request automatically.

Phase 1A — this revision — ships the core data model + scope + capture
pipeline. Phase 1B wires the HTTP transport (``init``, batching, flush).
Until 1B lands, ``capture_*`` calls are silent no-ops unless a test sets
a sink via ``set_transport``.

Quick taste (post-1B):

    from onelo import Onelo
    from onelo import monitor

    onelo = Onelo(publishable_key="onelo_pk_live_…")
    monitor.init(onelo=onelo)

    try:
        risky()
    except Exception as e:
        monitor.capture_exception(e)
"""
from __future__ import annotations

from onelo.monitor._capture import (
    add_breadcrumb,
    capture_event,
    capture_exception,
    capture_message,
    set_context,
    set_extra,
    set_flag_provider,
    set_tag,
    set_transport,
    set_user,
    track,
)
from onelo.monitor._executor import ScopeAwareExecutor, scope_aware
from onelo.monitor._init import close, flush, init, is_initialised
from onelo.monitor._propagation import carrier, continue_trace, continue_trace_task
from onelo.monitor._scope import (
    Scope,
    get_current_scope,
    get_global_scope,
    get_isolation_scope,
    identified_scope,
    isolation_scope,
    new_scope,
)
from onelo.monitor._types import (
    Breadcrumb,
    BreadcrumbCategory,
    CapturedException,
    EventLevel,
    MonitorEvent,
    StackFrame,
)


__all__ = [
    # Lifecycle
    "init",
    "close",
    "flush",
    "is_initialised",
    # Capture API
    "add_breadcrumb",
    "capture_event",
    "capture_exception",
    "capture_message",
    "set_context",
    "set_extra",
    "set_flag_provider",
    "set_tag",
    "set_transport",
    "set_user",
    "track",
    # Scope API
    "Scope",
    "get_current_scope",
    "get_global_scope",
    "get_isolation_scope",
    "identified_scope",
    "isolation_scope",
    "new_scope",
    # Background-job propagation
    "carrier",
    "continue_trace",
    "continue_trace_task",
    # Executor / thread propagation
    "ScopeAwareExecutor",
    "scope_aware",
    # Types
    "Breadcrumb",
    "BreadcrumbCategory",
    "CapturedException",
    "EventLevel",
    "MonitorEvent",
    "StackFrame",
]
