"""Public capture API — what user code calls to send an event to Onelo.

Three entry points mirror the Sentry vocabulary because devs migrating from
Sentry expect them:

    capture_exception(e)                  # caught an Exception
    capture_message("text", level="error")  # no exception, just a string
    capture_event(MonitorEvent(...))      # low-level, integrations only

All three:
  1. Build / accept a ``MonitorEvent``
  2. Layer scope data (global → isolation → current)
  3. Auto-attach feature-flag context (Onelo's USP — see __init__.py)
  4. Run the PII scrubber over ``meta`` and ``error``
  5. Hand the event to the transport (registered via ``set_transport``)

The transport itself lives in ``_transport.py`` (Faza 1B). For now we expose
a registration hook so this module is testable without HTTP.
"""
from __future__ import annotations

import inspect
import logging
import random
from functools import wraps
from time import time
from typing import Any, Callable

from onelo.monitor._scope import apply_scopes_to_event, get_isolation_scope
from onelo.monitor._scrub import scrub_meta, scrub_text
from onelo.monitor._stacktrace import (
    capture_current_stack,
    capture_exception_info,
    captured_exception_to_dict,
    stackframe_to_dict,
)
from onelo.monitor._types import EventLevel, MonitorEvent
from onelo._version import __version__


_log = logging.getLogger("onelo.monitor")

# SDK identity auto-attached to every event as ``meta.sdk`` so the dashboard can
# show the "SDK version" column and flag outdated installs — mirrors the JS SDK
# (``meta.sdk = {name, version}``). The developer never sets this.
_SDK_NAME = "onelo-python"


# ─── Transport hook ─────────────────────────────────────────────────────────
# The capture API must not import ``_transport`` directly (that would create a
# cycle once transport calls back into ``_capture`` for retries). Instead the
# init code in ``__init__.py`` registers a sink here.

TransportSink = Callable[[MonitorEvent], None]

_sink: TransportSink | None = None


def set_transport(sink: TransportSink | None) -> None:
    """Register the function that will receive scrubbed, scope-applied events.

    Pass ``None`` to disable capture (events are silently dropped). Used by
    tests and by ``monitor.init`` once the HTTP transport is ready.
    """
    global _sink
    _sink = sink


def get_transport() -> TransportSink | None:
    return _sink


# ─── Flag provider hook — Onelo's killer feature ────────────────────────────
# The provider is a callable that returns a {flag_name: status} dict for
# the current request. ``monitor.init(onelo=...)`` wires it to
# ``onelo._cache.snapshot()`` so every captured event ships with the flag
# state that was live at that moment. Sentry doesn't have flags. PostHog
# doesn't have stack traces. We have both — auto-correlated.

FlagProvider = Callable[[], dict[str, str]]

_flag_provider: FlagProvider | None = None


def set_flag_provider(provider: FlagProvider | None) -> None:
    """Register a callable that returns active feature flags for events.

    Pass ``None`` to clear. The provider is called once per captured event,
    inside the capture pipeline — it must be cheap (~µs) and never raise.
    """
    global _flag_provider
    _flag_provider = provider


def get_flag_provider() -> FlagProvider | None:
    return _flag_provider


# ─── Session ID — server-side equivalent of mobile install ID ──────────────
# A persistent install ID like Swift uses doesn't apply to server processes
# (they're stateless, often containerised, churned constantly). Instead we
# generate one UUID per process and stamp it on every event whose
# ``session_id`` is empty. Combined with ``release`` and ``server_name``
# tags on the global scope, this lets the dashboard group events by a
# single Python worker / pod across requests.

_session_id: str | None = None


def set_session_id(session_id: str | None) -> None:
    """Override the per-process session ID. ``monitor.init`` sets one
    automatically; tests may swap it."""
    global _session_id
    _session_id = session_id


def get_session_id() -> str | None:
    return _session_id


# ─── Deploy context — release / environment (backend-read meta paths) ───────
# monitor.init() also puts these on the global scope as tags, but the backend
# aggregates its Feature-Health Release/Environment dimensions from SPECIFIC
# meta paths — meta.app.version (nested) and meta.environment (top level) — NOT
# from meta.tags.* (see backend/app/routes/sdk_monitor.py _release_dim / _dim).
# We stamp those exact paths in _emit so the dashboard filters populate. Every
# other SDK (Swift/JS/Electron) already ships meta.app.version; Python was the
# odd one out putting release only under meta.tags.release.

_deploy_release: str | None = None
_deploy_environment: str | None = None
_deploy_server_name: str | None = None


