"""Tests for the HTTP client factory — verifies headers and timeout config."""
import httpx
import pytest

from onelo._http import create_async_client
from onelo._version import __version__


@pytest.mark.asyncio
async def test_user_agent_includes_sdk_version():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"ok": True})

    client = create_async_client(timeout=5.0, transport=httpx.MockTransport(handler))
    try:
        await client.get("https://example.com/anything")
    finally:
        await client.aclose()

    assert captured["ua"] is not None
    assert "onelo-python" in captured["ua"]
    assert __version__ in captured["ua"]


@pytest.mark.asyncio
async def test_timeout_is_applied():
    """Timeout passed to factory is reflected on the client."""
    client = create_async_client(timeout=7.5)
    try:
        assert client.timeout.connect == 7.5
        assert client.timeout.read == 7.5
    finally:
        await client.aclose()
