"""Public ``monitor.init`` entry point.

Wires the three pieces together — transport, scope (release/environment
on the global scope), and excepthooks — so user code only needs one call:

    monitor.init(publishable_key="onelo_pk_live_…", api_url="https://…")

Or, when reusing an already-constructed Onelo client:

    from onelo import Onelo
    from onelo import monitor

    onelo = Onelo(publishable_key="onelo_pk_live_…")
    monitor.init(onelo=onelo)

The second form pulls ``publishable_key`` and ``api_url`` from the existing
client so config never gets out of sync.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from onelo.monitor import _capture, _excepthook
from onelo.monitor._scope import get_global_scope
from onelo.monitor._transport import (
    DEFAULT_BUFFER_SIZE,
    DEFAULT_FLUSH_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    MonitorTransport,
)


if TYPE_CHECKING:
    from onelo._client import Onelo


_log = logging.getLogger("onelo.monitor")


_active_transport: MonitorTransport | None = None
"""Process-wide singleton — ``init`` enforces single configuration."""

_fork_hook_registered = False
"""``os.register_at_fork`` persists for the process, so we register the child
rebuild hook exactly once even if ``init`` is called again."""


def _after_fork_child() -> None:
    """Rebuild the transport in a forked worker (gunicorn/uwsgi preload).

    Without this the worker inherits a dead transport thread and silently
    buffers-then-loses every event. Reads the current ``_active_transport`` so
    a re-``init`` before the fork is honoured; no-op if monitoring is off.
    """
    transport = _active_transport
    if transport is None:
        return
    try:
        transport.reinit_after_fork()
    except Exception:  # noqa: BLE001 — never crash a forking child
        _log.warning("monitor: transport reinit after fork failed", exc_info=True)


def init(
    *,
    onelo: "Onelo | None" = None,
    publishable_key: str | None = None,
    api_url: str | None = None,
    release: str | None = None,
    environment: str | None = None,
    server_name: str | None = None,
    install_excepthook: bool = True,
    strict_email_scrub: bool = False,
    sensitive_headers: "Iterable[str] | None" = None,
    sample_rate: float = 1.0,
    success_sample_rate: float = 1.0,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    flush_interval: float = DEFAULT_FLUSH_INTERVAL,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Configure the monitor module and start the transport thread.

    Parameters
    ----------
    onelo:
        Existing :class:`onelo.Onelo` client to reuse. When provided,
        ``publishable_key`` and ``api_url`` are pulled from it and overrides
        passed explicitly are ignored to avoid a mismatch between auth /
        features / monitor.
    publishable_key:
        Required if ``onelo`` is not given. ``onelo_pk_live_…`` /
        ``onelo_pk_test_…`` / ``onelo_sk_live_…`` (secret keys are accepted
        for server-side use).
    api_url:
        Base URL of the Onelo backend — ``https://api.onelo.tools`` in
        production. Required when ``publishable_key`` is used; there is no
        default because the host differs per environment. Ignored (and taken
        from the client) when ``onelo`` is passed.
    release:
        Build identifier — typically ``$GIT_COMMIT_SHA`` or
        ``$VERCEL_GIT_COMMIT_SHA``. Falls back to env ``ONELO_RELEASE``.
    environment:
        ``"production"`` / ``"staging"`` / ``"development"``. Falls back to
        env ``ONELO_ENVIRONMENT``.
    server_name:
        Hostname / pod ID. Falls back to env ``HOSTNAME`` / ``POD_NAME``.
    install_excepthook:
        Install ``sys.excepthook`` / ``threading.excepthook`` /
        ``loop.set_exception_handler`` so uncaught exceptions are reported
        even when no framework error handler catches them. Default ``True``.
        Set ``False`` if another SDK in your stack already handles this.
    strict_email_scrub:
        When ``True``, email addresses are auto-redacted from event text
        and meta. Off by default because most apps want emails for support
        / debugging; turn on for GDPR-strict deployments where emails are
        considered PII that must not leave the host.
    sample_rate:
        Keep-probability for ERROR events (``ok=False``), in ``[0.0, 1.0]``.
        Default ``1.0`` keeps every error. Lower it to shed load under an error
        storm so the buffer / backend hourly quota isn't overwhelmed.
    success_sample_rate:
        Keep-probability for non-error events (``ok=True`` — ``track`` /
        ``event``), in ``[0.0, 1.0]``. Default ``1.0``. These are usually the
        high-volume ones, so this is the knob to turn down first.
    buffer_size, flush_interval, request_timeout, http_transport:
        Transport tuning. Defaults match the Swift SDK so dashboards behave
        consistently across platforms.
    """
    global _active_transport

    # Validate fail-fast BEFORE any side effect (a bad rate is a config error,
    # not a runtime condition).
    for _name, _rate in (("sample_rate", sample_rate), ("success_sample_rate", success_sample_rate)):
        if not (0.0 <= _rate <= 1.0):
            raise ValueError(f"{_name} must be between 0.0 and 1.0, got {_rate!r}")

    if _active_transport is not None:
        _log.warning("monitor.init called twice; closing previous transport")
        close()

    # Resolve config: prefer values from an existing Onelo client.
    if onelo is not None:
        resolved_key = onelo._key  # noqa: SLF001 — internal but stable
        resolved_url = onelo._api_url  # noqa: SLF001
    else:
        if not publishable_key:
            raise ValueError(
                "monitor.init() requires `publishable_key` (or pass an existing "
                "`onelo=Onelo(...)` instance)."
            )
        # No default api_url on purpose — see the same note in Onelo.__init__.
        # The host differs per environment, so a hardcoded fallback is wrong
        # somewhere and fails as a confusing network error rather than a clear
        # config error.
        if not api_url:
            raise ValueError(
                "monitor.init() requires `api_url` when called with "
                "`publishable_key` — e.g. api_url='https://api.onelo.tools' "
                "(or pass an existing `onelo=Onelo(...)` instance, which "
                "carries it)."
            )
        resolved_key = publishable_key
        resolved_url = api_url

    # Populate the global scope with deploy-level context. The isolation
    # scope (per-request) and current scope (per-span) inherit these.
    g = get_global_scope()
    if release is None:
        release = os.environ.get("ONELO_RELEASE") or os.environ.get("GIT_COMMIT_SHA")
    if environment is None:
        environment = os.environ.get("ONELO_ENVIRONMENT") or os.environ.get("ENVIRONMENT")
    if server_name is None:
        server_name = os.environ.get("HOSTNAME") or os.environ.get("POD_NAME")
    if release:
        g.set_tag("release", release)
    if environment:
        g.set_tag("environment", environment)
    if server_name:
        g.set_tag("server_name", server_name)
    # The tags above land in meta.tags.*, which the backend's Feature-Health
    # Release/Environment aggregates do NOT read — it reads meta.app.version and
    # meta.environment (backend/app/routes/sdk_monitor.py _release_dim / _dim).
    # Stamp those exact paths too, or those dashboard filters stay empty for
    # every Python event despite the values being sent.
    _capture.set_deploy_context(release, environment, server_name)
    _capture.set_sample_rates(sample_rate, success_sample_rate)

    # Spin up the transport.
    transport = MonitorTransport(
        api_url=resolved_url,
        publishable_key=resolved_key,
        buffer_size=buffer_size,
        flush_interval=flush_interval,
        request_timeout=request_timeout,
        http_transport=http_transport,
    )
    transport.start()
    _capture.set_transport(transport.send)
    _active_transport = transport

    # Rebuild the transport thread in forked workers (gunicorn/uwsgi preload,
    # multiprocessing). Registered once per process; the hook reads the current
    # _active_transport so a later re-init is honoured.
    global _fork_hook_registered
    if not _fork_hook_registered and hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_child)
        _fork_hook_registered = True

    # Server-side analogue of Swift's persistent install ID: one UUID per
    # process, stamped onto every event whose ``session_id`` is empty.
    # Combined with ``release`` + ``server_name`` tags this lets the
    # dashboard group events by worker / pod across many requests.
    _capture.set_session_id(uuid.uuid4().hex)

    # Opt-in strict email scrubbing.
    from onelo.monitor._scrub import set_strict_email_scrub, configure_sensitive_keys
    set_strict_email_scrub(strict_email_scrub)
    # App-declared extra sensitive header/key names (e.g. x-anthropic-key).
    configure_sensitive_keys(sensitive_headers)

    # Auto-attach active feature flags to every event when an Onelo client
    # is available. This is the cross-platform "killer feature" — error
    # events ship with flag state, enabling flag↔error correlation in the
    # dashboard without any extra integration work.
    if onelo is not None:
        cache = getattr(onelo, "_cache", None)
        if cache is not None and hasattr(cache, "snapshot"):
            _capture.set_flag_provider(cache.snapshot)

    if install_excepthook:
        _excepthook.install()

    # Unconditional baseline event — fixes the "auth-gated app emits ZERO
    # monitor events on cold start" bug: every developer-instrumented
    # track()/event() call can sit downstream of a gate that never fires
    # before the first request, leaving the dashboard looking uninitialised
    # even though instrumentation is correct. Mirrors the JS SDK's
    # `session_opened` (packages/onelo-js/src/monitor/monitor.ts), emitted
    # once construction/init completes and before any application code runs.
    #
    # Deliberately NOT re-emitted from `_after_fork_child` / reinit_after_fork:
    # "session" here means the process's monitor.init() lifetime, not each
    # transport thread. init() runs once in the parent (the common gunicorn/
    # uwsgi preload pattern); a fork only rebuilds the dead transport thread
    # inherited by the child, it isn't a second init(). Emitting again per
    # forked worker would produce N duplicate session_opened rows per process
    # tree instead of the one baseline signal this event exists to provide.
    _capture._emit_session_opened()  # noqa: SLF001 — internal, same module family


