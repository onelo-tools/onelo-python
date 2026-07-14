"""Polling consumer — opt-in fallback when SSE is unwanted/unavailable.

Periodically GETs /api/sdk/features/poll?since_version=N and updates the
cache. Backend returns 304 when nothing changed (cheap), 200 with new
snapshot when something did. Network errors are logged and retried on the
next interval — never propagate to the user thread.
"""
import asyncio
import logging
import threading
from collections.abc import Callable

import httpx

from onelo._cache import ThreadSafeCache
from onelo._http import create_async_client


logger = logging.getLogger("onelo")


class PollingConsumer:
    """Async loop that polls features/poll and updates cache."""

    def __init__(
        self,
        api_url: str,
        publishable_key: str,
        cache: ThreadSafeCache,
        first_event: threading.Event,
        get_user_id: Callable[[], str | None],
        poll_interval: float = 30.0,
        request_timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        feature_environment: str | None = None,
        on_discovery_requested: Callable[[], None] | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._key = publishable_key
        self._cache = cache
        self._first_event = first_event
        self._get_user_id = get_user_id
        self._interval = poll_interval
        self._feature_environment = feature_environment
        self._on_discovery_requested = on_discovery_requested
        self._http = create_async_client(timeout=request_timeout, transport=transport)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the loop to exit. Safe to call from any thread."""
        # asyncio.Event.set is safe across threads in CPython 3.10+ when the
        # loop hosting the event is running. The backend thread owns this loop.
        self._stop.set()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                await self._poll_once()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass  # interval elapsed, loop again
        finally:
            await self._http.aclose()

    async def _poll_once(self) -> None:
        params: dict[str, str] = {
            "key": self._key,
            "since_version": str(self._cache.config_version),
        }
        if self._feature_environment is not None:
            params["environment"] = self._feature_environment
        user_id = self._get_user_id()
        if user_id is not None:
            params["userId"] = user_id

        try:
            resp = await self._http.get(f"{self._api_url}/api/sdk/features/poll", params=params)
        except httpx.HTTPError as exc:
            logger.debug("poll request failed: %s", exc)
            return  # try again next interval

        # 304 = cache up-to-date; still counts as first contact for ready()
        if resp.status_code in (200, 304):
            self._first_event.set()

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning("poll response not JSON: %s", exc)
                return
            # A valid-JSON-but-non-object body (a stray proxy/captive-portal
            # array or string) must not crash the loop — the old subscript path
            # survived it via TypeError; keep that resilience explicitly.
            if not isinstance(data, dict):
                logger.warning("poll response not a JSON object: %s", type(data).__name__)
                return
            # Version short-circuit: when since_version matches, the backend
            # returns {config_version, up_to_date: true, discovery_requested}
            # with NO `features` payload (sdk_features_sse.py). The cache is
            # already current — treat it as a no-op, not a malformed response.
            if data.get("up_to_date") or "features" not in data:
                self._maybe_request_discovery(data)
                return
            try:
                # Keep the full per-feature state dicts (status + upsell
                # metadata); the cache normalises and copies each value.
                self._cache.replace_all(data["features"], data["config_version"])
            except (KeyError, TypeError, AttributeError) as exc:
                logger.warning("poll response malformed: %s", exc)
                return
            self._maybe_request_discovery(data)
        elif resp.status_code == 401:
            logger.error("poll: 401 Unauthorized — check publishable_key")
        elif 500 <= resp.status_code < 600:
            logger.warning("poll: %s server error", resp.status_code)

    def _maybe_request_discovery(self, data: dict) -> None:
        """Fire the discovery callback when the backend flags a pending
        discovery request (dashboard 'Discover Features' click). Parity with
        the SSE `discovery_requested` event — without this, the dashboard's
        Discover button is dead for polling-strategy clients."""
        if data.get("discovery_requested") and self._on_discovery_requested is not None:
            try:
                self._on_discovery_requested()
            except Exception:
                logger.exception("discovery_requested handler failed")
