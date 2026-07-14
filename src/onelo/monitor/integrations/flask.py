"""Flask integration.

Two equivalent patterns are exposed; pick the one that fits your code style:

    # 1. install() — recommended, one-liner
    from flask import Flask
    from onelo.monitor.integrations.flask import install

    app = Flask(__name__)
    install(app)

    # 2. WSGI middleware — useful if you have a custom WSGI stack
    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    app.wsgi_app = OneloMonitorWSGIMiddleware(app.wsgi_app)

``install()`` registers Flask's ``before_request``, ``teardown_request`` and
``got_request_exception`` signals — the canonical hooks. We prefer them
over WSGI middleware because Flask's request context (``flask.request``,
``flask.g``) is already set up there, so request body / view name are
available if you ever want them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from onelo.monitor._capture import capture_exception
from onelo.monitor._request_context import (
    attach_request_to_scope,
    attach_response_to_scope,
)
from onelo.monitor._scope import _isolation_scope_var, get_global_scope


if TYPE_CHECKING:
    from flask import Flask


_TOKEN_KEY = "_onelo_monitor_token"
"""Stash key on flask.g so before_request and teardown_request can pair up."""


def install(app: "Flask") -> None:
    """Register Onelo Monitor hooks on a Flask app."""
    from flask import g, got_request_exception, request

    @app.before_request
    def _onelo_before_request() -> None:
        # Manually fork the isolation scope (we can't use a `with` block
        # because before/teardown are separate calls). Save the contextvar
        # token on flask.g so we can reset it in teardown.
        forked = get_global_scope().clone()
        token = _isolation_scope_var.set(forked)
        setattr(g, _TOKEN_KEY, token)

        try:
            method = request.method or "GET"
            url = request.url
            headers = {k: v for k, v in request.headers.items()}
            attach_request_to_scope(method=method, url=url, headers=headers)
            forked.set_transaction(f"{method} {request.path}")
        except Exception:  # noqa: BLE001
            # Never break a request because of a monitor setup error.
            pass

    @app.after_request
    def _onelo_after_request(response: Any) -> Any:
        try:
            status = getattr(response, "status_code", None)
            if isinstance(status, int):
                attach_response_to_scope(status_code=status)
        except Exception:  # noqa: BLE001
            pass
        return response

    @got_request_exception.connect_via(app)
    def _onelo_got_exception(_sender: Any, exception: BaseException, **_: Any) -> None:
        try:
            capture_exception(exception, feature_name="http_request")
        except Exception:  # noqa: BLE001
            pass

    @app.teardown_request
    def _onelo_teardown(exception: BaseException | None) -> None:
        token = getattr(g, _TOKEN_KEY, None)
        if token is not None:
            try:
                _isolation_scope_var.reset(token)
            except (ValueError, LookupError):
                # Token from a different context — happens if Flask reuses
                # an app context across threads in a way we don't expect.
                pass


__all__ = ["install"]
