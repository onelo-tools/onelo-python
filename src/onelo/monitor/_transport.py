"""HTTP transport for monitor events — batches, flushes on a timer, retries
with exponential backoff, drains on process exit.

Design notes:

  - **Buffer**: ``deque(maxlen=N)`` so memory is bounded even if the network
    is unreachable for hours. Default 200 (matches Swift SDK). Old events
    are dropped silently — monitoring must never OOM the host.

  - **Flushing**: a background daemon thread runs an asyncio loop and flushes
    every 15s OR immediately when an error event arrives. Same cadence as
    the Swift SDK so dashboards behave consistently.

  - **Retries**: on 5xx / network error, exponential backoff up to 3 attempts.
    On 4xx (auth, validation, quota) we drop the batch — retrying won't help
    and would just re-trigger the same response.

  - **Quota awareness**: the backend returns ``X-Onelo-Quota-Remaining`` and
    ``Retry-After`` on 429. We honour Retry-After by sleeping the loop and
    drop subsequent batches with a debug log until the window resets.

  - **Atexit**: a hook drains the buffer on interpreter shutdown so events
    queued in the last 15s aren't lost on graceful exit.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import httpx

from onelo._http import create_async_client
from onelo.monitor._types import MonitorEvent


_log = logging.getLogger("onelo.monitor.transport")


DEFAULT_BUFFER_SIZE = 200
DEFAULT_FLUSH_INTERVAL = 15.0
DEFAULT_REQUEST_TIMEOUT = 5.0
MAX_RETRY_ATTEMPTS = 3
"""Tuned for indie dev profile — most failures are transient (DNS, brief 502
during deploy). Retrying more than 3× rarely helps and just delays drop."""

_QUOTA_LOW_WATERMARK = 50
"""Log an info line when the backend's remaining hourly quota drops to/below
this, so an operator sees throttling coming before events start dropping."""

_QUOTA_EXHAUSTED_HOLDOFF = 300.0
"""When the backend reports 0 remaining quota, hold off this long before the
next attempt instead of hammering it into a 429 every flush. Shorter than the
backend's ``Retry-After: 3600`` so we re-probe (and pick up a reset or the real
Retry-After) rather than guessing the full hour."""


class MonitorTransport:
    """Batches events and POSTs them to /api/sdk/monitor/events/batch.

    One instance per process (created by ``monitor.init``). The transport
    spawns its own daemon thread + asyncio loop so user code (sync or async)
    can call ``send`` without awaiting anything.
    """

    def __init__(
        self,
        api_url: str,
        publishable_key: str,
        *,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._publishable_key = publishable_key
        self._flush_interval = flush_interval
        self._request_timeout = request_timeout
        self._http_transport = http_transport

        # ── shared mutable state, guarded by _lock ─────────────────────
        self._lock = threading.Lock()
        self._buffer: deque[MonitorEvent] = deque(maxlen=buffer_size)
        self._dropped = 0  # how many events were evicted because buffer was full
        self._dropped_reported = 0  # high-water mark already surfaced via a log
        self._retry_after_until: float = 0.0  # epoch seconds; while non-zero we hold off
        self._quota_remaining: int | None = None  # last X-Onelo-Quota-Remaining, None=unlimited/unknown

        # ── thread + loop ──────────────────────────────────────────────
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="onelo-monitor-transport",
            daemon=True,
        )
        self._client: httpx.AsyncClient | None = None
        self._flush_task: asyncio.Task | None = None
        # ``asyncio.Event`` is *not* directly thread-safe and must be created
        # inside the loop that awaits it. Until ``_loop_main`` runs, we can
        # only signal the wake-up via ``call_soon_threadsafe`` *which itself*
        # checks ``_loop`` is running. That guard is enough — the previous
        # placeholder of ``self._wake = asyncio.Event`` (the class object)
        # was a footgun: an isinstance check could legitimately fail and
        # silently drop wakes during the start-up window.
        self._wake: asyncio.Event | None = None
        # Serialises _do_flush so an error-triggered wake flush and an explicit
        # monitor.flush() can't drain the buffer concurrently. Without it, the
        # wake flush grabs the batch and starts an in-flight POST while the
        # flush() path sees an already-empty buffer and signals `done`
        # immediately — so flush() returns BEFORE the error event is actually
        # delivered (lost on a process that exits right after). Created inside
        # the loop, like _wake.
        self._flush_lock: asyncio.Lock | None = None

        # ── atexit: best-effort drain on graceful shutdown ─────────────
        self._atexit_registered = False

    # ─── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()
        if not self._atexit_registered:
            atexit.register(self._atexit_drain)
            self._atexit_registered = True
        # Wait for the loop to be ready so callers can push immediately.
        self._loop_ready.wait(timeout=2.0)

    def reinit_after_fork(self) -> None:
        """Rebuild the transport thread + asyncio loop in a forked child.

        A ``fork()`` (gunicorn/uwsgi preload, ``multiprocessing``) does NOT copy
        the parent's daemon thread — the child inherits a ``MonitorTransport``
        whose loop thread is dead, so every event it buffers is silently never
        delivered (the exact failure the features client guards against in
        ``_client._after_fork``). ``monitor.init`` wires this via
        ``os.register_at_fork`` so each worker gets a live transport.

        Buffered events carry over. The sync primitives are recreated: the child
        is single-threaded at the moment of fork, so replacing the lock is safe
        even if the parent happened to hold it, and the inherited httpx client /
        loop belong to the dead parent loop and must be dropped.
        """
        self._lock = threading.Lock()
        # Start the child with a FRESH buffer. fork() copies the parent's deque,
        # but those events belong to the parent (the gunicorn master) which still
        # delivers them — carrying the copy into every worker would re-POST the
        # same startup events N times. The child only sends what it captures
        # itself after the fork.
        self._buffer = deque(maxlen=self._buffer.maxlen)
        self._dropped = 0
        self._dropped_reported = 0
        self._retry_after_until = 0.0
        self._quota_remaining = None
        self._loop = None
        self._loop_ready = threading.Event()
        self._stopping = threading.Event()
        self._wake = None
        self._flush_lock = None
        self._flush_task = None
        self._client = None
        # atexit is per-process; the child must register its own drain hook.
        self._atexit_registered = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name="onelo-monitor-transport",
            daemon=True,
        )
        self.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        """Drain the buffer, close the HTTP client, stop the thread.

        Idempotent. ``timeout`` caps total time spent — anything still
        buffered after that is dropped.
        """
        if not self._thread.is_alive():
            return
        self._stopping.set()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._wake_event_set)
        self._thread.join(timeout=timeout)

    # ─── public API ──────────────────────────────────────────────────────

    def send(self, event: MonitorEvent) -> None:
        """Enqueue an event. Always returns immediately, never raises."""
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                self._dropped += 1
            self._buffer.append(event)
            should_wake = not event.ok or event.source == "global_error"

        if should_wake and self._loop is not None and self._loop.is_running():
            # Errors flush immediately — wake the loop without waiting for
            # the periodic timer.
            try:
                self._loop.call_soon_threadsafe(self._wake_event_set)
            except RuntimeError:
                # Loop closed mid-call — drop silently.
                pass

    def flush(self, *, timeout: float = 2.0) -> None:
        """Synchronously flush the buffer. Blocks up to ``timeout`` seconds.

        Used by ``monitor.flush()`` (rare, dev-driven) and by atexit. Not on
        the hot path of ``send``.
        """
        if self._loop is None or not self._loop.is_running():
            return
        done = threading.Event()

        def _runner() -> None:
            asyncio.ensure_future(self._do_flush_and_signal(done))

        try:
            self._loop.call_soon_threadsafe(_runner)
        except RuntimeError:
            return
        done.wait(timeout=timeout)

    @property
    def dropped_count(self) -> int:
        """Number of events dropped because the in-memory buffer was full."""
        with self._lock:
            return self._dropped

    @property
    def buffer_size(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ─── thread / loop internals ─────────────────────────────────────────

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._loop_main())
        except Exception:  # noqa: BLE001 — last-resort guard
            _log.exception("monitor transport loop crashed")
        finally:
            loop.close()
            self._loop = None

    async def _loop_main(self) -> None:
        # `Event` and `Lock` must be created inside the loop that awaits them.
        wake = asyncio.Event()
        self._wake = wake
        self._flush_lock = asyncio.Lock()
        self._client = create_async_client(
            timeout=self._request_timeout,
            transport=self._http_transport,
        )
        self._loop_ready.set()
        try:
            while not self._stopping.is_set():
                # Wait for either the periodic timer or an explicit wake.
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self._flush_interval)
                except asyncio.TimeoutError:
                    pass
                wake.clear()

                if self._stopping.is_set():
                    break
                await self._do_flush()
                self._maybe_report_drops()

            # One last drain on shutdown.
            await self._do_flush()
            self._maybe_report_drops()
        finally:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    def _wake_event_set(self) -> None:
        """Loop-thread callback to set the wake event safely.

        Runs only after ``call_soon_threadsafe`` has dispatched onto the
        loop, by which point ``_loop_main`` has already assigned the real
        ``asyncio.Event``. The ``None`` guard is defence in depth — in
        practice this branch is always live.
        """
        wake = self._wake
        if wake is not None:
            wake.set()

    async def _do_flush(self) -> None:
        # Honour any active Retry-After window.
        now = asyncio.get_running_loop().time()
        if self._retry_after_until and now < self._retry_after_until:
            return

        # Serialise the drain+send. If a concurrent flush (e.g. the error-wake
        # path) is mid-send, an explicit flush() must WAIT for it here rather
        # than observe the already-emptied buffer and return "done" while that
        # send is still in flight. Holding the lock across the await means that
        # when flush() finally drains an empty buffer, every earlier event has
        # truly been delivered. The lock is created in _loop_main; guard for
        # the (unreachable) pre-loop window defensively.
        lock = self._flush_lock
        if lock is None:
            return
        async with lock:
            with self._lock:
                if not self._buffer:
                    return
                batch = list(self._buffer)
                self._buffer.clear()

            await self._send_batch_with_retry(batch)

    async def _do_flush_and_signal(self, done: threading.Event) -> None:
        try:
            await self._do_flush()
        finally:
            done.set()

    async def _send_batch_with_retry(self, batch: list[MonitorEvent]) -> None:
        if self._client is None or not batch:
            return

        backoff = 0.5
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            try:
                ok = await self._send_once(batch)
                if ok:
                    return
            except Exception:  # noqa: BLE001
                _log.debug("monitor batch send raised on attempt %d", attempt, exc_info=True)
            if attempt < MAX_RETRY_ATTEMPTS:
                await asyncio.sleep(backoff)
                backoff *= 2
        _log.debug("dropping batch of %d events after %d attempts", len(batch), MAX_RETRY_ATTEMPTS)

    async def _send_once(self, batch: list[MonitorEvent]) -> bool:
        """Returns True if the batch is done (success OR non-retryable failure)."""
        assert self._client is not None
        url = f"{self._api_url}/api/sdk/monitor/events/batch"
        payload = {
            "publishableKey": self._publishable_key,
            "events": [_event_to_wire(e) for e in batch],
        }
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.RequestError:
            # Network problem — caller will retry.
            return False

        if 200 <= resp.status_code < 300:
            self._note_quota(resp.headers.get("X-Onelo-Quota-Remaining"))
            return True

        if resp.status_code == 429:
            # Quota exceeded; honour Retry-After and stop the loop until then.
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after > 0:
                self._retry_after_until = (
                    asyncio.get_running_loop().time() + retry_after
                )
            return True  # don't retry the same batch — quota won't move

        if 400 <= resp.status_code < 500:
            # 401 invalid key, 403 forbidden, 422 validation — retrying is pointless.
            _log.debug(
                "monitor batch dropped due to %d response", resp.status_code,
            )
            return True

        # 5xx — fall through, caller retries.
        return False

    def _note_quota(self, remaining_header: str | None) -> None:
        """Read ``X-Onelo-Quota-Remaining`` (sent on every 204) so the client
        can throttle itself before hitting a 429 + hour-long drop window.

        - ``"unlimited"`` / missing → clear the tracker, nothing to do.
        - ``0`` → preemptively hold off (the next batch WOULD 429); modest window
          so we re-probe rather than block for the full hour on a guess.
        - low but > 0 → info log so the operator sees throttling coming.
        Called on the loop thread from ``_send_once``.
        """
        if remaining_header is None:
            return
        header = remaining_header.strip()
        if header.lower() == "unlimited":
            self._quota_remaining = None
            return
        try:
            remaining = int(header)
        except ValueError:
            return
        self._quota_remaining = remaining
        if remaining <= 0:
            hold = asyncio.get_running_loop().time() + _QUOTA_EXHAUSTED_HOLDOFF
            self._retry_after_until = max(self._retry_after_until, hold)
            _log.warning(
                "onelo.monitor: event quota exhausted; holding off ~%ds before retry",
                int(_QUOTA_EXHAUSTED_HOLDOFF),
            )
        elif remaining <= _QUOTA_LOW_WATERMARK:
            _log.info("onelo.monitor: event quota low — %d remaining this hour", remaining)

    def _maybe_report_drops(self) -> None:
        """Surface silently-dropped events (buffer overflowed) exactly once per
        newly-dropped batch. Without this, ``dropped_count`` climbs invisibly and
        an operator has no signal that monitoring itself is losing data — during
        an incident the DROPPED events are often the earliest, root-cause ones."""
        with self._lock:
            dropped = self._dropped
            newly = dropped - self._dropped_reported
            self._dropped_reported = dropped
            maxlen = self._buffer.maxlen
        if newly > 0:
            _log.warning(
                "onelo.monitor: dropped %d event(s) — buffer full (max %s). "
                "Raise buffer_size or lower volume via sample_rate.",
                newly, maxlen,
            )

    @property
    def quota_remaining(self) -> int | None:
        """Last-seen remaining hourly quota (None = unlimited/unknown)."""
        return self._quota_remaining

    # ─── atexit ──────────────────────────────────────────────────────────

    def _atexit_drain(self) -> None:
        # Two-step drain to maximise the chance of catching the last events
        # on interpreter shutdown (e.g. K8s rolling restart with 100ms of
        # in-flight events):
        #
        #   1) ``flush(timeout=1.5)`` — async path, lets the loop send the
        #      batch using its existing httpx client. Works in the common
        #      case where the loop is idle.
        #   2) Fallback synchronous send — if step 1 didn't drain (loop
        #      was wedged or ``call_soon_threadsafe`` missed its window
        #      before atexit returns), pull whatever's left in the buffer
        #      under the lock and POST it directly with stdlib ``urllib``.
        #      This avoids depending on the daemon thread being alive.
        try:
            self.flush(timeout=1.5)
        except Exception:  # noqa: BLE001
            pass

        # Sync fallback: only fires if events are still buffered. Bounded by
        # a 1s socket timeout so atexit doesn't hang the interpreter.
        try:
            with self._lock:
                if not self._buffer:
                    return
                events = list(self._buffer)
                self._buffer.clear()
            self._sync_post(events, timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        # Don't call stop() — atexit running while threads are mid-shutdown
        # can deadlock. The daemon thread will be reaped by the runtime.

    def _sync_post(self, events: list[MonitorEvent], *, timeout: float) -> None:
        """Last-resort synchronous batch send used by ``_atexit_drain``.

        Uses stdlib ``urllib`` instead of ``httpx`` because we can't rely
        on the asyncio loop being alive at this point. Quietly best-effort
        — failures are swallowed (the events were going to be lost either
        way once the process exits).
        """
        import json as _json
        from urllib import request as _urlreq
        from urllib.error import URLError

        url = f"{self._api_url}/api/sdk/monitor/events/batch"
        payload = {
            "publishableKey": self._publishable_key,
            "events": [_event_to_wire(e) for e in events],
        }
        body = _json.dumps(payload).encode("utf-8")
        req = _urlreq.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "onelo-python-atexit",
            },
        )
        try:
            with _urlreq.urlopen(req, timeout=timeout):
                pass
        except (URLError, OSError, TimeoutError):
            pass


# ─── helpers ────────────────────────────────────────────────────────────────


def _event_to_wire(event: MonitorEvent) -> dict[str, Any]:
    """Serialise a MonitorEvent into the JSON shape the backend expects.

    Field names mirror `backend/app/routes/sdk_monitor.py` and the Swift
    SDK exactly so the same ingest endpoint accepts both.
    """
    out: dict[str, Any] = {
        "featureName": event.feature_name,
        "ok": event.ok,
        "platform": event.platform,
        "source": event.source,
        # Real time the event happened (captured at MonitorEvent creation), so a
        # batched / retried / atexit-drained event isn't mis-dated to ingest time.
        # Backend stores it as a display-only `event_ts`, clamped, never used for
        # windowing/quota. Absent → backend falls back to its ingest now().
        "ts": datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat(),
    }
    if event.duration_ms is not None:
        out["durationMs"] = event.duration_ms
    if event.error is not None:
        out["error"] = event.error
    if event.user_id is not None:
        out["userId"] = event.user_id
    if event.session_id is not None:
        out["sessionId"] = event.session_id
    if event.meta:
        out["meta"] = event.meta
    return out


_MAX_RETRY_AFTER_SECONDS = 3600.0
"""Hard ceiling on how long we'll honour ``Retry-After``. Without a cap a
malicious or MITM'd backend could send ``Retry-After: 99999999`` and wedge
the client for years; with one we silently fall back to retry-anyway after
an hour. RFC 7231 doesn't require a max, so this is purely defensive."""


def _parse_retry_after(value: str | None) -> float:
    """Parse the Retry-After header. Returns seconds, clamped to
    ``[0, _MAX_RETRY_AFTER_SECONDS]``. 0 if missing/invalid.

    Per RFC 7231 the value is either a number of seconds or an HTTP-date.
    For monitor we only support the seconds form — the date form is rare
    and our backend doesn't use it.
    """
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        return 0.0
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


# Sink factory for ``capture._sink`` registration.
def make_sink(transport: MonitorTransport) -> Callable[[MonitorEvent], None]:
    """Returns a callable suitable for ``set_transport`` in ``_capture.py``."""
    return transport.send


__all__ = [
    "DEFAULT_BUFFER_SIZE",
    "DEFAULT_FLUSH_INTERVAL",
    "DEFAULT_REQUEST_TIMEOUT",
    "MAX_RETRY_ATTEMPTS",
    "MonitorTransport",
    "make_sink",
]
