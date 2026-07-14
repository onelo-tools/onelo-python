"""Django integration — middleware class.

Usage:

    # settings.py
    MIDDLEWARE = [
        "onelo.monitor.integrations.django.OneloMonitorMiddleware",
        # ... your other middleware ...
    ]

The middleware:
  - Forks an isolation scope per request (so concurrent threads / async
    handlers don't leak user / breadcrumbs).
  - Attaches request context (URL, method, scrubbed headers).
  - Captures exceptions via Django's ``process_exception`` hook *before*
    Django's own exception middleware turns them into 500s — this is the
    canonical place to plug error tracking in Django.
  - Tags the response with the status code.

For Django ASGI deployments (Daphne / uvicorn / hypercorn) this still works
— Django's MIDDLEWARE chain runs identically under both WSGI and ASGI for
the request/response lifecycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from onelo.monitor._capture import capture_exception
from onelo.monitor._request_context import (
    attach_request_to_scope,
    attach_response_to_scope,
)
from onelo.monitor._scope import isolation_scope


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


class OneloMonitorMiddleware:
    """Django middleware that wires Onelo Monitor into the request lifecycle."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[..., Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        with isolation_scope() as iso:
            method = request.method or "GET"
            url = request.build_absolute_uri()
            headers = self._headers_from_request(request)
            attach_request_to_scope(method=method, url=url, headers=headers)
            iso.set_transaction(f"{method} {request.path}")

            try:
                response = self.get_response(request)
            except Exception as exc:  # noqa: BLE001
                # Defensive — usually `process_exception` catches first, but
                # raw exception middleware does happen.
                try:
                    capture_exception(exc, feature_name="http_request")
                except Exception:  # noqa: BLE001
                    pass
                raise

            status = getattr(response, "status_code", None)
            if isinstance(status, int):
                attach_response_to_scope(status_code=status)
            return response

    # Django calls this on any exception raised from a view. It runs BEFORE
    # the framework's own 500 page renders, so it's the right place to
    # capture stack traces.
    def process_exception(
        self, request: "HttpRequest", exception: BaseException
    ) -> None:
        try:
            capture_exception(exception, feature_name="http_request")
        except Exception:  # noqa: BLE001
            pass
        # Returning None lets Django's default exception handling proceed.

    @staticmethod
    def _headers_from_request(request: "HttpRequest") -> dict[str, str]:
        # Django ≥ 2.2 has request.headers (case-insensitive dict-like).
        # Older versions only expose META — we support both.
        try:
            headers_attr = request.headers
        except AttributeError:
            return {}
        # request.headers is a HttpHeaders type that iterates as items.
        try:
            return {str(k): str(v) for k, v in headers_attr.items()}
        except (AttributeError, TypeError):
            return {}


__all__ = ["OneloMonitorMiddleware"]
