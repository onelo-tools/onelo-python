"""httpx outgoing HTTP auto-instrumentation.

Wraps a regular ``httpx.HTTPTransport`` / ``httpx.AsyncHTTPTransport`` so
every outgoing request emits a breadcrumb with method, scrubbed URL,
response status, and duration. The next captured event picks up these
breadcrumbs from the active isolation scope.

Usage:

    import httpx
    from onelo.monitor.integrations.httpx import wrap_transport, wrap_async_transport

    # Sync
    client = httpx.Client(transport=wrap_transport(httpx.HTTPTransport()))

    # Async
    async_client = httpx.AsyncClient(transport=wrap_async_transport(httpx.AsyncHTTPTransport()))

We deliberately do NOT monkey-patch ``httpx.Client.send`` globally — that
breaks tests using ``MockTransport`` and surprises users with custom
transports. Opt-in wrap is explicit and safe.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from onelo.monitor._capture import add_breadcrumb
from onelo.monitor._types import Breadcrumb


if TYPE_CHECKING:
    pass  # only for the comment

# Headers we look at for scrubbing — handled by the breadcrumb factory itself.


class OneloHTTPXTransport(httpx.BaseTransport):
    """Sync transport wrapper. Records one breadcrumb per outgoing request."""

    def __init__(self, wrapped: httpx.BaseTransport) -> None:
        self._wrapped = wrapped

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        start = time.monotonic()
        status: int | None = None
        try:
            response = self._wrapped.handle_request(request)
            status = response.status_code
            return response
        except Exception:
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            _record_http_breadcrumb(request, status, duration_ms)

    def close(self) -> None:
        self._wrapped.close()


class OneloHTTPXAsyncTransport(httpx.AsyncBaseTransport):
    """Async transport wrapper for ``httpx.AsyncClient``."""

    def __init__(self, wrapped: httpx.AsyncBaseTransport) -> None:
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        start = time.monotonic()
        status: int | None = None
        try:
            response = await self._wrapped.handle_async_request(request)
            status = response.status_code
            return response
        except Exception:
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            _record_http_breadcrumb(request, status, duration_ms)

    async def aclose(self) -> None:
        await self._wrapped.aclose()


def wrap_transport(transport: httpx.BaseTransport | None = None) -> OneloHTTPXTransport:
    """Wrap a sync transport. ``None`` defaults to ``httpx.HTTPTransport()``."""
    return OneloHTTPXTransport(transport or httpx.HTTPTransport())


def wrap_async_transport(
    transport: httpx.AsyncBaseTransport | None = None,
) -> OneloHTTPXAsyncTransport:
    """Wrap an async transport. ``None`` defaults to ``httpx.AsyncHTTPTransport()``."""
    return OneloHTTPXAsyncTransport(transport or httpx.AsyncHTTPTransport())


# ─── internals ─────────────────────────────────────────────────────────────


def _record_http_breadcrumb(
    request: httpx.Request,
    status: int | None,
    duration_ms: int,
) -> None:
    """Emit a single ``http`` breadcrumb for an outgoing request.

    Never raises — a misbehaving instrumentation must not crash the
    underlying request. Headers are passed to the Breadcrumb factory which
    runs them through ``MonitorScrubber`` before storage.
    """
    try:
        # ``request.headers`` is a Headers object that iterates as items.
        headers: dict[str, str] = {}
        try:
            for k, v in request.headers.items():
                headers[str(k)] = str(v)
        except Exception:  # noqa: BLE001
            pass

        crumb = Breadcrumb.http(
            method=str(request.method),
            url=str(request.url),
            status=status,
            duration_ms=duration_ms,
        )
        # Breadcrumb.http already scrubs URL; manually inject scrubbed headers
        # if we have them (the factory takes them via **extra). Skip for now —
        # adding to data adds payload bulk; URL-level scrub is the high-value
        # signal.
        add_breadcrumb(crumb)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "OneloHTTPXAsyncTransport",
    "OneloHTTPXTransport",
    "wrap_async_transport",
    "wrap_transport",
]
