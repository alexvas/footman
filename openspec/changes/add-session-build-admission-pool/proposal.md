# Proposal

## Why

The current fixture-owned `mcp-build.lock` serializes independent build, preparation and provisioning work across pytest workers. A supported free-slot pool provides bounded concurrency while preserving deadlines and descriptor ownership.

## What Changes

- Replace the session's single admission destination with an explicit, immutable session-owned pool shared by all xdist workers.
- Acquire any free slot; retain a bounded concurrency limit and original absolute deadlines.
- Resolve one conservative CPU/worker-bounded capacity in the controller and propagate it to workers, including replacement workers.
- **BREAKING (internal test support):** Remove `build_slot` and migrate every caller to immutable `AdmissionPool.acquire(deadline=...)`; represent single-slot admission with capacity one. Remove the legacy session-path configuration surface: `BUILD_SLOT_PATH`, `_wheel_build_slot_path`, module-global admission monkeypatch wiring and tests importing/calling `_mcp_build_admission.__wrapped__`; preserve their behavioral coverage through supported fixtures/callers.
- Keep existing heavy-work boundaries, including session setup before autouse fixtures and mandatory workspace admission. Retain intentional attribution-only bypasses explicitly at their callers.
- Expose an optional `system=` OS boundary for fault injection through real filesystem-owner paths and controlled time.
- Deliver one public-seam behavioral slice at a time through RED → GREEN → INTROSPECT → VALIDATE, with atomic design, test, implementation, review and verification tasks.
- Add deterministic policy, real interprocess concurrency, security and lifecycle fault tests; update the documented single-file contract. Require every mandated check in the complete green gate to pass.
- Compare an unchanged full gate before and after integration with durable evidence.

## Capabilities

### New Capabilities

- `test-build-admission`: Session-scoped bounded admission for independent test build and provisioning work, including free-slot acquisition, deadlines and owner-safe release.

### Modified Capabilities

The existing `dispatch-deadline-verification` capability retains its production dispatcher verification scope. This change adds the separate test-build-admission capability.

## Impact

- Test support: `tests/mcp_wheel_support.py`, `tests/conftest.py`, importable `tests/admission_plugin.py` for shared session hooks/fixture registration, `tests/admission_support.py`, host build/acceptance support and their admission contract tests.
- Documentation: the admission section of `docs/testing.md` and any affected lifecycle links/inventories.
- New focused pool tests and verification summaries, using the existing runtime dependencies and production runner interface.
- Existing six-collector coverage, isolated build destinations, external-results scheduling rules, production timeouts and cleanup policy remain unchanged.