def set_deploy_context(
    release: str | None,
    environment: str | None,
    server_name: str | None,
) -> None:
    """Record the process's release / environment / server_name so ``_emit``
    can stamp them onto the exact meta paths the backend reads. Called by
    ``monitor.init`` on every configure (overwrites, so re-init fully resets)."""
    global _deploy_release, _deploy_environment, _deploy_server_name
    _deploy_release = release
    _deploy_environment = environment
    _deploy_server_name = server_name


# ─── Client-side sampling ───────────────────────────────────────────────────
# Probability an event is KEPT. Separate knobs for errors vs successes because
# they have very different volume/value: you usually keep every error but may
# heavily sample high-volume success/track events. Default 1.0 = keep all, so
# behaviour is unchanged unless a dev opts in. Sampling happens client-side
# BEFORE the network so an error storm can't thrash the buffer / trip the
# backend quota (which then drops for an hour).

_error_sample_rate: float = 1.0
_success_sample_rate: float = 1.0


def set_sample_rates(error_rate: float, success_rate: float) -> None:
    """Set keep-probabilities for error (ok=False) and success (ok=True) events.
    Both in [0.0, 1.0]. ``monitor.init`` validates and sets these; ``close``
    resets to 1.0."""
    global _error_sample_rate, _success_sample_rate
    _error_sample_rate = error_rate
    _success_sample_rate = success_rate


def _should_keep(event: MonitorEvent) -> bool:
    """Decide whether to keep ``event`` under the configured sample rates.
    Fast paths for the common rate==1.0 (keep) and rate==0.0 (drop)."""
    rate = _error_sample_rate if not event.ok else _success_sample_rate
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


# ─── Public capture API ─────────────────────────────────────────────────────


def capture_exception(
    exc: BaseException | None = None,
    *,
    feature_name: str = "manual",
    meta: dict[str, Any] | None = None,
) -> None:
    """Record an exception. Pass nothing to use the current ``sys.exc_info()``.

    The captured event includes:
      - exception type, message, full stack frames (with in-app classifier)
      - chained exception cause (``raise X from Y``)
      - all breadcrumbs from the active isolation scope
      - active user / tags / contexts from all three scope layers
      - active feature flags (auto-attached by integrations)
    """
    captured = capture_exception_info(exc)
    if captured is None:
        _log.debug("capture_exception called outside an except block; nothing to do")
        return

    enriched_meta: dict[str, Any] = dict(meta) if meta else {}
    enriched_meta["exception"] = captured_exception_to_dict(captured)
    enriched_meta.setdefault("error_type", captured.type)

    event = MonitorEvent(
        feature_name=feature_name,
        ok=False,
        error=captured.message,
        source="event",
        meta=enriched_meta,
    )
    _emit(event)


def capture_message(
    message: str,
    *,
    level: EventLevel = "info",
    feature_name: str = "manual",
    attach_stacktrace: bool = False,
    meta: dict[str, Any] | None = None,
) -> None:
    """Record a free-form message without an exception.

    ``level`` follows OTel/Sentry vocabulary (debug | info | warning | error
    | fatal). For ``error`` and ``fatal`` we set ``ok=False`` so dashboards
    treat the row as a failure.
    """
    enriched: dict[str, Any] = dict(meta) if meta else {}
    enriched["level"] = level
    if attach_stacktrace:
        enriched["stack"] = [stackframe_to_dict(f) for f in capture_current_stack(skip=1)]

    is_error = level in ("error", "fatal")
    event = MonitorEvent(
        feature_name=feature_name,
        ok=not is_error,
        error=message if is_error else None,
        source="event",
        meta=enriched,
    )
    _emit(event)


def _emit_session_opened() -> None:
    """Auto-emitted, unconditional baseline event — called once by
    ``monitor.init`` right after the transport is wired up.

    Guarantees the dashboard shows *something* even when every
    developer-instrumented ``track()``/``event()`` call sits downstream of
    an auth gate / feature flag that never fires on a cold start (the bug
    this exists to fix). ``ok=True``, no error, no extra meta — mirrors the
    JS SDK's ``session_opened`` (``monitor.ts``). Exempt from sampling (see
    ``_emit``'s ``bypass_sampling``) since a randomly-dropped baseline event
    would defeat its own purpose.
    """
    _emit(
        MonitorEvent(
            feature_name="session_opened",
            ok=True,
            source="event",
        ),
        bypass_sampling=True,
    )


def capture_event(event: MonitorEvent) -> None:
    """Low-level: hand a fully-built event to the pipeline.

    Skip enrichment (caller is responsible). Still runs scope merge + scrub —
    those are non-negotiable. Integrations use this when they have a
    pre-formatted event (e.g. from MetricKit, from a worker payload).
    """
    _emit(event)


