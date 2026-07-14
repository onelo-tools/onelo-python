"""HTTP client factory — single place to set timeout, User-Agent, etc.

Thin wrapper around httpx.AsyncClient. Exists so SSE and polling consumers
don't each re-derive these settings.
"""
import httpx

from onelo._version import __version__


def create_async_client(
    timeout: float | httpx.Timeout = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build an AsyncClient with the SDK's standard headers and timeout.

    `timeout` may be a float (applied to all phases) or an `httpx.Timeout`
    instance for per-phase control (used by the SSE consumer to disable the
    read timeout while keeping connect/write/pool short).

    `transport` is a hook for tests to inject MockTransport. In production
    callers leave it None and httpx uses the default httpcore transport.
    """
    headers = {"User-Agent": f"onelo-python/{__version__}"}
    timeout_obj = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
    return httpx.AsyncClient(
        timeout=timeout_obj,
        headers=headers,
        transport=transport,
    )
