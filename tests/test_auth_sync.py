"""Tests for the sync auth core (onelo.auth.verify_token_sync).

These tests do NOT require the [fastapi] extra — they exercise the
framework-agnostic sync helper used by Flask, Django, and WSGI adapters.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from onelo import (
    Onelo,
    OneloAuthForbidden,
    OneloAuthInvalidToken,
    OneloAuthUnavailable,
    OneloUser,
    verify_token_sync,
)
from onelo._auth_cache import InProcessSyncAuthCache


SAMPLE_PAYLOAD = {
    "id": "user-123",
    "email": "alice@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class SyncAuthTransport(httpx.MockTransport):
    """Synchronous MockTransport scripting /api/sdk/auth/user.

    Also stubs the SSE/poll endpoints used by the Onelo background thread
    so it doesn't spam errors during tests.
    """

    def __init__(self, user_endpoint_script: list[tuple[int, Any]]):
        self._script = list(user_endpoint_script)
        self.user_endpoint_calls: list[httpx.Request] = []
        self.client_classes_seen: list[str] = []
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        # Record which client type called us — sync client uses
        # `httpx.Client`, async uses `httpx.AsyncClient`. The transport
        # itself doesn't expose this, but we can detect by checking the
        # extensions dict (sync transports are called sync, async are
        # called via __aenter__). The simplest reliable signal: the
        # MockTransport's _handler signature being sync means the caller
        # invoked it synchronously.
        path = request.url.path
        if path == "/api/sdk/auth/user":
            self.user_endpoint_calls.append(request)
            if not self._script:
                return httpx.Response(500, json={"error": "script exhausted"})
            status, body = self._script.pop(0)
            if body is None:
                return httpx.Response(status)
            return httpx.Response(status, json=body)
        if path == "/api/sdk/features/stream":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='event: up_to_date\ndata: {"config_version": 0}\n\n',
            )
        if path == "/api/sdk/features/poll":
            return httpx.Response(304)
        return httpx.Response(404, json={"error": f"unmocked {path}"})


def _make_secret_client(transport: httpx.MockTransport) -> Onelo:
    return Onelo(
        secret_key="onelo_sk_test_abcdef",
        api_url="https://example.com",
        transport=transport,
    )


# ── Happy path ───────────────────────────────────────────────────────────


def test_verify_token_sync_happy_path():
    transport = SyncAuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        user = verify_token_sync(client, "abc.def.ghi")
        assert isinstance(user, OneloUser)
        assert user.id == "user-123"
        assert user.email == "alice@example.com"
        assert user.email_verified is False
        assert user.plan is None
        assert user.raw == SAMPLE_PAYLOAD
        # Auth headers were sent.
        req = transport.user_endpoint_calls[0]
        assert req.headers["authorization"] == "Bearer abc.def.ghi"
        assert req.headers["x-onelo-secret-key"] == "onelo_sk_test_abcdef"
        assert req.headers["x-publishable-key"] == "onelo_sk_test_abcdef"
    finally:
        client.close()


def test_verify_token_sync_reads_email_verified_and_plan_from_metadata():
    payload = {
        "id": "u1",
        "email": "x@y.z",
        "metadata": {"email_verified": True, "plan": "pro"},
    }
    transport = SyncAuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    try:
        user = verify_token_sync(client, "tok")
        assert user.email_verified is True
        assert user.plan == "pro"
    finally:
        client.close()


# ── Error mapping ────────────────────────────────────────────────────────


def test_verify_token_sync_401_raises_invalid():
    transport = SyncAuthTransport([(401, {"detail": "bad"})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthInvalidToken):
            verify_token_sync(client, "tok")
    finally:
        client.close()


def test_verify_token_sync_403_raises_forbidden():
    transport = SyncAuthTransport([(403, {"detail": "nope"})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthForbidden):
            verify_token_sync(client, "tok")
    finally:
        client.close()


def test_verify_token_sync_5xx_after_retries_raises_unavailable():
    transport = SyncAuthTransport([(503, {}), (503, {}), (503, {})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthUnavailable):
            verify_token_sync(
                client, "tok", retry_attempts=3, retry_total_timeout=5.0
            )
        # All three attempts exhausted.
        assert len(transport.user_endpoint_calls) == 3
    finally:
        client.close()


def test_verify_token_sync_missing_token():
    transport = SyncAuthTransport([])
    client = _make_secret_client(transport)
    try:
        from onelo import OneloAuthMissingToken

        with pytest.raises(OneloAuthMissingToken):
            verify_token_sync(client, "")
        with pytest.raises(OneloAuthMissingToken):
            verify_token_sync(client, "   ")
    finally:
        client.close()


def test_verify_token_sync_rejects_publishable_key_client():
    from onelo.auth import OneloAuthError

    transport = SyncAuthTransport([])
    client = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=transport,
    )
    try:
        with pytest.raises(OneloAuthError, match="secret_key"):
            verify_token_sync(client, "tok")
    finally:
        client.close()


# ── Retry policy ─────────────────────────────────────────────────────────


def test_verify_token_sync_retry_503_503_200_succeeds(monkeypatch):
    # Avoid waiting real backoff durations during the test.
    import onelo.auth as _auth_mod
    monkeypatch.setattr(_auth_mod, "_SYNC_BACKOFF_BASE", 0.0)

    transport = SyncAuthTransport(
        [(503, {}), (503, {}), (200, SAMPLE_PAYLOAD)]
    )
    client = _make_secret_client(transport)
    try:
        user = verify_token_sync(
            client, "tok", retry_attempts=3, retry_total_timeout=5.0
        )
        assert user.id == "user-123"
        assert len(transport.user_endpoint_calls) == 3
    finally:
        client.close()


def test_verify_token_sync_no_retry_on_4xx():
    """4xx must NOT trigger retries — only 5xx do."""
    transport = SyncAuthTransport([(401, {}), (200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthInvalidToken):
            verify_token_sync(
                client, "tok", retry_attempts=3, retry_total_timeout=5.0
            )
        # Only one call — 401 short-circuits the retry loop.
        assert len(transport.user_endpoint_calls) == 1
    finally:
        client.close()


# ── Sync httpx.Client is used (not AsyncClient) ──────────────────────────


def test_verify_token_sync_uses_sync_httpx_client(monkeypatch):
    """Ensure the sync helper uses httpx.Client (not AsyncClient).

    We patch ``httpx.Client`` *within the onelo.auth module* and confirm the
    sync helper instantiates it. The Onelo background thread uses
    ``httpx.AsyncClient`` for SSE — that's expected and unrelated; we
    only assert that the sync auth path goes through ``httpx.Client``.
    """
    import httpx as _httpx

    sync_client_uses: list[bool] = []

    real_sync = _httpx.Client

    class _SpySync(real_sync):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            sync_client_uses.append(True)
            super().__init__(*a, **kw)

    # Only patch the sync flavour inside onelo.auth — the async flavour is
    # used by the SSE thread and must stay untouched.
    monkeypatch.setattr("onelo.auth.httpx.Client", _SpySync)

    transport = SyncAuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        verify_token_sync(client, "tok")
    finally:
        client.close()

    assert sync_client_uses, "httpx.Client must be used by verify_token_sync"


# ── InProcessSyncAuthCache primitives ────────────────────────────────────


def test_inprocess_sync_auth_cache_ttl(monkeypatch):
    cache = InProcessSyncAuthCache()
    user = OneloUser(id="u1", email="a@b.c", raw={})
    cache.set("h", user, ttl=10.0)
    got = cache.get("h")
    assert got is not None and got.id == "u1"

    import time as _time
    real_mono = _time.monotonic
    monkeypatch.setattr(
        "onelo._auth_cache.time.monotonic",
        lambda: real_mono() + 1000.0,
    )
    assert cache.get("h") is None


def test_inprocess_sync_auth_cache_max_size_eviction():
    cache = InProcessSyncAuthCache(max_size=2)
    u1 = OneloUser(id="1", email="1@x", raw={})
    u2 = OneloUser(id="2", email="2@x", raw={})
    u3 = OneloUser(id="3", email="3@x", raw={})
    cache.set("a", u1, ttl=60.0)
    cache.set("b", u2, ttl=60.0)
    cache.set("c", u3, ttl=60.0)  # evicts oldest ("a")
    assert cache.get("a") is None
    got_b = cache.get("b")
    got_c = cache.get("c")
    assert got_b is not None and got_b.id == "2"
    assert got_c is not None and got_c.id == "3"


def test_inprocess_sync_auth_cache_invalidate():
    cache = InProcessSyncAuthCache()
    u = OneloUser(id="1", email="x", raw={})
    cache.set("h", u, ttl=60.0)
    cache.invalidate("h")
    assert cache.get("h") is None


def test_inprocess_sync_auth_cache_zero_ttl_skips():
    cache = InProcessSyncAuthCache()
    u = OneloUser(id="1", email="x", raw={})
    cache.set("h", u, ttl=0.0)
    assert cache.get("h") is None


def test_inprocess_sync_auth_cache_thread_safety():
    """Sanity check: concurrent writes from multiple threads don't corrupt."""
    import threading

    cache = InProcessSyncAuthCache(max_size=1000)

    def worker(i: int):
        u = OneloUser(id=str(i), email=f"{i}@x", raw={})
        for j in range(50):
            cache.set(f"k{i}-{j}", u, ttl=60.0)
            cache.get(f"k{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No exception raised, store size sane.
    assert 0 < len(cache._store) <= 1000


# ── A3: 429 rate-limit handling ─────────────────────────────────────────────

def test_verify_token_sync_429_raises_ratelimited_without_retry():
    from onelo import OneloAuthRateLimited, OneloAuthUnavailable

    transport = SyncAuthTransport([(429, {"detail": "slow down"})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthRateLimited) as ei:
            verify_token_sync(client, "tok", retry_attempts=3)
        # A rate-limit must NOT be retried (unlike a 5xx) — only one call.
        assert len(transport.user_endpoint_calls) == 1
        # Subclass of Unavailable so adapters still map it to 503.
        assert isinstance(ei.value, OneloAuthUnavailable)
    finally:
        client.close()


def test_ratelimited_is_subclass_of_unavailable():
    from onelo import OneloAuthRateLimited, OneloAuthUnavailable
    assert issubclass(OneloAuthRateLimited, OneloAuthUnavailable)


def test_parse_retry_after():
    from onelo.auth import _parse_retry_after
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("  5  ") == 5.0
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("-1") is None
    assert _parse_retry_after("garbage") is None


def test_429_populates_retry_after_from_header():
    from onelo import OneloAuthRateLimited

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/sdk/auth/user":
            return httpx.Response(429, headers={"Retry-After": "12"}, json={})
        if path == "/api/sdk/features/stream":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text='event: up_to_date\ndata: {"config_version": 0}\n\n')
        return httpx.Response(304)

    client = _make_secret_client(httpx.MockTransport(handler))
    try:
        with pytest.raises(OneloAuthRateLimited) as ei:
            verify_token_sync(client, "tok")
        assert ei.value.retry_after == 12.0
    finally:
        client.close()


# ── A2: HTTP client pooling ─────────────────────────────────────────────────

def test_pooled_sync_client_reused_and_rebuilt_when_closed():
    from onelo.auth import _get_pooled_sync_client
    c1 = _get_pooled_sync_client(5.0)
    c2 = _get_pooled_sync_client(5.0)
    assert c1 is c2  # reused, no fresh TLS handshake per call
    c1.close()
    c3 = _get_pooled_sync_client(5.0)
    assert c3 is not c1  # rebuilt after close
    assert not c3.is_closed
    c3.close()


@pytest.mark.asyncio
async def test_pooled_async_client_reused_within_loop():
    from onelo.auth import _get_pooled_async_client
    a1 = _get_pooled_async_client(5.0)
    a2 = _get_pooled_async_client(5.0)
    assert a1 is a2
    await a1.aclose()
    a3 = _get_pooled_async_client(5.0)
    assert a3 is not a1
    await a3.aclose()


def test_pooled_async_client_is_per_loop_and_weakly_held():
    """A2 leak fix: distinct event loops get distinct clients (no id-reuse
    crash), and dead loops are auto-evicted from the WeakKeyDictionary so a
    per-task asyncio.run() caller doesn't leak a client per loop forever."""
    import asyncio
    import gc
    from onelo.auth import _get_pooled_async_client, _async_clients

    held: list[httpx.AsyncClient] = []

    async def grab() -> None:
        # Keep the object alive so we compare by identity (id() can be reused
        # after GC — the exact bug this fix is about).
        held.append(_get_pooled_async_client(5.0))

    asyncio.run(grab())
    asyncio.run(grab())
    assert held[0] is not held[1]  # a fresh loop never reuses another loop's client

    # Drop the client refs; the two transient loops are already closed, so the
    # weak keys must evict — no unbounded growth per asyncio.run() call.
    held.clear()
    gc.collect()
    assert len(_async_clients) == 0


# ── A6: empty id rejected ────────────────────────────────────────────────────

def test_verify_token_sync_200_without_id_raises_unavailable():
    """A 200 lacking a usable id is a malformed backend response, not a valid
    anonymous user — must NOT pass as authenticated (A6)."""
    transport = SyncAuthTransport([(200, {"email": "x@y.z", "metadata": {}})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthUnavailable):
            verify_token_sync(client, "tok")
    finally:
        client.close()


# ── A4: query-token extraction helper ────────────────────────────────────────

def test_token_from_query_string():
    from onelo.auth import _token_from_query_string
    assert _token_from_query_string("token=abc&x=1") == "abc"
    assert _token_from_query_string(b"token=abc") == "abc"
    assert _token_from_query_string("token=%20abc%20") == "abc"  # trimmed
    assert _token_from_query_string("x=1") is None
    assert _token_from_query_string("") is None
    assert _token_from_query_string("token=") is None


@pytest.mark.parametrize("body", [None, [1, 2], "ok", 42])
def test_verify_token_sync_200_non_object_body_raises_unavailable(body):
    """A 200 whose JSON body is not an object (proxy/CDN error page) must raise
    a typed OneloAuthUnavailable, not crash with AttributeError (A6)."""
    transport = SyncAuthTransport([(200, body)])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthUnavailable):
            verify_token_sync(client, "tok", retry_attempts=1)
    finally:
        client.close()
