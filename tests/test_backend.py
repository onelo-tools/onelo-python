"""Tests for BackendThread — daemon thread + asyncio loop wiring."""
import threading

import httpx
import pytest

from onelo._backend import BackendThread
from onelo._cache import ThreadSafeCache


def _sse_response(events):
    body = "".join(f"event: {evt}\ndata: {data}\n\n" for evt, data in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def test_start_and_stop_cleanly():
    cache = ThreadSafeCache()

    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([("up_to_date", '{"config_version": 0}')])

    bt = BackendThread(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        strategy="sse",
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
    )
    bt.start()
    assert bt.first_event.wait(timeout=2.0)  # event arrives
    bt.stop(timeout=2.0)
    # Thread is no longer alive
    assert not bt.is_alive()


def test_first_event_blocks_until_consumer_signals():
    cache = ThreadSafeCache()
    delay = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        # Pause before responding to test the wait
        delay.wait(timeout=1.0)
        return _sse_response([("up_to_date", '{"config_version": 0}')])

    bt = BackendThread(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        strategy="sse",
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
    )
    bt.start()
    # Before delay is released, first_event should NOT be set
    assert not bt.first_event.is_set()
    delay.set()
    assert bt.first_event.wait(timeout=2.0)
    bt.stop(timeout=2.0)


def test_polling_strategy_path():
    """Verify the BackendThread wires PollingConsumer when strategy='polling'."""
    cache = ThreadSafeCache()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "/poll" in str(request.url)
        return httpx.Response(200, json={
            "config_version": 1,
            "features": {"x": {"status": "enabled"}},
        })

    bt = BackendThread(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        strategy="polling",
        poll_interval=0.05,
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
    )
    bt.start()
    assert bt.first_event.wait(timeout=2.0)
    assert cache.get("x") == "enabled"
    bt.stop(timeout=2.0)


def test_stop_is_idempotent():
    """Calling stop() twice must not raise."""
    cache = ThreadSafeCache()

    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([("up_to_date", '{"config_version": 0}')])

    bt = BackendThread(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        strategy="sse",
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
    )
    bt.start()
    bt.first_event.wait(timeout=2.0)
    bt.stop(timeout=2.0)
    bt.stop(timeout=2.0)  # second call is no-op, no exception

