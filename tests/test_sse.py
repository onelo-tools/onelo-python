"""Tests for SSEConsumer — verifies event handling and reconnect behavior."""
import asyncio
import threading

import httpx
import pytest

from onelo._cache import ThreadSafeCache
from onelo._sse import SSEConsumer


def _sse_response(events: list[tuple[str, str]]) -> httpx.Response:
    """Build an SSE-formatted response body from (event_type, json_data) pairs."""
    body = "".join(f"event: {evt}\ndata: {data}\n\n" for evt, data in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=body,
    )


@pytest.mark.asyncio
async def test_connected_event_applies_snapshot():
    cache = ThreadSafeCache()
    first_event = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            ("connected", '{"config_version": 3, "features": {"chat": {"status": "enabled"}}}'),
        ])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=first_event,
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],  # large so test stops cleanly after one cycle
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)  # let one connection happen
    consumer.stop()
    await task

    assert cache.config_version == 3
    assert cache.get("chat") == "enabled"
    assert first_event.is_set()


@pytest.mark.asyncio
async def test_features_updated_event_applies_snapshot():
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=1)

    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            ("features_updated", '{"config_version": 2, "features": {"chat": {"status": "hidden"}}}'),
        ])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    assert cache.config_version == 2
    assert cache.get("chat") == "hidden"


@pytest.mark.asyncio
async def test_up_to_date_event_signals_first_event_without_cache_update():
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=5)
    first_event = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            ("up_to_date", '{"config_version": 5}'),
        ])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=first_event,
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    assert cache.config_version == 5  # untouched
    assert cache.get("chat") == "enabled"  # untouched
    assert first_event.is_set()  # signaled


@pytest.mark.asyncio
async def test_unknown_events_are_ignored():
    cache = ThreadSafeCache()

    async def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            ("future_event_type", '{"some": "payload"}'),
            ("connected", '{"config_version": 1, "features": {"x": {"status": "enabled"}}}'),
        ])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    # Connected event still processed despite the unknown earlier event
    assert cache.get("x") == "enabled"


@pytest.mark.asyncio
async def test_user_id_in_query_params():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["userId"] = request.url.params.get("userId")
        captured["since_version"] = request.url.params.get("since_version")
        return _sse_response([("up_to_date", '{"config_version": 0}')])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: "user-99",
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    assert captured["userId"] == "user-99"
    assert captured["since_version"] == "0"


@pytest.mark.asyncio
async def test_reconnect_after_network_error():
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("simulated")
        return _sse_response([("connected", '{"config_version": 1, "features": {}}')])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[0.05],  # very fast retry for test
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.3)
    consumer.stop()
    await task

    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_401_is_terminal_no_reconnect_storm():
    """A 401/403 (dead/revoked key) must stop the consumer, not retry forever.

    Regression guard for the 2026-07 self-monitoring outage: dogfooding on a
    dead key hammered /features/stream with 401 in an infinite reconnect loop.
    The handler counts calls; with the terminal-auth fix it is hit exactly once.
    """
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(401, json={"error": "invalid_key"})

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_dead",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[0.05],  # fast — a buggy retry loop would rack up calls
    )

    task = asyncio.create_task(consumer.run())
    # Well past several backoff windows: a retry loop would call the handler
    # many times; the terminal path calls it once then returns.
    await asyncio.wait_for(task, timeout=2.0)

    assert call_count["n"] == 1, f"expected exactly 1 call (terminal), got {call_count['n']}"


@pytest.mark.asyncio
async def test_403_is_terminal():
    """403 (revoked/forbidden) is terminal too."""
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(403, json={"error": "forbidden"})

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_revoked",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[0.05],
    )

    await asyncio.wait_for(asyncio.create_task(consumer.run()), timeout=2.0)
    assert call_count["n"] == 1
