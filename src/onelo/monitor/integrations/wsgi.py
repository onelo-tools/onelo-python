"""WSGI middleware — Flask, plain Django (sync), other WSGI apps.

Usage (Flask):

    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    app = Flask(__name__)
    app.wsgi_app = OneloMonitorWSGIMiddleware(app.wsgi_app)

Same lifecycle as the ASGI middleware: fork isolation scope, attach request
context, capture exceptions before the framework's error handler, tag the
status code on response.

For async-only stacks (FastAPI / Starlette / Litestar) prefer
``OneloMonitorASGIMiddleware`` — this WSGI version exists for sync apps.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable

from onelo.monitor._request_context import (
    attach_request_to_scope,
    attach_response_to_scope,
    capture_and_reraise,
    env_to_headers,
    wsgi_url,
)
from onelo.monitor._scope import isolation_scope


# Sticky WSGI types — defined locally to avoid pulling in extra deps.
WSGIEnviron = dict[str, Any]
WSGIStartResponse = Callable[..., Any]
WSGIApp = Callable[[WSGIEnviron, WSGIStartResponse], Iterable[bytes]]


class OneloMonitorWSGIMiddleware:
    """Wraps any WSGI callable.

    Parameters
    ----------
    app:
        The inner WSGI app to wrap.
    transaction_from_path:
        Set ``scope.transaction`` to the request path. Disable if paths
        embed user-specific IDs you don't want logged.
    """

    def __init__(self, app: WSGIApp, *, transaction_from_path: bool = True) -> None:
        self.app = app
        self.transaction_from_path = transaction_from_path

    def __call__(
        self,
        environ: WSGIEnviron,
        start_response: WSGIStartResponse,
    ) -> Iterable[bytes]:
        with isolation_scope() as iso:
            method = str(environ.get("REQUEST_METHOD", "GET"))
            url = wsgi_url(environ)
            headers = env_to_headers(environ)
            attach_request_to_scope(method=method, url=url, headers=headers)

            if self.transaction_from_path:
                path = environ.get("PATH_INFO") or "/"
                iso.set_transaction(f"{method} {path}")

            captured_status: list[int] = []

            def start_response_proxy(status: str, *args: Any, **kwargs: Any) -> Any:
                # status is "200 OK" / "500 Internal Server Error". Extract code.
                code_str = status.split(" ", 1)[0] if status else ""
                try:
                    code = int(code_str)
                except ValueError:
                    code = 0
                if code:
                    captured_status.append(code)
                    attach_response_to_scope(status_code=code)
                return start_response(status, *args, **kwargs)

            # Stream the body while the isolation scope is STILL OPEN. Because
            # __call__ is a generator, the `with isolation_scope()` block stays
            # active across the whole `yield from` — request-scoped breadcrumbs
            # remain in scope during body iteration WITHOUT materialising the
            # response into a list. The old `list(self.app(...))` broke
            # streaming/SSE and large downloads (whole body buffered in memory
            # before the first byte). Exceptions raised while producing the body
            # are captured too; `close()` is honoured per the WSGI spec.
            try:
                result = self.app(environ, start_response_proxy)
            except Exception as exc:  # noqa: BLE001
                capture_and_reraise(exc, feature_name="http_request")
                return  # unreachable — capture_and_reraise re-raises
            try:
                yield from result
            except Exception as exc:  # noqa: BLE001
                capture_and_reraise(exc, feature_name="http_request")
            finally:
                close = getattr(result, "close", None)
                if callable(close):
                    close()


__all__ = ["OneloMonitorWSGIMiddleware"]
