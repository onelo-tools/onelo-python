"""Propagate monitor scope across process / queue boundaries.

Background jobs (Celery, RQ, Cloud Tasks, BullMQ-via-RPC) run outside the
request that scheduled them — so the user_id / tags / breadcrumbs that
were on the request scope never reach the worker. This module gives you
two helpers:

  - ``carrier()``  → serialise the active isolation scope to a dict
  - ``continue_trace(carrier)`` → context manager / decorator that
    re-applies that scope to the worker side

Wire pattern:

    from onelo import monitor

    @celery.task
    @monitor.continue_trace_task
    def my_task(...): ...

    # Producer side:
    my_task.apply_async(args=(...,), headers={"onelo": monitor.carrier()})

The carrier is small (a few hundred bytes typically — only ``user`` +
``tags`` + ``trace_id``, no breadcrumbs by default since they would bloat
every queue message). Pass ``include_breadcrumbs=True`` if you need them.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from onelo.monitor._scope import (
    Scope,
    _isolation_scope_var,
    get_global_scope,
    get_isolation_scope,
)
from onelo.monitor._types import Breadcrumb


F = TypeVar("F", bound=Callable[..., Any])

_log = logging.getLogger("onelo.monitor.propagation")


# ─── HMAC for cross-process carrier integrity ──────────────────────────────
#
# When a Celery / RQ / Cloud Tasks worker reads a queue message, it has no
# way to verify the producer was on the same tenant — anyone with publish
# access to the broker can forge a carrier with ``user.id = "victim"``.
# We sign the carrier with a shared secret (the publishable key works:
# producer and worker both have it because they're the same Onelo app).
# The signature is not authentication of the producer's identity, but it
# is integrity proof: a different tenant's app can't mint a carrier that
# decodes cleanly on this worker, because they don't have this app's key.
#
# Use ``carrier(key=…)`` on the producer and ``continue_trace(payload, key=…)``
# on the worker. Without a key, the carrier is unsigned and accepted as
# "best-effort" (back-compat with single-tenant deployments).

_HMAC_KEY_NAME = "_sig"
_HMAC_PAYLOAD_KEY = "_data"


def _sign(data: dict[str, Any], key: str) -> str:
    """HMAC-SHA256 of the canonical JSON encoding."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _verify(envelope: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Returns the inner ``_data`` dict if HMAC matches, ``None`` otherwise."""
    data = envelope.get(_HMAC_PAYLOAD_KEY)
    sig = envelope.get(_HMAC_KEY_NAME)
    if not isinstance(data, dict) or not isinstance(sig, str):
        return None
    expected = _sign(data, key)
    return data if hmac.compare_digest(expected, sig) else None


def carrier(*, include_breadcrumbs: bool = False, key: str | None = None) -> dict[str, Any]:
    """Serialise the active isolation scope into a JSON-safe dict.

    Add the result to your task headers / payload, then call
    ``continue_trace(carrier_dict)`` on the worker side.

    Privacy:
      - Only ``user["id"]`` is forwarded (never ``email`` / ``username`` /
        arbitrary user fields). The carrier ends up in the broker's payload
        — possibly persisted to disk (Redis AOF, SQS log) — and forwarding
        full ``user`` would put PII in places that don't have a DPA.
      - Tags / contexts / transaction / breadcrumbs all run through the
        same PII scrubber the wire transport uses, so secrets accidentally
        captured into a tag or breadcrumb don't escape into the queue.
    """
    from onelo.monitor._scrub import scrub_meta, scrub_text

    scope = get_isolation_scope()
    out: dict[str, Any] = {
        "trace_id": uuid.uuid4().hex,  # for correlation in dashboards
    }
    if scope.user:
        # Whitelist: ``id`` only. Email / username / metadata stay on-host.
        uid = scope.user.get("id")
        if uid is not None:
            out["user"] = {"id": str(uid)}
    if scope.tags:
        scrubbed_tags = scrub_meta(dict(scope.tags))
        if scrubbed_tags:
            out["tags"] = scrubbed_tags
    if scope.contexts:
        scrubbed_ctx = scrub_meta({k: dict(v) for k, v in scope.contexts.items()})
        if scrubbed_ctx:
            out["contexts"] = scrubbed_ctx
    if scope.transaction:
        cleaned_tx = scrub_text(scope.transaction)
        if cleaned_tx:
            out["transaction"] = cleaned_tx
    if include_breadcrumbs and len(scope.breadcrumbs) > 0:
        # Breadcrumb data already passes through the URL/value scrubber at
        # construction time (see ``Breadcrumb.http``). Run scrub_meta over
        # the serialised list anyway — defense in depth.
        crumbs = scope.breadcrumbs.to_event_data()
        scrubbed = scrub_meta({"_": crumbs})
        if scrubbed and "_" in scrubbed:
            out["breadcrumbs"] = scrubbed["_"]

    # If a shared key is provided, return a signed envelope; the worker
    # side will verify and unwrap. Without a key, return the raw dict for
    # back-compat with single-tenant deployments where forging a carrier
    # has no realistic threat surface.
    if key:
        return {
            _HMAC_PAYLOAD_KEY: out,
            _HMAC_KEY_NAME: _sign(out, key),
        }
    return out


