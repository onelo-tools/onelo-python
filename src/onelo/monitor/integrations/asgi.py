"""ASGI middleware — base class FastAPI / Starlette / Litestar / Quart.

Usage (FastAPI / Starlette):

    from fastapi import FastAPI
    from onelo.monitor.integrations import OneloMonitorASGIMiddleware

    app = FastAPI()
    app.add_middleware(OneloMonitorASGIMiddleware)

The middleware:

  - Forks an isolation scope at the start of each request (so user_id /
    breadcrumbs from request A can't leak into request B).
  - Attaches request context (URL, method, scrubbed headers) to the scope.
  - Captures any exception that escapes user code, then re-raises so the
    framework's error handling still produces a 500 response.
  - Tags the scope with the HTTP status code when the response starts.
  - Skips ``websocket`` and ``lifespan`` scopes — they're not requests
    (websocket integration is a separate Faza).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import asyncio

from onelo.monitor._excepthook import install_for_loop
from onelo.monitor._request_context import (
    asgi_url,
    attach_request_to_scope,
    attach_response_to_scope,
    capture_and_reraise,
    normalise_headers,
)
from onelo.monitor._scope import isolation_scope


# Sticky type hints for ASGI primitives. Defining them here (rather than
# importing ``asgiref.typing``) keeps the integration zero-dependency.
ASGIScope = dict[str, Any]
ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


class OneloMonitorASGIMiddleware:
    """A pure ASGI middleware. Does NOT depend on FastAPI / Starlette /
    Litestar — those frameworks accept any ASGI app via ``add_middleware``
    so this single class covers all three.
    """

    def __init__(self, app: ASGIApp, *, transaction_from_path: bool = True) -> None:
        """
        Parameters
        ----------
        app:
            The wrapped ASGI app (Starlette ``add_middleware`` passes this).
        transaction_from_path:
            When ``True`` (default), set ``scope.transaction`` to the request
            path so events show "POST /api/users" in dashboards. Disable if
            your paths contain high-cardinality IDs you don't want logged.
        """
        self.app = app
        self.transaction_from_path = transaction_from_path
        # Register the asyncio loop exception handler the first time this
        # middleware is invoked. ``monitor.init`` may have been called
        # before the worker's loop existed (uvicorn spawns its loop later),
        # so the install at init-time was a no-op. Doing it lazily here
        # captures unhandled task exceptions once the loop is available.
        self._loop_handler_installed = False

    async def __call__(
        self,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            # WebSocket / lifespan / etc. — pass through untouched.
            await self.app(scope, receive, send)
            return

        # Lazy-install the asyncio exception handler on the worker's loop.
        # ``monitor.init`` typically runs before uvicorn spawns its loop, so
        # this is the first place we can grab it. Idempotent — install_for_loop
        # is a no-op on subsequent calls.
        if not self._loop_handler_installed:
            try:
                install_for_loop(asyncio.get_running_loop())
            except RuntimeError:
                pass
            self._loop_handler_installed = True

        with isolation_scope() as iso:
            method = str(scope.get("method", "GET"))
            url = asgi_url(scope)
            headers = normalise_headers(scope.get("headers"))
            attach_request_to_scope(method=method, url=url, headers=headers)

            if self.transaction_from_path:
                path = scope.get("path") or "/"
                iso.set_transaction(f"{method} {path}")

            # Wrap `send` so we can pluck the status code out of the
            # `http.response.start` message before the response is dispatched.
            async def send_with_capture(message: ASGIMessage) -> None:
                if message.get("type") == "http.response.start":
                    status = message.get("status")
                    if isinstance(status, int):
                        attach_response_to_scope(status_code=status)
                await send(message)

            try:
                await self.app(scope, receive, send_with_capture)
            except Exception as exc:  # noqa: BLE001
                # Re-raise after capture so the framework's exception
                # handler still runs and produces a real 500 response.
                capture_and_reraise(exc, feature_name="http_request")


__all__ = ["OneloMonitorASGIMiddleware"]
