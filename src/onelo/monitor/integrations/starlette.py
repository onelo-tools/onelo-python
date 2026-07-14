"""Starlette integration — alias for the ASGI middleware.

Starlette accepts any ASGI app via ``middleware=[Middleware(cls, ...)]``
or ``app.add_middleware(cls, ...)``, so the base ASGI class works directly.
This module re-exports it for discoverability.
"""
from __future__ import annotations

from onelo.monitor.integrations.asgi import OneloMonitorASGIMiddleware

__all__ = ["OneloMonitorASGIMiddleware"]
