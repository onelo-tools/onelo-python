"""Tests for Onelo client — public surface, lifecycle, validation."""
import httpx
import pytest

from onelo import Onelo, Feature


def _sse_handler(events):
    body = "".join(f"event: {evt}\ndata: {data}\n\n" for evt, data in events)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    return handler


def _make_onelo(events=None, **overrides):
    events = events or [("up_to_date", '{"config_version": 0}')]
    defaults = dict(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=httpx.MockTransport(_sse_handler(events)),
    )
    defaults.update(overrides)
    return Onelo(**defaults)


def test_invalid_publishable_key_format_raises():
    with pytest.raises(ValueError, match="onelo_pk_"):
        Onelo(publishable_key="not-an-onelo-key", api_url="https://example.com")


def test_secret_key_accepted():
    o = Onelo(secret_key="onelo_sk_live_abcdef", api_url="https://example.com")
    assert o._is_secret_key is True
    assert o._key == "onelo_sk_live_abcdef"
    o.close()


def test_neither_key_raises():
    with pytest.raises(ValueError, match="requires a key"):
        Onelo(api_url="https://example.com")


def test_both_keys_raises():
    with pytest.raises(ValueError, match="exactly one"):
        Onelo(
            publishable_key="onelo_pk_live_a",
            secret_key="onelo_sk_live_b",
            api_url="https://example.com",
        )


def test_unified_key_arg_accepts_publishable():
    """Onelo(key=...) — happy path with a publishable test key."""
    onelo = Onelo(key="onelo_pk_test_abc", api_url="https://example.com")
    try:
        assert onelo._key == "onelo_pk_test_abc"
        assert onelo._is_secret_key is False
    finally:
        onelo.close()


def test_unified_key_arg_accepts_secret():
    """Onelo(key=...) — auto-detects secret by `_sk_` segment in prefix."""
    onelo = Onelo(key="onelo_sk_live_xyz", api_url="https://example.com")
    try:
        assert onelo._key == "onelo_sk_live_xyz"
        assert onelo._is_secret_key is True
    finally:
        onelo.close()


def test_unified_key_conflicts_with_explicit_form():
    """Passing both `key=` and `publishable_key=` is rejected."""
    with pytest.raises(ValueError, match="exactly one"):
        Onelo(
            key="onelo_pk_live_a",
            publishable_key="onelo_pk_live_b",
            api_url="https://example.com",
        )


def test_invalid_secret_key_format_raises():
    with pytest.raises(ValueError, match="onelo_pk_\\* or onelo_sk_\\*"):
        Onelo(secret_key="onelo_zk_live_abc", api_url="https://example.com")


def test_invalid_api_url_raises():
    with pytest.raises(ValueError, match="api_url"):
        Onelo(publishable_key="onelo_pk_test_abc", api_url="not-a-url")


def test_feature_lookup_returns_feature_object():
    onelo = _make_onelo([
        ("connected", '{"config_version": 1, "features": {"chat": {"status": "enabled"}}}'),
    ])
    onelo.ready(timeout=2.0)
    try:
        feat = onelo.features.feature("chat")
        assert isinstance(feat, Feature)
        assert feat.name == "chat"
        assert feat.is_enabled is True
    finally:
        onelo.close()


def test_feature_lookup_cache_miss_is_hidden():
    onelo = _make_onelo()
    onelo.ready(timeout=2.0)
    try:
        feat = onelo.features.feature("never-declared")
        assert feat.status == "hidden"
        assert feat.is_enabled is False
    finally:
        onelo.close()


def test_identify_updates_user_id():
    onelo = _make_onelo()
    onelo.ready(timeout=2.0)
    try:
        onelo.identify("user-42")
        # No public getter; verify by behavior in integration tests.
        # Here we just ensure no exception.
    finally:
        onelo.close()


def test_identify_churn_warns_once(caplog):
    """5 distinct user ids within the window → one warning, then silence."""
    import logging

    onelo = _make_onelo()
    onelo.ready(timeout=2.0)
    try:
        with caplog.at_level(logging.WARNING, logger="onelo"):
            for i in range(10):
                onelo.identify(f"user-{i}")
        churn_warnings = [
            r for r in caplog.records if "distinct user ids" in r.getMessage()
        ]
        assert len(churn_warnings) == 1
    finally:
        onelo.close()


def test_identify_same_user_does_not_warn(caplog):
    """Repeated identify() with the SAME id is the documented single-user
    pattern — must never trigger the churn warning."""
    import logging

    onelo = _make_onelo()
    onelo.ready(timeout=2.0)
    try:
        with caplog.at_level(logging.WARNING, logger="onelo"):
            for _ in range(20):
                onelo.identify("user-42")
        assert not any(
            "distinct user ids" in r.getMessage() for r in caplog.records
        )
    finally:
        onelo.close()


def test_close_is_idempotent():
    onelo = _make_onelo()
    onelo.ready(timeout=2.0)
    onelo.close()
    onelo.close()  # second call must not raise


