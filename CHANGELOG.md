# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
