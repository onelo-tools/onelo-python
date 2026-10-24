"""onelo.store.list_plans() — the App's plans with prices (#909)."""
import asyncio

import httpx
import pytest

from onelo import OneloPlan, OneloStoreError
from onelo._store import StoreClient


def _client(handler, key="onelo_sk_live_x") -> StoreClient:
    return StoreClient(api_url="https://example.com", key=key, transport=httpx.MockTransport(handler))


def test_list_plans_sends_secret_key_and_maps():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/sdk/store/plans"
        assert request.headers["X-Onelo-Secret-Key"] == "onelo_sk_live_x"
        return httpx.Response(200, json={"plans": [{
            "plan": "pro", "name": "Pro", "short_label": None, "mode": "subscription",
            "currency": "eur", "price_amount": 1900, "annual_price_amount": 19000,
            "trial_period_days": 14, "is_active": True, "sort_order": 2,
        }]})

    plans = asyncio.run(_client(handler).list_plans())
    assert plans == [OneloPlan(
        plan="pro", name="Pro", short_label=None, mode="subscription", currency="eur",
        price_amount=1900, annual_price_amount=19000, trial_period_days=14,
        is_active=True, sort_order=2,
    )]


def test_publishable_key_refused_before_request():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    with pytest.raises(ValueError, match="secret key"):
        asyncio.run(_client(handler, key="onelo_pk_live_x").list_plans())


def test_refusal_raises_store_error_with_status():
    async def handler(request):
        return httpx.Response(403, json={"detail": "nope"})

    with pytest.raises(OneloStoreError) as exc:
        asyncio.run(_client(handler).list_plans())
    assert exc.value.status == 403
