"""Litestar integration.

Litestar (formerly Starlite) accepts ASGI middleware via the ``middleware``
parameter on the ``Litestar(...)`` constructor. The base
``OneloMonitorASGIMiddleware`` works directly.

Usage:

    from litestar import Litestar
    from onelo.monitor.integrations.litestar import OneloMonitorASGIMiddleware

    app = Litestar(
        route_handlers=[...],
        middleware=[OneloMonitorASGIMiddleware],
    )
"""
from __future__ import annotations

from onelo.monitor.integrations.asgi import OneloMonitorASGIMiddleware

__all__ = ["OneloMonitorASGIMiddleware"]
