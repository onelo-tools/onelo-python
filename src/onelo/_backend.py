"""Background thread manager.

Spawns one daemon thread per Onelo client. The thread owns its own asyncio
event loop and runs either SSEConsumer (default) or PollingConsumer (opt-in
fallback). Communicates with the user thread via a thread-safe cache and a
threading.Event for ready() coordination.

The asyncio event loop runs inside the daemon thread, isolated from any
event loop the user code may be using (FastAPI, aiohttp). User thread reads
the cache directly — no async needed in the public API.
"""
import asyncio
import logging
import threading
from collections.abc import Callable

import httpx

from onelo._cache import ThreadSafeCache
from onelo._polling import PollingConsumer
from onelo._sse import SSEConsumer


logger = logging.getLogger("onelo")


class BackendThread:
    """Owns one daemon thread + asyncio loop running an SSE or polling consumer."""

    def __init__(
        self,
        api_url: str,
        publishable_key: str,
        cache: ThreadSafeCache,
        strategy: str,
        get_user_id: Callable[[], str | None],
        poll_interval: float = 30.0,
        request_timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        app_version: str | None = None,
        on_discovery_requested: Callable[[], None] | None = None,
        feature_environment: str | None = None,
    ) -> None:
        if strategy not in ("sse", "polling"):
            raise ValueError(f"strategy must be 'sse' or 'polling', got {strategy!r}")
        self._api_url = api_url
        self._key = publishable_key
        self._cache = cache
        self._strategy = strategy
        self._get_user_id = get_user_id
        self._poll_interval = poll_interval
        self._request_timeout = request_timeout
        self._transport = transport
        self._app_version = app_version
        self._on_discovery_requested = on_discovery_requested
        self._feature_environment = feature_environment

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer: SSEConsumer | PollingConsumer | None = None
        self._stopped = False

        # Public Event for ready() synchronization. Set by the consumer on
        # first contact with backend.
        self.first_event = threading.Event()

    def start(self) -> None:
        """Spawn the daemon thread and block until the asyncio loop is ready."""
        if self._thread is not None:
            return  # already started
        loop_ready = threading.Event()

        def thread_main() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._consumer = self._build_consumer()
            loop_ready.set()

            async def run_logged(consumer, label: str) -> None:
                try:
                    await consumer.run()
                except Exception:
                    logger.exception("%s consumer crashed", label)

            try:
                self._loop.run_until_complete(
                    run_logged(self._consumer, "background")
                )
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=thread_main, daemon=True, name="onelo-backend")
        self._thread.start()
        loop_ready.wait(timeout=5.0)

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the consumer to exit and join the thread. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        if self._loop is not None and self._consumer is not None:
            # Schedule the stop on the consumer's own event loop.
            try:
                self._loop.call_soon_threadsafe(self._consumer.stop)
            except RuntimeError:
                pass  # loop already closed
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def signal_user_change(self) -> None:
        """Tell the consumer that user_id changed — it should reconnect.

        For SSE: force-reconnect immediately so the next event uses the new
        userId. Mirrors Swift's `features._load(userId:)` which closes the
        stream task right after writing the new id. Without this, the
        backend keeps streaming features for the previous user until the
        connection naturally drops — which can take minutes on a healthy
        link and means the very first `feature()` after `identify()` may
        return the wrong tenant's state.

        For polling: no-op — the next poll cycle reads `get_user_id()`
        directly so the new id is picked up naturally within `poll_interval`.
        """
        if self._consumer is None or self._loop is None:
            return
        if isinstance(self._consumer, SSEConsumer):
            try:
                self._loop.call_soon_threadsafe(self._consumer.force_reconnect)
            except RuntimeError:
                pass  # loop already closed

    def _build_consumer(self) -> SSEConsumer | PollingConsumer:
        common = dict(
            api_url=self._api_url,
            publishable_key=self._key,
            cache=self._cache,
            first_event=self.first_event,
            get_user_id=self._get_user_id,
            request_timeout=self._request_timeout,
            transport=self._transport,
            feature_environment=self._feature_environment,
        )
        if self._strategy == "sse":
            return SSEConsumer(
                **common,
                app_version=self._app_version,
                on_discovery_requested=self._on_discovery_requested,
            )
        return PollingConsumer(
            **common,
            poll_interval=self._poll_interval,
            on_discovery_requested=self._on_discovery_requested,
        )
