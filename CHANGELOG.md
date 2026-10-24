# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0a1] — 2026-10-22

### Added

- **`onelo.store.list_plans()`** (#909). Returns the App's active plans as `OneloPlan` (plan key, name, short label, mode, currency, `price_amount` and `annual_price_amount` in minor units, trial days, sort order) from `GET /api/sdk/store/plans`. `plan` is the same key as `OneloUser.plan`. Secret key only: a publishable-key client raises `ValueError` before sending; a refused or failed request raises `OneloStoreError`.

## [0.6.0a4] — 2026-10-07

### Fixed

- **`identify()` no longer leaves the previous identity's feature snapshot in place** (#739). Changing the identity now clears the snapshot (fail-closed), re-arms `ready()` and asks for a full snapshot: the previous identity's `config_version` was sent as `since_version`, and the backend answered `up_to_date`, so the new identity never got its own snapshot. A stream event or poll answer for the previous identity is dropped, and the polling strategy polls the new identity at once. Call `onelo.ready()` after `identify()` before gating. `features.for_user()` is unchanged.

## [0.6.0a3] — 2026-10-03

### Changed

- **`require_email_verified` now passes verified users** (#819). The backend reports `email_verified` on `/api/sdk/auth/user` since Email Verification shipped, so the option no longer refuses everyone. The README says when users are verified: Email verification Optional or Required in the parent App's Auth → Sign-up, or an OAuth, Magic Link or e-mailed password reset sign-in. No code change.

## [0.6.0a2] — 2026-09-29

### Fixed

- **The Features Event Stream no longer reconnects every two minutes on a healthy connection** (#368). The server keeps the stream alive with a `: keepalive` comment every 30 s, but `httpx_sse` discards comment lines inside its decoder, so the staleness watchdog saw only named events and tore down an idle stream after 90 s of "silence" — about 30 reconnects an hour. The SDK now decodes the stream itself and counts every keepalive comment as proof of life; comments still reach no event handler. A stream that delivers nothing for 90 s is still reconnected.

## [0.6.0a1] — 2026-09-25

### Changed

- **Per-user Features calls require the secret key** (#520). `features.for_user(user_id)` and `identify(user_id)` now raise `ValueError` when the client was configured with a publishable key (`onelo_pk_…`). The backend trusts a caller-supplied user id only from a secret-key caller, so a publishable-key call naming a user was as spoofable as a client. The message names the fix: use your secret key (`onelo_sk_…`) on the server. Anonymous calls (`feature()`, `declare()`, `ready()`, `identify(None)`) keep working with a publishable key; secret-key calls are unchanged.
- `publishable_key=` on `Onelo(...)` is documented as deprecated for servers — pass `secret_key=` (or `key=`). `FeaturesClient(publishable_key=...)` is renamed to `key=`; the old name is still accepted.

## [0.5.0a44] — 2026-09-05

### Changed

- `GET /api/sdk/features/poll` now sends its credential as the `X-Publishable-Key` request header instead of the `?key=` query parameter — the same move 0.5.0a42 made for the Event Stream (ADR-0003, #105). The poll fires on a timer, so the raw key (typically a Secret Key on a server) was written to the reverse proxy's access.log on every tick; from 2027-03-31 the backend refuses `?key=` outright. The key is sent only as the header, never both.

## [0.5.0a43] — 2026-09-05

### Fixed

- Two monitor tests asserted a **byte substring** of the serialized request body (`b'"publishableKey":"k"'`) and so only passed on httpx >= 0.28, which changed `encode_json` to compact separators. They now parse the JSON and assert on values, which is correct across the whole declared `httpx>=0.27,<1.0` range. The transport is unchanged — serialization belongs to httpx, and the byte shape was never a contract. The remaining byte-substring assertions in the suite were converted for the same reason; one of them turned out to be asserting the wrong thing (a thread exception's message matched anywhere in the body, where the event's `featureName` is `thread_uncaught` and the message lives in `meta.exception.message`).

### Added

- The package declares its own test environment: a `dev` extra (`pip install -e '.[dev]'`). It no longer borrows `backend/venv`, which shadows `src/` with a stale installed copy and pins httpx to the floor of the supported range.
- A CI job (`onelo-python`) that runs the suite at the floor **and** the ceiling of the declared httpx range. Recorded as [ADR-0006](../../docs/adr/0006-onelo-python-tests-its-own-declared-httpx-range.md).

## [0.5.0a42] — 2026-09-05

### Changed

- The Features Event Stream (`GET /api/sdk/features/stream`) now sends the publishable key as the `X-Publishable-Key` **request header** instead of the `?key=` query parameter, per [ADR-0003](../../docs/adr/0003-event-stream-credential-header-for-every-sdk-but-browser-js.md). The raw key no longer appears in the stream URL, so it no longer lands in a reverse proxy's access log on every connect. The key is sent *only* as the header — the backend resolves `?key=` first, so sending both would keep the connection stamped `Deprecation` / `Sunset: 2027-03-31`, which a header-authenticated connection is deliberately not given.
- No Stream Token is used. Per ADR-0003 that credential stays scoped to browser `onelo-js`, the one client whose native `EventSource` cannot set a request header.
- Everything else about the stream is unchanged: event multiplexing, heartbeat/keepalive handling, the staleness healthcheck, full-jitter reconnect backoff, forward-compatible handling of unknown events, and the terminal (never-retried) stop on a genuine `401`/`403`.
- `GET /api/sdk/features/poll` still sends `?key=` and is migrated separately.

## [0.5.0a15] — 2026-06-11

### Added

- `discovery_key` now also opens a second, lightweight SSE presence stream authorized with the test key (alongside the primary stream). The backend derives presence env from the connecting key, so a `sk_live` primary + `pk_test` discovery setup finally shows the instance as **online under the Test environment** in the dashboard.

### Fixed

- The dashboard's "Discover Features" click now reaches `sk_live`-primary processes. The backend env-scopes the `discovery_requested` broadcast to test-env SSE subscribers, so the listener on the live primary stream could never fire; the event is now received on the discovery presence stream and triggers the immediate batch-ping as designed.

### Changed

- Documentation: `identify()` is now clearly documented as setting a single **process-global** identity (full SSE reconnect per switch) — suitable for single-user processes (CLI, workers, single-tenant services), not for per-request use in multi-user backends, where concurrent requests would race on the shared identity. Behavior unchanged.
- A rejected batch-ping (HTTP 4xx — e.g. a test key bound to a different device/app) is now logged at **warning** level with the backend's error code. Previously the response status wasn't checked at all, so binding rejections were completely silent and discovery appeared to "do nothing".

## [0.3.0a2] — 2026-05-06

### Fixed

- Fix SSE consumer disconnecting every 5s due to read timeout shorter than server heartbeat. The streaming HTTP client now uses `read=None` while keeping connect/write/pool at `request_timeout`; liveness is handled by the backend's 30s heartbeat and the existing reconnect-on-error loop.

## [0.1.0a1] — 2026-05-05

### Added

- `Onelo` client with sync user-facing API.
- `features.feature(name)` returning a `Feature` with `is_enabled`, `is_visible`, and per-status booleans.
- `features.declare(names)` for upfront feature registration.
- `identify(user_id)` for per-user targeting.
- `ready(timeout)` cold-start gate.
- `close()` and context manager support.
- Default SSE-per-worker strategy with polling fallback (`strategy="polling"`).
- Fork detection via `os.register_at_fork` for uvicorn/gunicorn workers.
- Thread-safe cache with fail-closed semantics.
- Reconnect with exponential backoff (1, 2, 4, 8, 16, 30s).
- Forward-compatible: unknown SSE event types are ignored.
- Logging via standard Python `logging` module under the `onelo` logger.
