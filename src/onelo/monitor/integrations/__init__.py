"""Framework integrations for Onelo Monitor.

Each integration is opt-in — import the one matching your stack. They all
share the same per-request lifecycle:

  1. Fork a fresh isolation scope so user_id / breadcrumbs from another
     request can't bleed into this one.
  2. Attach request context (URL, method, scrubbed headers) to the scope.
  3. Run the inner handler.
  4. On exception: capture and re-raise so the framework's error handler
     still produces a 500.
  5. On clean response: tag the scope with the status code.
"""
from __future__ import annotations

from onelo.monitor.integrations.asgi import OneloMonitorASGIMiddleware
from onelo.monitor.integrations.wsgi import OneloMonitorWSGIMiddleware

__all__ = [
    "OneloMonitorASGIMiddleware",
    "OneloMonitorWSGIMiddleware",
]
