"""The Store module — ``onelo.store.list_plans()`` (#909).

Reads the App's plan catalog from ``GET /api/sdk/store/plans`` so a backend
can show plan prices without copying them from the dashboard. Secret key only:
the endpoint refuses a publishable key, so the call is refused before sending.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from onelo._http import create_async_client


@dataclass(frozen=True)
class OneloPlan:
    """One active plan of the App; amounts in minor units (1900 = 19.00).

    ``plan`` is the plan key — the same key as ``OneloUser.plan`` and feature
    plan rules.
    """

    plan: Optional[str]
    name: str
    short_label: Optional[str]
    mode: str
    currency: str
    price_amount: int
    annual_price_amount: Optional[int]
    trial_period_days: Optional[int]
    is_active: bool
    sort_order: int

    @classmethod
    def from_dict(cls, p: dict[str, Any]) -> "OneloPlan":
        return cls(
            plan=p.get("plan"),
            name=p["name"],
            short_label=p.get("short_label"),
            mode=p["mode"],
            currency=p["currency"],
            price_amount=p["price_amount"],
            annual_price_amount=p.get("annual_price_amount"),
            trial_period_days=p.get("trial_period_days"),
            is_active=bool(p.get("is_active")),
            sort_order=p["sort_order"],
        )


class OneloStoreError(Exception):
    """A non-2xx answer or network failure; ``status`` is None for network errors."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class StoreClient:
    def __init__(
        self,
        *,
        api_url: str,
        key: str,
        request_timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url
        self._key = key
        self._request_timeout = request_timeout
        self._transport = transport

    async def list_plans(self) -> list[OneloPlan]:
        """The App's active plans, ordered by sort order. Requires the secret key."""
        if not self._key.startswith("onelo_sk_"):
            raise ValueError(
                "onelo.store.list_plans() needs the secret key (onelo_sk_…) — call it from your server."
            )
        try:
            async with create_async_client(
                timeout=self._request_timeout, transport=self._transport,
            ) as client:
                res = await client.get(
                    f"{self._api_url}/api/sdk/store/plans",
                    headers={"X-Onelo-Secret-Key": self._key},
                )
        except httpx.HTTPError as exc:
            raise OneloStoreError(f"network error: {exc}") from exc
        if res.status_code >= 400:
            try:
                detail = res.json().get("detail")
            except ValueError:
                detail = None
            suffix = f" — {detail}" if detail else ""
            raise OneloStoreError(
                f"store/plans failed: HTTP {res.status_code}{suffix}", res.status_code,
            )
        return [OneloPlan.from_dict(p) for p in (res.json().get("plans") or [])]
