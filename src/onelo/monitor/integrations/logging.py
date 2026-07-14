"""Standard ``logging`` integration.

Routes log records to the monitor with sensible defaults:

  - ``ERROR`` and ``CRITICAL`` → captured as events. If ``exc_info`` is
    attached (logger.exception(...)) we capture the exception with full
    stack trace; otherwise capture as a message.
  - ``WARNING`` → breadcrumb at ``warning`` level (no event).
  - ``INFO`` and ``DEBUG`` → breadcrumb at ``info`` level (opt-in via
    ``breadcrumb_level``).

Usage:

    import logging
    from onelo.monitor.integrations.logging import OneloLoggingHandler

    logging.getLogger().addHandler(OneloLoggingHandler())

The handler runs through the full capture pipeline (scope merge + scrub +
transport), so log messages with secrets get scrubbed just like any other
captured event.
"""
from __future__ import annotations

import logging
from typing import Any

from onelo.monitor._capture import add_breadcrumb, capture_exception, capture_message
from onelo.monitor._types import Breadcrumb


# Loggers we never instrument. Recursing on our own log output would burn
# the buffer and the API quota. Match by hierarchy prefix.
_IGNORED_LOGGERS: frozenset[str] = frozenset({
    "onelo",
    "onelo.monitor",
    "onelo.monitor.transport",
    "httpx",  # transport itself logs requests — would double-count
    "httpcore",
})


class OneloLoggingHandler(logging.Handler):
    """A ``logging.Handler`` that bridges Python logging to the monitor.

    Parameters
    ----------
    event_level:
        Records at or above this level are captured as events. Defaults to
        ``logging.ERROR``. Set to ``logging.CRITICAL`` to capture only
        the loudest failures.
    breadcrumb_level:
        Records at or above this level (and below ``event_level``) become
        breadcrumbs attached to the next captured event. Defaults to
        ``logging.INFO`` so warnings + info land as crumbs but debug is
        dropped.
    feature_name_prefix:
        Each event uses ``f"{prefix}{record.name}"`` as its feature name.
        Default ``"log:"`` so events show up as e.g. ``log:my_app.payments``
        in the dashboard.
    """

    def __init__(
        self,
        *,
        event_level: int = logging.ERROR,
        breadcrumb_level: int = logging.INFO,
        feature_name_prefix: str = "log:",
    ) -> None:
        super().__init__(level=min(event_level, breadcrumb_level))
        self.event_level = event_level
        self.breadcrumb_level = breadcrumb_level
        self.feature_name_prefix = feature_name_prefix

    def emit(self, record: logging.LogRecord) -> None:
        # Refuse to instrument records from the SDK itself.
        if _is_ignored_logger(record.name):
            return

        try:
            if record.levelno >= self.event_level:
                self._capture_event(record)
            elif record.levelno >= self.breadcrumb_level:
                self._record_breadcrumb(record)
            # Below breadcrumb_level — drop.
        except Exception:  # noqa: BLE001
            self.handleError(record)

    # ─── internals ─────────────────────────────────────────────────────

    def _capture_event(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        feature_name = f"{self.feature_name_prefix}{record.name}"
        level = _level_to_event_level(record.levelno)

        # If logger.exception(...) was used, exc_info is the tuple from sys.exc_info().
        if record.exc_info and record.exc_info[1] is not None:
            capture_exception(
                record.exc_info[1],
                feature_name=feature_name,
                meta={"log_message": message},
            )
            return

        capture_message(
            message,
            level=level,  # type: ignore[arg-type]
            feature_name=feature_name,
            attach_stacktrace=False,
        )

    def _record_breadcrumb(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        level = _level_to_event_level(record.levelno)
        crumb = Breadcrumb(
            category="log",
            message=message,
            timestamp=record.created,
            level=level,  # type: ignore[arg-type]
            data={"logger": record.name},
        )
        add_breadcrumb(crumb)


# ─── helpers ───────────────────────────────────────────────────────────────


def _is_ignored_logger(name: str) -> bool:
    if name in _IGNORED_LOGGERS:
        return True
    return any(name.startswith(prefix + ".") for prefix in _IGNORED_LOGGERS)


def _level_to_event_level(levelno: int) -> str:
    """Map a stdlib logging level number to an OTel/Sentry ``EventLevel`` string."""
    if levelno >= logging.CRITICAL:
        return "fatal"
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


__all__ = ["OneloLoggingHandler"]