def test_context_manager_closes_on_exit():
    events = [("connected", '{"config_version": 1, "features": {"x": {"status": "enabled"}}}')]
    with _make_onelo(events) as onelo:
        onelo.ready(timeout=2.0)
        assert onelo.features.feature("x").is_enabled
    # After exit, calling close() again is fine (idempotent).
    onelo.close()


def test_ready_returns_true_when_event_arrives():
    onelo = _make_onelo()
    try:
        assert onelo.ready(timeout=2.0) is True
    finally:
        onelo.close()


def test_ready_returns_false_on_timeout():
    """If the backend never responds, ready() returns False after the timeout."""

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        # Hang forever (well, until pytest aborts)
        import asyncio
        await asyncio.sleep(60)
        return httpx.Response(500)

    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=httpx.MockTransport(slow_handler),
        request_timeout=0.05,  # fast fail
    )
    try:
        assert onelo.ready(timeout=0.2) is False
    finally:
        onelo.close()


def test_declare_forwards_to_backend():
    captured: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/batch-ping"):
            captured.append(request)
            return httpx.Response(204)
        # Default: SSE endpoint up_to_date
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='event: up_to_date\ndata: {"config_version": 0}\n\n',
        )

    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=httpx.MockTransport(handler),
    )
    onelo.ready(timeout=2.0)
    try:
        onelo.features.declare(["a", "b"])
        # Auto-discovery batch-ping is debounced (1s). Wait long enough for
        # the timer to fire AND the async POST to complete.
        import time
        time.sleep(1.5)
        assert len(captured) == 1
        body = captured[0].read()
        import json as _json
        payload = _json.loads(body)
        assert sorted(payload["features"]) == ["a", "b"]
    finally:
        onelo.close()


def test_batch_ping_4xx_logs_warning(caplog):
    """A rejected batch-ping (e.g. test key bound to a different device) must
    surface at warning level — staying silent leaves the dev guessing why
    discovery never happens."""
    import asyncio
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/batch-ping" in str(request.url):
            return httpx.Response(403, json={"detail": {
                "error": "key_bound_to_different_device",
                "message": "This test key is bound to a different device.",
            }})
        body = 'event: up_to_date\ndata: {"config_version": 0}\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    onelo = Onelo(
        key="onelo_sk_live_abc",
        api_url="https://example.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        with caplog.at_level(logging.WARNING, logger="onelo"):
            asyncio.run(onelo._batch_ping_async(["foo"]))
        assert any(
            "batch-ping rejected" in r.message and "key_bound_to_different_device" in r.message
            for r in caplog.records
        )
    finally:
        onelo.close()


# ── feature_environment / ONELO_FEATURE_ENVIRONMENT ─────────────────────────

def test_feature_environment_arg_stored():
    o = Onelo(secret_key="onelo_sk_live_abc", api_url="https://example.com", feature_environment="test")
    try:
        assert o._feature_environment == "test"
    finally:
        o.close()


def test_feature_environment_from_env_var(monkeypatch):
    monkeypatch.setenv("ONELO_FEATURE_ENVIRONMENT", "test")
    o = Onelo(secret_key="onelo_sk_live_abc", api_url="https://example.com")
    try:
        assert o._feature_environment == "test"
    finally:
        o.close()


def test_feature_environment_arg_overrides_env_var(monkeypatch):
    monkeypatch.setenv("ONELO_FEATURE_ENVIRONMENT", "live")
    o = Onelo(secret_key="onelo_sk_live_abc", api_url="https://example.com", feature_environment="test")
    try:
        assert o._feature_environment == "test"
    finally:
        o.close()


def test_feature_environment_default_none(monkeypatch):
    monkeypatch.delenv("ONELO_FEATURE_ENVIRONMENT", raising=False)
    o = Onelo(secret_key="onelo_sk_live_abc", api_url="https://example.com")
    try:
        assert o._feature_environment is None
    finally:
        o.close()


def test_feature_environment_invalid_raises(monkeypatch):
    monkeypatch.delenv("ONELO_FEATURE_ENVIRONMENT", raising=False)
    with pytest.raises(ValueError, match="feature_environment must be"):
        Onelo(secret_key="onelo_sk_live_abc", api_url="https://example.com", feature_environment="staging")


def test_after_fork_propagates_feature_environment():
    """A preload+fork server (gunicorn/uwsgi) rebuilds the backend in the child
    via _after_fork. It MUST carry feature_environment across — otherwise the
    worker silently reverts to the key-prefix env and resolves the wrong
    snapshot (G1). Here the explicit env ('live') differs from the key prefix
    ('test'), so a dropped env would be observable."""
    o = _make_onelo(feature_environment="live")
    original_backend = o._backend
    try:
        assert o._feature_environment == "live"
        # Simulate the after-fork rebuild (does not actually fork; just rebuilds
        # FeaturesClient + BackendThread the way register_at_fork would).
        o._after_fork()
        assert o.features._feature_environment == "live"
        assert o._backend._feature_environment == "live"
    finally:
        # No real fork happened, so the pre-fork backend thread is still alive
        # in this process — stop it too so the test leaks no daemon thread.
        original_backend.stop(timeout=1.0)
        o.close()
