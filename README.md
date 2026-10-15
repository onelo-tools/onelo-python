# onelo-python

The Onelo SDK for Python backends — verify users, gate features by plan, and monitor errors. Ships adapters for FastAPI, Django, Flask, Litestar and any ASGI/WSGI app.

Part of [Onelo](https://onelo.tools): hosted sign-in, a paywall on **your own Stripe** account, plan-gated feature flags, uptime monitoring, in-app feedback, a public roadmap and a waitlist.

## Install

```bash
pip install git+https://github.com/onelo-tools/onelo-python.git
```

For staging:

```bash
pip install git+https://github.com/onelo-tools/onelo-python.git@staging
```

For a pinned version:

```bash
pip install git+https://github.com/onelo-tools/onelo-python.git@v0.1.0
```

## FastAPI Quickstart (auth)

Verify Onelo user tokens on your backend with a single dependency. Install with the
`fastapi` extra:

```bash
pip install 'onelo[fastapi] @ git+https://github.com/onelo-tools/onelo-python.git'
```

```python
import os
from fastapi import FastAPI, Depends
from onelo import Onelo
from onelo.fastapi import RequireUser, OneloUser

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],  # onelo_sk_live_…  (NEVER commit)
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)
require_user = RequireUser(onelo)

app = FastAPI()

@app.get("/me")
async def me(user: OneloUser = Depends(require_user)):
    return {"id": user.id, "email": user.email}
```

`RequireUser` handles header parsing, in-process caching (default 30s TTL), retries
on 5xx, and maps failures to the right HTTP status (`401` invalid, `403` gate,
`503` upstream down). Optional knobs: `accept_query_token`, `require_email_verified`,
`require_plan=["pro"]`, custom `cache`, `on_auth_event` for audit logging.
`require_email_verified` refuses users who have not verified their address; turn
Email verification on (Optional or Required) in the parent App's Auth → Sign-up,
or every password user stays unverified. OAuth, Magic Link and e-mailed password
resets count as verified.

For routes where auth is optional, swap in `OptionalUser` — it returns `None`
on missing/invalid tokens but still surfaces 503 if Onelo is unreachable.

For non-FastAPI integrations (websockets, Celery), call the low-level
`await verify_token(onelo, token)` from `onelo.auth`.

## Quickstart

```python
import os
from onelo import Onelo

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],  # onelo_sk_live_…  (NEVER commit)
    api_url="https://api.onelo.tools",
)

# Optional: register all known features upfront so they appear in the dashboard
onelo.features.declare(["chat-stream", "voice-stream", "game-think"])

# Optional (single-user processes only, e.g. a CLI or worker): identify the
# user for per-user targeting. Requires the secret key — see below.
onelo.identify("user-123")
onelo.ready(timeout=2.0)  # wait for THIS user's snapshot before gating

# Sync feature check — no `await` needed
if onelo.features.feature("chat-stream").is_enabled:
    do_the_thing()
```

## FastAPI integration

```python
from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, Depends, HTTPException
from onelo import Onelo

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],  # onelo_sk_live_…  (NEVER commit)
    api_url="https://api.onelo.tools",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    onelo.features.declare([...])  # your feature names
    onelo.ready(timeout=2.0)        # block up to 2s for first refresh
    yield
    onelo.close()

app = FastAPI(lifespan=lifespan)

@app.post("/chat/stream")
async def chat_stream():
    if not onelo.features.feature("chat-stream").is_enabled:
        raise HTTPException(404, "Feature not available")
    return ...
```

Per-user or per-plan gating resolves for the Onelo Auth user the request
verified — `user: OneloUser = Depends(RequireUser(onelo))` from `onelo.fastapi`
— with `uf = await onelo.features.for_user(user.id)`. Onelo requires Onelo
Auth, and never call the process-global `identify()` per request — concurrent
requests would race.

## Flask integration

Auth for Flask views uses the synchronous `verify_token_sync` under the hood
and exposes a decorator factory. Install the `flask` extra:

```bash
pip install 'onelo[flask] @ git+https://github.com/onelo-tools/onelo-python.git@staging' @ git+https://github.com/onelo-tools/onelo-python.git@staging'
```

```python
import os
from flask import Flask
from onelo import Onelo
from onelo.flask import require_user, optional_user, OneloUser

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)
app = Flask(__name__)

@app.get("/me")
@require_user(onelo)
def me(user: OneloUser):
    return {"id": user.id, "email": user.email}

@app.get("/public")
@optional_user(onelo)
def public(user):
    return {"signed_in": user is not None}
```

Status code mapping, gates (`require_email_verified`, `require_plan`),
caching, retry policy, and `on_auth_event` semantics match the FastAPI
adapter — see the [FastAPI Quickstart (auth)](#fastapi-quickstart-auth)
section above. The decorated view receives the verified user as a
keyword argument named `user` (or `None` for `optional_user` when the
token is missing/invalid).

## Django + DRF (auth)

Verify Onelo user tokens in Django views — two flavours, same status code
mapping (`401` / `403` / `503`) as the FastAPI / Flask adapters. Install with
the `django` extra:

```bash
pip install 'onelo[django] @ git+https://github.com/onelo-tools/onelo-python.git@staging' @ git+https://github.com/onelo-tools/onelo-python.git@staging'
```

### Django REST Framework

```python
# myapp/auth.py
import os
from onelo import Onelo
from onelo.django import OneloAuthenticationFactory

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],  # NEVER commit
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)
OneloAuthentication = OneloAuthenticationFactory(onelo)

# settings.py
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["myapp.auth.OneloAuthentication"],
}

# views.py
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["GET"])
def me(request):
    # request.user is an OneloDjangoUser — id, email, plan, etc.
    return Response({"id": request.user.id, "email": request.user.email})
```

`OneloAuthenticationFactory` accepts the same gates as the FastAPI adapter:
`require_email_verified=True`, `require_plan=["pro"]`, `cache_ttl=...`,
`on_auth_event=...`. It returns `None` (DRF convention) when the
`Authorization` header is missing so other auth classes still get a chance.

### Plain Django (no DRF)

```python
# settings.py
import os
from onelo import Onelo

ONELO_CLIENT = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)
MIDDLEWARE = [
    ...,
    "onelo.django.OneloAuthMiddleware",
]

# views.py
from django.http import JsonResponse
from onelo.django import require_onelo_user

@require_onelo_user
def me(request):
    return JsonResponse({"id": request.onelo_user.id})
```

The middleware never raises — it just populates `request.onelo_user`
(or leaves it `None`). The `@require_onelo_user` decorator is the strict
gate that returns `401` / `403` / `503` JSON responses.

## Django integration (feature flags)

In your `apps.py`:

```python
import os
from django.apps import AppConfig
from onelo import Onelo

onelo: Onelo | None = None

class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        global onelo
        onelo = Onelo(
            secret_key=os.environ["ONELO_SECRET_KEY"],
            api_url="https://api.onelo.tools",
        )
        onelo.ready(timeout=2.0)
```

In your views:

```python
from .apps import onelo

def chat_view(request):
    if not onelo.features.feature("chat-stream").is_enabled:
        return HttpResponseNotFound()
    return ...
```

Per-user or per-plan gating needs an async view, because `for_user()` is
async: `uf = await onelo.features.for_user(request.onelo_user.id)`, with the
Onelo Auth user that `OneloAuthMiddleware` puts on `request.onelo_user`. Onelo
requires Onelo Auth, and never call the process-global `identify()` per
request — concurrent requests would race.

## Litestar integration

Install the Litestar extra:

```bash
pip install 'onelo[litestar] @ git+https://github.com/onelo-tools/onelo-python.git@staging'
```

Two patterns are supported. The **guard** pattern enforces auth without
injecting a typed user — read it from `connection.scope["user"]`:

```python
import os
from litestar import Litestar, get
from litestar.connection import ASGIConnection
from onelo import Onelo
from onelo.litestar import OneloGuardFactory

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)
require_user = OneloGuardFactory(onelo)

@get("/me", guards=[require_user])
async def me(request: ASGIConnection) -> dict:
    user = request.scope["user"]
    return {"id": user.id, "email": user.email}

app = Litestar(route_handlers=[me])
```

The **dependency injection** pattern (recommended) gives you a typed
`OneloUser` directly:

```python
from litestar import Litestar, get
from onelo.litestar import provide_onelo_user, OneloUser

@get("/me")
async def me(user: OneloUser) -> dict:
    return {"id": user.id, "email": user.email}

app = Litestar(
    route_handlers=[me],
    dependencies={"user": provide_onelo_user(onelo)},
)
```

Use `provide_optional_onelo_user(onelo)` for routes that should accept
anonymous traffic — missing/invalid tokens resolve to `None` instead of
401/403, while 503 (auth backend unreachable) still propagates.

Status codes mirror the FastAPI integration: **401** missing/invalid,
**403** plan/email gate failed, **503** Onelo backend unreachable.

## Universal ASGI/WSGI middleware

When your framework isn't covered above, use the universal middleware.
It's less ergonomic than the per-framework adapters (no automatic
401/403/503 mapping — your handler decides) but works in **any** ASGI 3
or WSGI app.

### ASGI (FastAPI / Starlette / Litestar / Quart / any ASGI 3)

```python
from fastapi import FastAPI, HTTPException, Request
from onelo import Onelo
from onelo.asgi import OneloAsgiMiddleware

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)

app = FastAPI()
app.add_middleware(OneloAsgiMiddleware, onelo=onelo)

@app.get("/me")
async def me(request: Request):
    user = request.scope.get("onelo_user")
    if not user:
        raise HTTPException(401)
    return {"id": user.id, "email": user.email}
```

### WSGI (Flask / Django / Pyramid / Bottle / any WSGI)

```python
from flask import Flask, request, jsonify
from onelo import Onelo
from onelo.wsgi import OneloWsgiMiddleware

onelo = Onelo(
    secret_key=os.environ["ONELO_SECRET_KEY"],
    api_url=os.environ["ONELO_API_URL"],  # https://api.onelo.tools
)

app = Flask(__name__)
app.wsgi_app = OneloWsgiMiddleware(app.wsgi_app, onelo=onelo)

@app.get("/me")
def me():
    user = request.environ.get("onelo.user")
    if not user:
        return jsonify(error="unauthorized"), 401
    return jsonify(id=user.id, email=user.email)
```

The middleware **never raises on auth failure** — it simply sets
`scope['onelo_user']` (ASGI) or `environ['onelo.user']` (WSGI) to either
an `OneloUser` instance or `None`. Your handler decides the response.
Both middlewares accept the same options as the per-framework adapters
(`cache_ttl`, `retry_attempts`, `retry_total_timeout`, `header_name`,
`on_auth_event`).

For framework-idiomatic auth with proper 401/403/503 mapping, prefer
`onelo.fastapi`, `onelo.flask`, `onelo.django`, or `onelo.litestar`.

## API reference

### `Onelo(secret_key=..., api_url=..., strategy="auto", poll_interval=30, request_timeout=5, log_level=None)`

Creates an SDK instance. Spawns one background daemon thread per instance.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `secret_key` | `str` | one key required | Your server secret key, `onelo_sk_live_…` / `onelo_sk_test_…`. Required for auth verification and for per-user Features calls. `key=` is also accepted (kind detected from the prefix). |
| `publishable_key` | `str` | — | **Deprecated for servers.** Still accepted, but with a publishable key auth verification refuses to run and per-user Features calls raise `ValueError`. Anonymous `feature()` / `declare()` keep working. |
| `api_url` | `str` | **required** | Backend URL — `https://api.onelo.tools` in production, `https://st.backend.onelo.tools` for staging. No default: the host differs per environment, so passing it explicitly (from your env) is the only safe option. |
| `strategy` | `"auto" \| "sse" \| "polling"` | `"auto"` | `"auto"` resolves to `"sse"` in v1. |
| `poll_interval` | `float` | `30.0` | Seconds between polls. Only used when `strategy="polling"`. |
| `request_timeout` | `float` | `5.0` | HTTP request timeout in seconds. |
| `log_level` | `str \| None` | `None` | Sets `logging.getLogger("onelo")` level. |

### `onelo.identify(user_id)`

Set the process-global user_id for per-user feature targeting. Sync,
idempotent. For single-user processes only; a multi-user backend uses
`await onelo.features.for_user(user_id)`.

Changing the identity drops the previous identity's snapshot at once
(`feature()` answers hidden meanwhile), re-arms `ready()` and fetches the new
identity's full snapshot. Call `onelo.ready()` after `identify()` before gating
on features. A late answer for the previous identity is discarded.

### Per-user calls require the secret key

`features.for_user(user_id)` and `identify(user_id)` send a user id the backend
trusts only from a **secret-key** caller. A publishable key is public, so a
client configured with one raises `ValueError` on these calls ("use your secret
key (onelo_sk_…) on the server") instead of sending a spoofable request.
Anonymous calls (`feature()`, `declare()`, `ready()`, `identify(None)`) work
with either key.

### `onelo.features.feature(name) -> Feature`

Returns a `Feature` object. Cache miss returns `status="hidden"` (fail-closed).

### `onelo.features.declare(names)`

Send a batch-ping to register the listed feature names so they appear in the dashboard registry without waiting for code paths to execute.

### `onelo.ready(timeout=1.5) -> bool`

Block until the first SSE event (or polling response) for the current identity is received. Returns `True` if event received within timeout, `False` otherwise. `identify()` with a different id re-arms it.

### `onelo.close()`

Stop the background thread and release resources. Idempotent. Auto-registered via `atexit`.

### `Feature`

Read-only value object with these properties:

- `feature.name: str`
- `feature.status: str` — wire status: `"enabled"`, `"new"`, `"beta"`, `"coming_soon"`, `"greyed"`, `"hidden"`
- `feature.is_enabled: bool` — `True` for `enabled`, `new`, `beta`
- `feature.is_visible: bool` — `True` for everything except `hidden`
- `feature.is_greyed`, `feature.is_new`, `feature.is_beta`, `feature.is_coming_soon: bool`

## How it works

One background daemon thread per `Onelo` instance holds a long-lived SSE connection to the backend. Updates land in a thread-safe in-memory cache. User code reads from the cache synchronously (~1µs) — never blocks on the network.

uvicorn/gunicorn fork workers? Each worker gets its own background thread automatically via `os.register_at_fork`. No special configuration.

## Development

The SDK has its own test environment — do **not** run its suite from
`backend/venv` (that venv carries a stale, non-editable `onelo` in
`site-packages` which shadows `src/`, and pins httpx to the floor of the range
this package supports):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -c "import onelo, httpx; print(onelo.__file__, httpx.__version__)"
.venv/bin/python -m pytest -q
```

`httpx>=0.27,<1.0` is tested at **both** ends of that range in CI
(`.github/workflows/ci.yml`, job `onelo-python`). Widening the range means
adding the new point to that matrix — see
[ADR-0006](../../docs/adr/0006-onelo-python-tests-its-own-declared-httpx-range.md).

## Links

- **Docs:** [onelo.tools/docs](https://onelo.tools/docs)
- **Dashboard:** [onelo.tools](https://onelo.tools) — your app's snippet comes pre-filled with your keys
- **HTTP contract spec:** `https://api.onelo.tools/api/sdk/docs`
- **Issues:** please report them on this repository

## License

MIT
