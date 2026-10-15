"""Tests for SSEConsumer — verifies event handling and reconnect behavior."""
import asyncio
import threading

import httpx
import pytest

from onelo._cache import ThreadSafeCache
import onelo._sse as sse_module
from onelo._sse import SSEConsumer


def _sse_response(events: list[tuple[str, str]]) -> httpx.Response:
    """Build an SSE-formatted response body from (event_type, json_data) pairs."""
    body = "".join(f"event: {evt}\ndata: {data}\n\n" for evt, data in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=body,
    )


class _LiveWireStream(httpx.AsyncByteStream):
    """A held-open Event Stream body, byte for byte what the backend sends.

    One `connected` frame, then either the server's real keepalive comment
    (`: keepalive\n\n`, backend/app/routes/sdk_features_sse.py) every
    `interval` seconds, or nothing at all — a connection that stays open but
    goes silent. It never ends by itself; the consumer has to drop it.
    """

    def __init__(self, *, keepalive: bool, interval: float) -> None:
        self._keepalive = keepalive
        self._interval = interval

    async def __aiter__(self):
        yield b'event: connected\ndata: {"config_version": 1, "features": {}}\n\n'
        while True:
            await asyncio.sleep(self._interval)
            if self._keepalive:
                yield b": keepalive\n\n"

    async def aclose(self) -> None:
        return None


async def _count_connects_over_window(monkeypatch, *, keepalive: bool) -> int:
    """Run a consumer for well over one staleness window; return how many
    times it connected. One connect = the stream was held; more = the
    staleness watchdog tore it down and reconnected."""
    monkeypatch.setattr(sse_module, "STALE_THRESHOLD_SECONDS", 0.3)
    monkeypatch.setattr(sse_module, "HEALTHCHECK_INTERVAL_SECONDS", 0.05)
    connects = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal connects
        connects += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_LiveWireStream(keepalive=keepalive, interval=0.05),
        )

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(1.0)  # > 3 staleness windows
    consumer.stop()
    await task
    return connects


@pytest.mark.asyncio
async def test_keepalive_comment_on_the_wire_holds_the_stream(monkeypatch):
    """#368: the server's `: keepalive` comment is the stream's proof of life.
    httpx_sse drops comments inside its decoder, so a consumer that only
    watched decoded events saw two minutes of silence on a healthy stream and
    reconnected ~30 times an hour."""
    assert await _count_connects_over_window(monkeypatch, keepalive=True) == 1


@pytest.mark.asyncio
async def test_silence_for_the_staleness_window_still_reconnects(monkeypatch):
    """The inverse: an open socket that delivers nothing is still a zombie."""
    assert await _count_connects_over_window(monkeypatch, keepalive=False) >= 2


@pytest.mark.asyncio
async def test_comment_is_not_dispatched_and_frames_split_across_chunks_decode():
    """A comment reaches no event handler; an event split mid-line across
    chunks (and CRLF endings) still decodes; data containing U+2028 is not
    split (httpx's own line decoder would split it)."""
    handled: list[tuple[str, str]] = []

    class _Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (
                b": keep",
                b"alive\r\n\r\nevent: up_to",
                b"_date\r\ndata: {}\r\n\r\n",
                'event: x\ndata: a\u2028b\n\n'.encode(),
            ):
                yield chunk

        async def aclose(self) -> None:
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_Chunks()
        )

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )
    consumer._handle_event = lambda event, data: handled.append((event, data))  # type: ignore[method-assign]
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    assert handled[:2] == [("up_to_date", "{}"), ("x", "a\u2028b")]


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


@pytest.mark.asyncio
async def test_key_travels_in_header_not_query_string():
    """ADR-0003: the Event Stream credential is the X-Publishable-Key header.

    The key must not appear anywhere in the request URL — that is the leak
    #58 set out to close, and a query-string key is also what the backend
    stamps `Deprecation`/`Sunset` on.
    """
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["header"] = request.headers.get("X-Publishable-Key")
        captured["query_key"] = request.url.params.get("key")
        captured["url"] = str(request.url)
        return _sse_response([("up_to_date", '{"config_version": 0}')])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="onelo_pk_live_secret",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    assert captured["header"] == "onelo_pk_live_secret"
    assert captured["query_key"] is None
    assert "onelo_pk_live_secret" not in captured["url"]


@pytest.mark.asyncio
async def test_transport_headers_survive_the_credential_header():
    """httpx_sse's own Accept/Cache-Control must still be sent.

    aconnect_sse builds its header map from the caller's `headers=`; passing
    one must extend that map, not replace it.
    """
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("Accept")
        captured["cache_control"] = request.headers.get("Cache-Control")
        captured["user_agent"] = request.headers.get("User-Agent")
        return _sse_response([("up_to_date", '{"config_version": 0}')])

    consumer = SSEConsumer(
        api_url="https://example.com",
        publishable_key="pk_test",
        cache=ThreadSafeCache(),
        first_event=threading.Event(),
        get_user_id=lambda: None,
        transport=httpx.MockTransport(handler),
        backoff_seconds=[10],
    )

    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.2)
    consumer.stop()
    await task

    assert captured["accept"] == "text/event-stream"
    assert captured["cache_control"] == "no-store"
    assert captured["user_agent"].startswith("onelo-python/")