# ─── track() — performance + error span, unified with the Swift SDK ─────────


class _TrackSpan:
    """Span returned by :func:`track`. Measures wall-clock duration, emits one
    ``source="track"`` event (``ok=True`` on success, an error event with full
    stack/breadcrumbs/flags on exception), and never suppresses the exception —
    your control flow is unchanged, exactly like Swift's ``track``.

    Usable three ways; pick whichever fits the call site:

        with monitor.track("checkout", meta={"plan": "pro"}):
            process_payment()

        async with monitor.track("ai_response", meta={"model": "gpt-4"}):
            await call_model()

        @monitor.track("pdf_export")
        def export(doc): ...
    """

    __slots__ = ("_feature_name", "_meta", "_start")

    def __init__(self, feature_name: str, meta: dict[str, Any] | None) -> None:
        self._feature_name = feature_name
        self._meta = meta
        self._start = 0.0

    # ── sync context manager ──
    def __enter__(self) -> "_TrackSpan":
        self._start = time()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        self._finish(exc)
        return False  # never suppress — propagate like Swift's `rethrows`

    # ── async context manager (same semantics; no awaiting needed inside) ──
    async def __aenter__(self) -> "_TrackSpan":
        self._start = time()
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        self._finish(exc)
        return False

    # ── decorator (works on sync and async functions) ──
    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        # Build a FRESH span per call so concurrent invocations don't share
        # the start timestamp.
        feature_name, meta = self._feature_name, self._meta
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with _TrackSpan(feature_name, meta):
                    return await fn(*args, **kwargs)

            return async_wrapper

        @wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _TrackSpan(feature_name, meta):
                return fn(*args, **kwargs)

        return sync_wrapper

    def _finish(self, exc: BaseException | None) -> None:
        duration_ms = int((time() - self._start) * 1000)
        if exc is None:
            _emit(MonitorEvent(
                feature_name=self._feature_name,
                ok=True,
                source="track",
                duration_ms=duration_ms,
                meta=dict(self._meta) if self._meta else {},
            ))
            return

        # Failure path — same enrichment as capture_exception (full stack
        # frames + chained cause), tagged source="track" and carrying the
        # measured duration. Breadcrumbs + active flags are auto-attached by
        # ``_emit``. The exception itself is re-raised by __exit__/__aexit__.
        meta: dict[str, Any] = dict(self._meta) if self._meta else {}
        captured = capture_exception_info(exc)
        if captured is not None:
            meta["exception"] = captured_exception_to_dict(captured)
            meta.setdefault("error_type", captured.type)
            error_msg: str | None = captured.message
        else:
            meta.setdefault("error_type", type(exc).__name__)
            error_msg = str(exc)
        _emit(MonitorEvent(
            feature_name=self._feature_name,
            ok=False,
            error=error_msg,
            source="track",
            duration_ms=duration_ms,
            meta=meta,
        ))


def track(feature_name: str, *, meta: dict[str, Any] | None = None) -> _TrackSpan:
    """Measure an operation and emit one ``source="track"`` event — the Python
    equivalent of Swift's ``track``.

    Wraps a unit of work, records its wall-clock duration, emits ``ok=True`` on
    success or an error event (with full stack trace, breadcrumbs and active
    feature flags) on exception, then lets the exception propagate unchanged.

    Python has no closure-passing idiom like Swift's trailing closure, so
    ``track`` returns a span usable as a context manager (sync **or** async) or
    as a decorator:

        with monitor.track("checkout", meta={"plan": "pro"}):
            process_payment()

        async with monitor.track("ai_response", meta={"model": "gpt-4"}):
            await call_model()

        @monitor.track("pdf_export")
        async def export(doc): ...
    """
    return _TrackSpan(feature_name, meta)


def add_breadcrumb(crumb_or_message: Any, **kwargs: Any) -> None:
    """Append a breadcrumb to the active isolation scope.

    Two call shapes:

        add_breadcrumb(Breadcrumb.info("loaded users"))
        add_breadcrumb("loaded users", category="db", duration_ms=42)
    """
    from onelo.monitor._types import Breadcrumb

    if isinstance(crumb_or_message, Breadcrumb):
        get_isolation_scope().add_breadcrumb(crumb_or_message)
        return

    # String shape: build a default breadcrumb.
    category = kwargs.pop("category", "info")
    level = kwargs.pop("level", "info")
    crumb = Breadcrumb(
        category=category,
        message=str(crumb_or_message),
        timestamp=__import__("time").time(),
        level=level,
        data=dict(kwargs) if kwargs else None,
    )
    get_isolation_scope().add_breadcrumb(crumb)


