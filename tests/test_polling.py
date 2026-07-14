"""Tests for PollingConsumer — verifies poll request shape and cache updates."""
import asyncio
import threading

import httpx
import pytest

from onelo._cache import ThreadSafeCache
from onelo._polling import PollingConsumer


@pytest.mark.asyncio
async def test_polling_updates_cache_on_200():
    cache = ThreadSafeCache()
    first_event = threading.Event()
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # Verify request shape on first call only — subsequent polls have
        # since_version=7 (the version we just returned), which is correct
        # but would break a strict equality assertion.
        if call_count["n"] == 1:
            assert request.url.path == "/api/sdk/features/poll"
            assert request.url.params.get("key") == "pk_test"
            assert request.url.params.get("since_version") == "0"
        return httpx.Response(200, json={
            "config_version": 7,
            "features": {"chat": {"status": "enabled"}, "voice": {"status": "hidden"}},
        })

    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=first_event,
        get_user_id=lambda: None,
        poll_interval=0.05,  # fast for test
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.1)  # let one poll happen
    consumer.stop()
    await task

    assert cache.config_version == 7
    assert cache.get("chat") == "enabled"
    assert cache.get("voice") == "hidden"
    assert first_event.is_set()


@pytest.mark.asyncio
async def test_polling_handles_304_no_change():
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=5)
    first_event = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=first_event,
        get_user_id=lambda: None,
        poll_interval=0.05,
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.1)
    consumer.stop()
    await task

    # Cache untouched
    assert cache.config_version == 5
    assert cache.get("chat") == "enabled"
    # 304 still counts as "first contact with backend" for ready()
    assert first_event.is_set()


@pytest.mark.asyncio
async def test_polling_includes_user_id_when_set():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["userId"] = request.url.params.get("userId")
        return httpx.Response(304)

    cache = ThreadSafeCache()
    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=threading.Event(),
        get_user_id=lambda: "user-42",
        poll_interval=0.05,
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.1)
    consumer.stop()
    await task

    assert captured["userId"] == "user-42"


@pytest.mark.asyncio
async def test_polling_survives_network_error():
    """Network errors must not crash the loop — keep retrying."""
    cache = ThreadSafeCache()
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("simulated network error")
        return httpx.Response(200, json={"config_version": 1, "features": {}})

    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=threading.Event(),
        get_user_id=lambda: None,
        poll_interval=0.05,
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)  # allow at least 2 polls
    consumer.stop()
    await task

    assert call_count["n"] >= 2  # retried after error


@pytest.mark.asyncio
async def test_polling_up_to_date_short_circuit_is_not_malformed(caplog):
    """Backend returns 200 {config_version, up_to_date, discovery_requested}
    with NO `features` key when nothing changed. That must be a no-op, not a
    KeyError logged as 'malformed' every interval (G3)."""
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=9)
    first_event = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "config_version": 9, "up_to_date": True, "discovery_requested": False,
        })

    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=first_event,
        get_user_id=lambda: None,
        poll_interval=0.05,
        transport=httpx.MockTransport(handler),
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="onelo"):
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.12)  # let >1 poll happen
        consumer.stop()
        await task

    assert "malformed" not in caplog.text
    # Cache snapshot untouched; version stays put.
    assert cache.config_version == 9
    assert cache.get("chat") == "enabled"
    assert first_event.is_set()


@pytest.mark.asyncio
async def test_polling_survives_non_object_json_body():
    """A valid-JSON-but-non-object 200 body (e.g. a stray proxy array/string)
    must be logged and skipped — never crash the poll loop permanently."""
    cache = ThreadSafeCache()
    call_count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=[])  # JSON array, not an object

    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=cache,
        first_event=threading.Event(),
        get_user_id=lambda: None,
        poll_interval=0.05,
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.16)  # allow multiple polls
    consumer.stop()
    await task

    assert call_count["n"] >= 2  # loop survived and kept polling


@pytest.mark.asyncio
async def test_polling_fires_discovery_requested():
    """discovery_requested: true in the poll body must invoke the callback —
    parity with the SSE discovery_requested event."""
    fired = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "config_version": 3,
            "features": {"chat": {"status": "enabled"}},
            "discovery_requested": True,
        })

    consumer = PollingConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        poll_interval=0.05,
        transport=httpx.MockTransport(handler),
        on_discovery_requested=lambda: fired.__setitem__("n", fired["n"] + 1),
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.08)
    consumer.stop()
    await task

    assert fired["n"] >= 1
