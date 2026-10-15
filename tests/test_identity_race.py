"""#739: feature-snapshot identity races on the process-global identify() path.

The backend answers `up_to_date` (no snapshot) when `since_version` equals its
per-app config_version, so a version from the previous identity must never be
sent for the new one; results and ready() are bound to the identity that was
current when the request started.
"""
import asyncio

import httpx

from onelo import Onelo

SK = "onelo_sk_test_abc"
ANON = '{"config_version": 5, "features": {"chat": {"status": "enabled"}}}'
USER = '{"config_version": 5, "features": {"pro": {"status": "enabled"}}}'


def _sse(events):
    body = "".join(f"event: {e}\ndata: {d}\n\n" for e, d in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def test_identify_gets_new_users_snapshot_not_up_to_date_and_rearms_ready():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("userId") != "u2":
            return _sse([("connected", ANON)])
        await asyncio.sleep(0.3)
        # Mimic backend: equal since_version -> up_to_date, no snapshot.
        if request.url.params.get("since_version") == "5":
            return _sse([("up_to_date", '{"config_version": 5}')])
        return _sse([("connected", USER)])

    onelo = Onelo(secret_key=SK, api_url="https://example.com",
                  transport=httpx.MockTransport(handler), strategy="sse")
    try:
        assert onelo.ready(timeout=2.0)
        assert onelo.features.feature("chat").is_enabled
        onelo.identify("u2")
        # Previous identity's snapshot is gone and ready() re-arms.
        assert not onelo.features.ready(timeout=0)
        assert not onelo.features.feature("chat").is_enabled
        assert onelo.features.ready(timeout=2.0)
        assert onelo.features.feature("pro").is_enabled
        assert not onelo.features.feature("chat").is_enabled
    finally:
        onelo.close()


def test_poll_result_for_previous_identity_is_dropped():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("userId") == "u2":
            return httpx.Response(200, json={"config_version": 9,
                                             "features": {"pro": {"status": "enabled"}}})
        await asyncio.sleep(0.4)  # identify() happens while this is in flight
        return httpx.Response(200, json={"config_version": 9,
                                         "features": {"chat": {"status": "enabled"}}})

    onelo = Onelo(secret_key=SK, api_url="https://example.com",
                  transport=httpx.MockTransport(handler), strategy="polling",
                  poll_interval=60)
    try:
        import time
        time.sleep(0.1)
        onelo.identify("u2")
        assert onelo.features.ready(timeout=2.0)
        assert onelo.features.feature("pro").is_enabled
        assert not onelo.features.feature("chat").is_enabled
    finally:
        onelo.close()
