"""SSE consumer — the default real-time strategy.

Opens a long-lived GET on /api/sdk/features/stream, parses Server-Sent
Events, updates the cache on 'connected' and 'features_updated' events.
Reconnects with exponential backoff on disconnect or error.

Forward-compatible: unknown event types are ignored, so adding new event
kinds on the backend doesn't break older SDKs.

Zombie detection: a healthcheck task watches `_last_event_at`. If no event
(including the server's `:` heartbeat comments) arrives within
`STALE_THRESHOLD_SECONDS`, we force a reconnect. NAT/proxy idle kills
silently drop the TCP connection without sending RST; without this
watchdog the consumer would hang forever waiting for bytes that will
never come. Mirrors Swift's `_startHealthcheck` / `_checkSSEHealth`.
"""
import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Callable

import httpx
import httpx_sse

from onelo._cache import ThreadSafeCache
from onelo._http import create_async_client
from onelo._instance import get_instance_id
from onelo._version import __version__


logger = logging.getLogger("onelo")


class _SSETerminalAuthError(Exception):
    """The stream returned 401/403 — the key is invalid or revoked.

    Deliberately NOT a subclass of httpx.HTTPError so the run() loop's generic
    ``except httpx.HTTPError`` (transient-network) branch does not catch it. A
    dead credential never becomes valid by retrying, so this is terminal: the
    consumer logs once and stops, instead of hammering the backend with a
    reconnect storm (the 2026-07 self-monitoring-on-a-dead-key outage).
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"features stream auth failed: HTTP {status}")
        self.status = status


DEFAULT_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)

# Zombie detection knobs. Server emits a `:` heartbeat every ~30s so 90s
# equals 3 missed heartbeats — long enough to absorb a single GC pause or
# packet-loss blip but short enough that a NAT-killed socket recovers in
# under two minutes. Healthcheck polls at a third of the staleness window
# so detection latency stays bounded without burning cycles.
# Exposed at module level so tests can monkeypatch them.
STALE_THRESHOLD_SECONDS = 90.0
HEALTHCHECK_INTERVAL_SECONDS = 30.0


class SSEConsumer:
    """Async loop that holds an SSE connection and applies snapshots."""

    def __init__(
        self,
        api_url: str,
        publishable_key: str,
        cache: ThreadSafeCache,
        first_event: threading.Event,
        get_user_id: Callable[[], str | None],
        request_timeout: float = 5.0,
        backoff_seconds: tuple[float, ...] | list[float] = DEFAULT_BACKOFF_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        app_version: str | None = None,
        on_discovery_requested: Callable[[], None] | None = None,
        feature_environment: str | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._key = publishable_key
        self._feature_environment = feature_environment
        self._cache = cache
        self._first_event = first_event
        self._get_user_id = get_user_id
        self._backoff = list(backoff_seconds)
        # Streaming connection: read timeout is disabled (None) because the
        # backend sends heartbeats every ~30s and we'd otherwise raise
        # ReadTimeout between them and reconnect every `request_timeout`
        # seconds. Liveness is handled by the heartbeat-driven healthcheck
        # task below — that's the one place that decides "stream is dead,
        # reconnect", so the read-timeout path stays uninvolved.
        stream_timeout = httpx.Timeout(
            connect=request_timeout,
            read=None,
            write=request_timeout,
            pool=request_timeout,
        )
        self._http = create_async_client(timeout=stream_timeout, transport=transport)
        self._app_version = app_version
        self._on_discovery_requested = on_discovery_requested
        self._stop = asyncio.Event()
        # Set by force_reconnect() — observed by the inner consume loop so
        # an in-flight `aiter_sse()` aborts and the outer loop reconnects
        # immediately (no backoff). Also used by the healthcheck task.
        self._reconnect_signal = asyncio.Event()
        # Monotonic timestamp of the last byte we saw from the server,
        # including `:` heartbeat comments. The healthcheck task compares
        # this against STALE_THRESHOLD_SECONDS to detect zombie sockets.
        self._last_event_at: float = time.monotonic()
        self._healthcheck_task: asyncio.Task | None = None

    def stop(self) -> None:
        self._stop.set()
        # Wake the inner consume loop too — otherwise it stays blocked on
        # `aiter_sse()` until the next event/heartbeat.
        self._reconnect_signal.set()

    def force_reconnect(self) -> None:
        """Abort the current SSE connection and reconnect immediately.

        Safe to call from any thread when wrapped in `call_soon_threadsafe`
        — `asyncio.Event.set()` is sync and idempotent. Used by
        `BackendThread.signal_user_change()` so a fresh `identify()` reaches
        the backend on the very next event (without waiting for the
        natural reconnect window).
        """
        self._reconnect_signal.set()

    async def run(self) -> None:
        # Reset the staleness clock at startup so a slow first connect
        # doesn't immediately trip the watchdog.
        self._last_event_at = time.monotonic()
        self._healthcheck_task = asyncio.create_task(self._healthcheck_loop())
        attempt = 0
        try:
            while not self._stop.is_set():
                reconnect_immediate = False
                try:
                    await self._connect_and_consume()
                    attempt = 0  # clean close, reset backoff
                except _SSETerminalAuthError as exc:
                    # Dead/revoked key — retrying can never succeed. Log ONCE
                    # (loudly) and stop the consumer so we don't storm the
                    # backend with reconnects. The finally block still cleans
                    # up the healthcheck task + HTTP client.
                    logger.error(
                        "onelo features stream stopped: authentication failed "
                        "(HTTP %s). The publishable/secret key is invalid or "
                        "revoked — fix the key and re-initialise the SDK. Not "
                        "retrying (a dead credential will not become valid).",
                        exc.status,
                    )
                    return
                except httpx.HTTPError as exc:
                    logger.debug("sse connection failed: %s", exc)
                if self._stop.is_set():
                    break
                # If we exited because force_reconnect/healthcheck/identify
                # asked us to, skip the backoff entirely — the user is
                # waiting on a fresh handshake. Counter resets so a real
                # network blip that follows still gets backoff.
                if self._reconnect_signal.is_set():
                    self._reconnect_signal.clear()
                    attempt = 0
                    reconnect_immediate = True
                if reconnect_immediate:
                    continue
                # Full jitter (AWS architecture blog pattern) — picks a random
                # delay in [0, base_delay] instead of the fixed base. This is
                # the same scheme Swift's OneloFeatures uses on reconnect and
                # is the recommended approach for many-replica backend fleets:
                # if a shared dependency blips and N workers all reconnect,
                # straight exponential makes them re-collide on every retry,
                # whereas full jitter spreads them across the window so the
                # server-side reconnect storm flattens out.
                base = self._backoff[min(attempt, len(self._backoff) - 1)]
                delay = random.uniform(0, base)
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._healthcheck_task is not None:
                self._healthcheck_task.cancel()
                try:
                    await self._healthcheck_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._http.aclose()

    async def _connect_and_consume(self) -> None:
        params: dict[str, str] = {
            "key": self._key,
            "since_version": str(self._cache.config_version),
            "instance_id": get_instance_id(),
            "sdk_platform": "python",
            "sdk_version": __version__,
        }
        if self._app_version:
            params["app_version"] = self._app_version
        if self._feature_environment is not None:
            params["environment"] = self._feature_environment
        user_id = self._get_user_id()
        if user_id is not None:
            params["userId"] = user_id

        url = f"{self._api_url}/api/sdk/features/stream"
        # Reset the staleness clock on each fresh handshake — a successful
        # connect counts as "we just heard from the server".
        self._last_event_at = time.monotonic()
        async with httpx_sse.aconnect_sse(self._http, "GET", url, params=params) as event_source:
            # A 401/403 here means the key is bad — terminal, never retry
            # (checked before aiter_sse(), which would otherwise surface it as
            # a generic SSEError/HTTPError that the run() loop retries forever).
            status = event_source.response.status_code
            if status in (401, 403):
                raise _SSETerminalAuthError(status)
            iterator = event_source.aiter_sse().__aiter__()
            while True:
                if self._stop.is_set() or self._reconnect_signal.is_set():
                    break
                # Race the next SSE event against a reconnect signal. Without
                # this race a force_reconnect() (identify / healthcheck) would
                # block until the server sent the next event/heartbeat —
                # exactly the case we're trying to handle on a zombie socket.
                next_event_task = asyncio.create_task(iterator.__anext__())
                signal_task = asyncio.create_task(self._reconnect_signal.wait())
                done, pending = await asyncio.wait(
                    {next_event_task, signal_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    # Drain the cancellation so httpx_sse's async generator
                    # can run its own cleanup (otherwise we get a noisy
                    # "coroutine 'aclose' was never awaited" warning).
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                if signal_task in done:
                    # force_reconnect() / healthcheck / stop() triggered us.
                    # Don't await the cancelled next_event_task — its httpx
                    # generator may swallow the cancellation on some httpx
                    # versions; the surrounding `async with` cleans up the
                    # underlying stream on exit.
                    break
                try:
                    sse_event = next_event_task.result()
                except StopAsyncIteration:
                    # Server closed the stream cleanly — outer loop will
                    # reconnect (with backoff unless a signal is set).
                    return
                # Any byte from the server — data event OR `:` heartbeat
                # comment that httpx_sse surfaces as an event with empty
                # `event` field — counts as "stream is alive". This is the
                # signal the healthcheck task watches.
                self._last_event_at = time.monotonic()
                self._handle_event(sse_event.event, sse_event.data)

    def _handle_event(self, event_type: str, data: str) -> None:
        if not event_type:
            # Heartbeat / comment line. `_last_event_at` already bumped by
            # the caller; nothing else to do.
            return
        if event_type == "up_to_date":
            self._first_event.set()
            return
        if event_type in ("connected", "features_updated"):
            try:
                payload = json.loads(data)
                # Pass the full per-feature state dicts (status + upsell
                # metadata) straight to the cache; it normalises and copies.
                self._cache.replace_all(payload["features"], payload["config_version"])
                self._first_event.set()
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                logger.warning("malformed %s event: %s", event_type, exc)
            return
        if event_type == "discovery_requested":
            # Dashboard's "Discover Features" button broadcasts this. Swift
            # SDK reacts by re-pinging its known slug set so the dashboard
            # registry picks up anything the dev added since last batch-ping.
            # Python side is read-only for live keys (the backend gates new
            # slug INSERTs to test keys only), but firing the ping anyway
            # bumps `last_seen_at` on existing rows so the dashboard's
            # presence indicators stay fresh. For test keys, it also gives
            # any features that were registered between deploys a chance to
            # land.
            if self._on_discovery_requested is not None:
                try:
                    self._on_discovery_requested()
                except Exception:
                    logger.exception("discovery_requested handler failed")
            return
        # Unknown event — forward-compat, ignore.
        logger.debug("ignoring unknown sse event: %s", event_type)

    async def _healthcheck_loop(self) -> None:
        """Zombie SSE detector. Wakes every HEALTHCHECK_INTERVAL_SECONDS and
        force-reconnects if no bytes have been seen for STALE_THRESHOLD.
        See module docstring for why this exists despite read=None timeout."""
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=HEALTHCHECK_INTERVAL_SECONDS,
                    )
                    return  # stop() was set
                except asyncio.TimeoutError:
                    pass
                elapsed = time.monotonic() - self._last_event_at
                if elapsed > STALE_THRESHOLD_SECONDS:
                    logger.debug(
                        "sse stream looks stale (no events for %.0fs) — reconnecting",
                        elapsed,
                    )
                    # Reset the clock before signalling so the next iteration
                    # waits a full window after the reconnect opens, mirroring
                    # Swift's `_checkSSEHealth`.
                    self._last_event_at = time.monotonic()
                    self.force_reconnect()
        except asyncio.CancelledError:
            return