def close() -> None:
    """Tear down the monitor — drain buffer, stop transport, restore hooks.

    Idempotent. Called automatically on interpreter shutdown via the
    transport's ``atexit`` hook for graceful exits; user code can also call
    it explicitly (e.g. between integration tests).
    """
    global _active_transport

    _capture.set_transport(None)
    _capture.set_flag_provider(None)
    _capture.set_session_id(None)
    _capture.set_deploy_context(None, None, None)
    _capture.set_sample_rates(1.0, 1.0)
    from onelo.monitor._scrub import set_strict_email_scrub
    set_strict_email_scrub(False)
    _excepthook.uninstall()

    if _active_transport is not None:
        _active_transport.stop()
        _active_transport = None


def flush(*, timeout: float = 2.0) -> None:
    """Synchronously drain the in-memory buffer.

    Useful at the end of a CLI command, between batch jobs, or before
    ``sys.exit``. ``timeout`` caps total wait time.
    """
    if _active_transport is not None:
        _active_transport.flush(timeout=timeout)


def is_initialised() -> bool:
    return _active_transport is not None


def _get_active_transport() -> MonitorTransport | None:
    """Internal — used by integrations + tests. Public API is ``flush``."""
    return _active_transport


__all__ = ["close", "flush", "init", "is_initialised"]
