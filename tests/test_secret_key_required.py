"""#520: per-user Features calls from a server SDK require the SECRET key.

A publishable key is public, so a pk-signed call naming a user_id is exactly as
spoofable as a client. The SDK refuses it loudly (ValueError naming the fix)
before anything is sent. Anonymous/global calls with a publishable key keep
working, and secret-key per-user calls are unchanged.
"""
import asyncio
import json

import httpx
import pytest

from onelo import Onelo
from onelo._cache import ThreadSafeCache
from onelo._features import FeaturesClient


def _features(key: str, handler) -> FeaturesClient:
    return FeaturesClient(
        cache=ThreadSafeCache(),
        schedule_batch_ping_callback=lambda: None,
        api_url="https://example.com",
        key=key,
        transport=httpx.MockTransport(handler),
    )


def _sse_handler():
    body = b'event: up_to_date\ndata: {"config_version": 0}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return handler


def test_for_user_with_publishable_key_raises_and_sends_nothing():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"features": {}})

    fc = _features("onelo_pk_live_x", handler)
    with pytest.raises(ValueError, match=r"use your secret key \(onelo_sk_"):
        asyncio.run(fc.for_user("alice"))
    assert calls == []


def test_publishable_key_with_sk_in_its_tail_is_still_publishable():
    fc = _features("onelo_pk_live_sk_abc", lambda r: httpx.Response(200, json={"features": {}}))
    with pytest.raises(ValueError, match=r"use your secret key"):
        asyncio.run(fc.for_user("alice"))


def test_for_user_with_secret_key_is_unchanged():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"features": {"f": {"status": "enabled"}}})

    fc = _features("onelo_sk_live_x", handler)
    uf = asyncio.run(fc.for_user("alice"))
    assert uf.is_enabled("f") is True
    assert seen["publishableKey"] == "onelo_sk_live_x"
    assert seen["userId"] == "alice"


def test_publishable_key_kwarg_still_accepted_as_deprecated_alias():
    fc = FeaturesClient(
        cache=ThreadSafeCache(),
        schedule_batch_ping_callback=lambda: None,
        api_url="https://example.com",
        publishable_key="onelo_sk_live_x",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"features": {}})),
    )
    asyncio.run(fc.for_user("alice"))


def test_identify_user_with_publishable_key_raises():
    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=httpx.MockTransport(_sse_handler()),
    )
    try:
        with pytest.raises(ValueError, match=r"use your secret key \(onelo_sk_"):
            onelo.identify("user-42")
        # Anonymous use keeps working with a publishable key.
        onelo.identify(None)
        assert onelo.features.feature("anything").is_enabled is False
    finally:
        onelo.close()


def test_identify_user_with_secret_key_is_unchanged():
    onelo = Onelo(
        secret_key="onelo_sk_test_abc",
        api_url="https://example.com",
        transport=httpx.MockTransport(_sse_handler()),
    )
    try:
        onelo.identify("user-42")
    finally:
        onelo.close()
