"""Tests for features.for_user() — stateless, multi-user-safe per-user resolve.

These cover the path Turingo's WebSocket backend needs: gate a feature for a
SPECIFIC connection's user without touching the process-global identity.
"""
import asyncio
import json

import httpx
import pytest

from onelo._cache import ThreadSafeCache
from onelo._features import FeaturesClient, UserFeatures


def _client(handler) -> FeaturesClient:
    return FeaturesClient(
        cache=ThreadSafeCache(),
        schedule_batch_ping_callback=lambda: None,
        api_url="https://example.com",
        publishable_key="onelo_pk_live_x",
        transport=httpx.MockTransport(handler),
    )


def test_for_user_resolves_per_user_snapshot():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sdk/features/resolve"
        payload = json.loads(request.content)
        assert payload["userId"] == "alice"
        assert payload["publishableKey"] == "onelo_pk_live_x"
        return httpx.Response(200, json={
            "features": {
                "face-stream": {"status": "enabled"},
                "locked": {"status": "greyed"},
            },
            "config_version": 1,
        })

    fc = _client(handler)
    uf = asyncio.run(fc.for_user("alice"))
    assert isinstance(uf, UserFeatures)
    assert uf.feature("face-stream").is_enabled is True
    assert uf.is_enabled("face-stream") is True
    assert uf.feature("locked").is_enabled is False
    assert uf.feature("unknown").status == "hidden"  # fail-closed on snapshot miss


def test_for_user_caches_per_user():
    count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(200, json={"features": {"x": {"status": "enabled"}}, "config_version": 1})

    fc = _client(handler)
    asyncio.run(fc.for_user("bob", ttl=60))
    asyncio.run(fc.for_user("bob", ttl=60))  # within TTL → served from cache
    assert count["n"] == 1


def test_for_user_distinct_users_get_distinct_snapshots():
    async def handler(request: httpx.Request) -> httpx.Response:
        uid = json.loads(request.content)["userId"]
        status = "enabled" if uid == "pro" else "greyed"
        return httpx.Response(200, json={"features": {"f": {"status": status}}, "config_version": 1})

    fc = _client(handler)
    assert asyncio.run(fc.for_user("pro")).feature("f").is_enabled is True
    assert asyncio.run(fc.for_user("free")).feature("f").is_enabled is False


def test_for_user_empty_id_raises():
    async def handler(request: httpx.Request) -> httpx.Response:  # never called
        return httpx.Response(200, json={"features": {}})

    fc = _client(handler)
    with pytest.raises(ValueError):
        asyncio.run(fc.for_user(""))


def test_for_user_http_error_is_fail_closed():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "boom"})

    fc = _client(handler)
    uf = asyncio.run(fc.for_user("alice"))
    assert uf.feature("face-stream").is_enabled is False  # error → hidden, never raises


def test_for_user_does_not_touch_global_identity():
    """for_user must NOT mutate the process-global cache (the identify() path)."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"features": {"x": {"status": "enabled"}}, "config_version": 1})

    cache = ThreadSafeCache()
    fc = FeaturesClient(
        cache=cache,
        schedule_batch_ping_callback=lambda: None,
        api_url="https://example.com",
        publishable_key="onelo_pk_live_x",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(fc.for_user("alice"))
    # The global cache (used by the non-per-user feature()) is untouched.
    assert fc.feature("x").status == "hidden"


def test_invalidate_user_forces_refetch():
    count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(200, json={"features": {"x": {"status": "enabled"}}, "config_version": 1})

    fc = _client(handler)
    asyncio.run(fc.for_user("u", ttl=300))  # 1st → network
    asyncio.run(fc.for_user("u", ttl=300))  # cached → no network
    assert count["n"] == 1
    fc.invalidate_user("u")                  # drop this user's cache
    asyncio.run(fc.for_user("u", ttl=300))  # re-fetch
    assert count["n"] == 2


def test_invalidate_user_none_clears_all():
    count = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(200, json={"features": {"x": {"status": "enabled"}}, "config_version": 1})

    fc = _client(handler)
    asyncio.run(fc.for_user("a", ttl=300))
    asyncio.run(fc.for_user("b", ttl=300))
    assert count["n"] == 2
    fc.invalidate_user()  # clear EVERY cached user
    asyncio.run(fc.for_user("a", ttl=300))
    asyncio.run(fc.for_user("b", ttl=300))
    assert count["n"] == 4


def test_for_user_forwards_feature_environment():
    """When feature_environment is set, /resolve body carries environment."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"features": {}, "config_version": 1})

    fc = FeaturesClient(
        cache=ThreadSafeCache(),
        schedule_batch_ping_callback=lambda: None,
        api_url="https://example.com",
        publishable_key="onelo_sk_live_x",
        transport=httpx.MockTransport(handler),
        feature_environment="test",
    )
    asyncio.run(fc.for_user("alice"))
    assert captured["body"]["environment"] == "test"


def test_for_user_omits_environment_when_unset():
    """No feature_environment → field absent so backend falls back to prefix."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"features": {}, "config_version": 1})

    fc = FeaturesClient(
        cache=ThreadSafeCache(),
        schedule_batch_ping_callback=lambda: None,
        api_url="https://example.com",
        publishable_key="onelo_sk_live_x",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(fc.for_user("alice"))
    assert "environment" not in captured["body"]