def set_user(user: dict[str, Any] | None) -> None:
    """Set the user on the active isolation scope (per-request, not global)."""
    get_isolation_scope().set_user(user)


def set_tag(key: str, value: str) -> None:
    get_isolation_scope().set_tag(key, value)


def set_context(key: str, value: dict[str, Any]) -> None:
    get_isolation_scope().set_context(key, value)


def set_extra(key: str, value: Any) -> None:
    get_isolation_scope().set_extra(key, value)


# ─── internals ──────────────────────────────────────────────────────────────


def _emit(event: MonitorEvent, *, bypass_sampling: bool = False) -> None:
    """Run the full pipeline: scope merge → flag enrichment → PII scrub → transport.

    ``bypass_sampling`` skips ``_should_keep`` — used exactly once, by the
    auto-emitted ``session_opened`` event (see ``monitor.init``). That event
    exists specifically to guarantee a baseline signal reaches the dashboard
    even when every developer-instrumented call sits behind an auth gate /
    feature flag; letting ``success_sample_rate`` randomly drop it would
    defeat its entire purpose.
    """
    if _sink is None:
        # Capture API may be called before init (or after destroy). Drop
        # silently — surfacing an error here would be hostile to user code.
        return

    # Client-side sampling — decided BEFORE scope merge / scrub / transport so a
    # dropped event costs almost nothing. Keeps an error storm from thrashing
    # the buffer and tripping the backend hourly quota.
    if not bypass_sampling and not _should_keep(event):
        return

    apply_scopes_to_event(event)

    # Stamp the per-process session ID if no integration set one. This is
    # the server-side analogue of Swift's persistent install ID — groups
    # all events from a single worker / pod into one session in the dashboard.
    if event.session_id is None and _session_id is not None:
        event.session_id = _session_id

    # Stamp deploy context onto the EXACT meta paths the backend aggregates on —
    # meta.environment (top level) and meta.app.version (nested). The scope only
    # writes these under meta.tags.*, which the backend's Release/Environment
    # dimensions ignore (backend/app/routes/sdk_monitor.py _dim / _release_dim),
    # so without this the dashboard filters are empty for Python events.
    # setdefault semantics: an explicit per-event value or integration wins.
    if _deploy_environment and "environment" not in event.meta:
        event.meta["environment"] = _deploy_environment
    if _deploy_release:
        app_ctx = event.meta.setdefault("app", {})
        if isinstance(app_ctx, dict):
            app_ctx.setdefault("version", _deploy_release)

    # Auto-attach active feature-flag state. The provider is cheap (snapshot
    # of an in-memory dict) and gives every event the flag context for free —
    # devs can answer "is this error correlated with flag X being enabled?"
    # without lifting a finger.
    if _flag_provider is not None:
        try:
            flags = _flag_provider()
        except Exception:  # noqa: BLE001
            flags = None
        if flags:
            event.meta.setdefault("flags", dict(flags))

    # Auto-attach the SDK identity so the dashboard can surface the "SDK
    # version" column and flag outdated installs — same as the JS / Swift SDKs,
    # no developer setup needed. setdefault so an explicit meta["sdk"] wins.
    event.meta.setdefault("sdk", {"name": _SDK_NAME, "version": __version__})

    # Scrub at the boundary, after enrichment, before handing off. Anything
    # added by integrations (request URL, headers via contexts, breadcrumbs
    # with HTTP details) goes through here.
    #
    # The whole scrub block is wrapped in try/except because user-supplied
    # ``meta`` can contain awkward types (list / tuple subclasses with a
    # custom ``__init__`` like namedtuples; ``UserDict`` shapes; objects with
    # a misbehaving ``__iter__``). A failure here MUST NOT propagate — we
    # promise "monitoring never crashes user code", and a re-raise from this
    # path can also re-enter the excepthook and loop forever.
    try:
        event.error = scrub_text(event.error)
        scrubbed_meta = scrub_meta(event.meta)
        event.meta = scrubbed_meta if scrubbed_meta is not None else {}
    except Exception:  # noqa: BLE001
        _log.warning("monitor scrubber failed; dropping event", exc_info=True)
        return

    try:
        _sink(event)
    except Exception:  # noqa: BLE001 — transport must never crash user code
        _log.warning("monitor transport failed", exc_info=True)


__all__ = [
    "add_breadcrumb",
    "capture_event",
    "capture_exception",
    "capture_message",
    "get_transport",
    "set_context",
    "set_extra",
    "set_tag",
    "set_transport",
    "set_user",
    "track",
]