@contextmanager
def continue_trace(
    data: dict[str, Any] | None,
    *,
    key: str | None = None,
) -> Iterator[Scope]:
    """Restore a serialised scope for the duration of the ``with`` block.

    Forks a fresh isolation scope from the global one (so we don't pollute
    sibling work) and applies whatever the producer put in the carrier.

    If ``key`` is provided, the carrier is treated as a signed envelope
    and the HMAC is verified before any field is applied. A bad signature
    is silently rejected (the body still runs, but with an empty isolation
    scope) so workers don't crash on a single forged message.
    """
    parent = get_global_scope()
    forked = parent.clone()
    token = _isolation_scope_var.set(forked)
    try:
        payload = _unwrap_carrier(data, key=key)
        if payload:
            _apply_carrier(forked, payload)
        yield forked
    finally:
        _isolation_scope_var.reset(token)


def _unwrap_carrier(
    data: dict[str, Any] | None,
    *,
    key: str | None,
) -> dict[str, Any] | None:
    """Return the trusted carrier payload, or ``None`` if rejected.

    Acceptance rules:
      - ``data`` is ``None``                       → ``None`` (legacy task)
      - ``key`` not provided                        → trust as-is
                                                       (back-compat / single-tenant)
      - ``data`` is a signed envelope + key matches → unwrap and trust
      - ``data`` is a signed envelope + key mismatch → reject (log + return None)
      - ``data`` looks signed (has ``_sig``) but no key was provided
                                                   → reject (server config bug;
                                                       we can't verify so we
                                                       refuse to trust)
    """
    if data is None:
        return None
    looks_signed = _HMAC_KEY_NAME in data and _HMAC_PAYLOAD_KEY in data
    if key is None:
        if looks_signed:
            _log.warning(
                "monitor: continue_trace received a signed carrier but no key "
                "was supplied to verify it; rejecting"
            )
            return None
        return data
    if not looks_signed:
        _log.warning(
            "monitor: continue_trace was given a key but the carrier is not "
            "signed; rejecting (producer must use carrier(key=...))"
        )
        return None
    verified = _verify(data, key)
    if verified is None:
        _log.warning("monitor: continue_trace HMAC verification failed; rejecting")
    return verified


def continue_trace_task(func: F | None = None, *, key: str | None = None) -> F:
    """Decorator for Celery / RQ task functions.

    Looks for a ``carrier`` (or first dict-shaped kwarg named ``onelo``) in
    the task arguments, restores the scope, and runs the body inside it.

    For Celery 5+:

        @celery_app.task(bind=True)
        @continue_trace_task
        def my_task(self, *args, **kwargs):
            ...

    The producer attaches the carrier through ``headers``:

        my_task.apply_async(args=..., headers={"onelo": monitor.carrier()})

    The decorator inspects the task instance (Celery passes ``self`` when
    ``bind=True``) for ``self.request.headers`` and pulls the carrier from
    there. For RQ / plain Python jobs the producer passes ``onelo=carrier``
    as a kwarg directly.
    """

    # Support both ``@continue_trace_task`` and ``@continue_trace_task(key=...)``.
    def _decorate(inner: F) -> F:
        @functools.wraps(inner)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            data = _extract_carrier(args, kwargs)
            with continue_trace(data, key=key):
                return inner(*args, **kwargs)
        return wrapper  # type: ignore[return-value]

    if func is None:
        return _decorate  # type: ignore[return-value]
    return _decorate(func)


# ─── internals ──────────────────────────────────────────────────────────────


def _apply_carrier(scope: Scope, data: dict[str, Any]) -> None:
    user = data.get("user")
    if isinstance(user, dict):
        scope.set_user(user)

    tags = data.get("tags")
    if isinstance(tags, dict):
        for k, v in tags.items():
            scope.set_tag(str(k), str(v))

    contexts = data.get("contexts")
    if isinstance(contexts, dict):
        for k, v in contexts.items():
            if isinstance(v, dict):
                scope.set_context(str(k), v)

    transaction = data.get("transaction")
    if isinstance(transaction, str):
        scope.set_transaction(transaction)

    trace_id = data.get("trace_id")
    if isinstance(trace_id, str):
        scope.set_tag("trace_id", trace_id)

    breadcrumbs = data.get("breadcrumbs")
    if isinstance(breadcrumbs, list):
        for c in breadcrumbs:
            if not isinstance(c, dict):
                continue
            scope.add_breadcrumb(Breadcrumb(
                category=c.get("category", "info"),  # type: ignore[arg-type]
                message=str(c.get("message", "")),
                timestamp=float(c.get("ts", 0)),
                level=c.get("level", "info"),  # type: ignore[arg-type]
                data=c.get("data"),
            ))


def _extract_carrier(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Find a carrier dict in the task's call args. Recognises:

      - ``kwargs["onelo"]`` (RQ / direct call convention)
      - ``args[0].request.headers["onelo"]`` (Celery ``bind=True``)

    Returns ``None`` when no carrier is found — the wrapped task still
    runs, just without inherited scope.
    """
    onelo_kwarg = kwargs.get("onelo")
    if isinstance(onelo_kwarg, dict):
        # Pop so the wrapped function doesn't receive an unexpected kwarg.
        kwargs.pop("onelo")
        return onelo_kwarg

    if args:
        bound_self = args[0]
        request = getattr(bound_self, "request", None)
        if request is not None:
            headers = getattr(request, "headers", None)
            if isinstance(headers, dict):
                onelo_header = headers.get("onelo")
                if isinstance(onelo_header, dict):
                    return onelo_header
    return None


__all__ = ["carrier", "continue_trace", "continue_trace_task"]
