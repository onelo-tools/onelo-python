"""Data classes for monitor events, breadcrumbs and runtime context.

Kept dependency-free (stdlib only) so this module can be imported from any
sub-module without circular imports. Frozen dataclasses for immutability —
events flow through transport without anyone mutating them mid-flight.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Literal


BreadcrumbCategory = Literal[
    "navigation",
    "http",
    "feature",
    "lifecycle",
    "user",
    "info",
    "error",
    "log",
    "auth",
    "db",
]
"""Coarse classification of a breadcrumb. Mirrors the Swift SDK enum so the
backend can render breadcrumbs uniformly across platforms.
"""


EventLevel = Literal["debug", "info", "warning", "error", "fatal"]


@dataclass(frozen=True)
class Breadcrumb:
    """A single trace entry attached to the next captured event.

    Created via convenience helpers (``Breadcrumb.info``, ``Breadcrumb.http``)
    or directly by integrations. Never mutate after creation — buffers rely
    on the frozen invariant.
    """

    category: BreadcrumbCategory
    message: str
    timestamp: float
    level: EventLevel = "info"
    data: dict[str, Any] | None = None

    @classmethod
    def info(cls, message: str, **data: Any) -> "Breadcrumb":
        return cls(
            category="info",
            message=message,
            timestamp=time(),
            data=dict(data) if data else None,
        )

    @classmethod
    def http(
        cls,
        method: str,
        url: str,
        status: int | None = None,
        duration_ms: int | None = None,
        **extra: Any,
    ) -> "Breadcrumb":
        # Scrub the URL eagerly so secrets in query params never sit in the
        # breadcrumb buffer — the deferred scrubber on `_emit` only catches
        # known regex patterns, not URL-component-aware redaction.
        # Imported here (not at module level) because `_scrub` may grow to
        # import other monitor modules and a top-level cycle would bite us.
        from onelo.monitor._scrub import scrub_url

        scrubbed = scrub_url(url) or url
        data: dict[str, Any] = {"method": method.upper(), "url": scrubbed}
        if status is not None:
            data["status"] = status
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        data.update(extra)
        return cls(
            category="http",
            message=f"{method.upper()} {scrubbed}",
            timestamp=time(),
            data=data,
        )

    @classmethod
    def feature(cls, name: str, value: str) -> "Breadcrumb":
        return cls(
            category="feature",
            message=name,
            timestamp=time(),
            data={"value": value},
        )

    @classmethod
    def lifecycle(cls, event: str) -> "Breadcrumb":
        return cls(
            category="lifecycle",
            message=event,
            timestamp=time(),
        )


@dataclass(frozen=True)
class StackFrame:
    """A single frame inside a captured stack trace. OTel-compatible names.

    `function`, `module`, `filename`, `lineno` map directly to OTel
    ``exception.stacktrace`` decomposition. ``in_app`` flags whether the
    frame is the developer's own code (vs. stdlib / third-party) so the
    dashboard can collapse noise.
    """

    function: str | None
    module: str | None
    filename: str | None
    lineno: int | None
    in_app: bool = True
    context_line: str | None = None


@dataclass(frozen=True)
class CapturedException:
    """An exception flattened to a transport-safe shape.

    Keeps the original `type` name and message; `frames` is the formatted
    backtrace with most-recent frame last (Python convention, opposite of
    JS). Backends symbolicate / group on `(type, frames[*].function)`.
    """

    type: str
    message: str
    frames: list[StackFrame]
    cause: "CapturedException | None" = None  # for `raise X from Y`


@dataclass
class MonitorEvent:
    """The wire envelope sent to /api/sdk/monitor/events/batch.

    Mutable on purpose — integrations enrich it (set user, attach feature
    flags, scrub PII) before transport hands it off. Frozen would force
    a wasteful copy at every step.
    """

    feature_name: str
    ok: bool
    timestamp: float = field(default_factory=time)
    duration_ms: int | None = None
    error: str | None = None
    source: str = "event"
    user_id: str | None = None
    platform: str = "python"
    session_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "Breadcrumb",
    "BreadcrumbCategory",
    "CapturedException",
    "EventLevel",
    "MonitorEvent",
    "StackFrame",
]
