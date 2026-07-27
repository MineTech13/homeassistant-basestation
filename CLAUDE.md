# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (HACS) that controls Valve Index (V2) and HTC Vive (V1, untested/WIP —
see [#4](https://github.com/MineTech13/homeassistant-basestation/issues/4)) VR lighthouse base stations
directly over Bluetooth Low Energy — no SteamVR required. Domain: `basestation`. Lives entirely under
`custom_components/basestation/`.

## Commands

```bash
scripts/setup                            # pip install -e . (run once, needs a devcontainer/venv)
scripts/develop                          # runs `hass --config ./config --debug` (HA config dir auto-created
                                          # on first run via `hass --script ensure_config`)
scripts/debug                            # same, but under debugpy listening on 127.0.0.1:5678 (--wait-for-client)
scripts/lint                             # ruff format . && ruff check . --fix && mypy custom_components/basestation

ruff format --check .                    # CI's non-mutating format check
ruff check .                             # lint only
mypy custom_components/basestation       # type check only (mypy targets this dir specifically, not `.`)
mypy custom_components/basestation/device.py   # single-file type check
```

There is no test suite (no `tests/` dir, nothing in CI runs `pytest`). CI (`.github/workflows/validate.yml`)
only runs: HACS validation, hassfest validation, `ruff format --check`, `ruff check`, `mypy`. Treat "lint +
mypy clean" as the bar for correctness, not test coverage — there isn't any.

`pyproject.toml` pins `ruff.lint.select = ["ALL"]` with an explicit ignore list — check that list before
adding `# noqa` suppressions, the rule may already be globally disabled. Line length 120, `datetime` imports
are banned by `flake8-import-conventions` (use `homeassistant.util.dt` instead, per HA convention).

## Architecture

**Flow:** `config_flow.py` (discovery/manual setup) → `device.py` (`get_basestation_device()` picks
`ValveBasestationDevice` or `ViveBasestationDevice` by name prefix / explicit type) → `__init__.py` wires the
device into two `DataUpdateCoordinator`s (`coordinator.py`) → `switch.py`/`sensor.py`/`button.py` render
coordinator data as entities via `CoordinatorEntity`.

**Two coordinators per device**, both created in `async_setup_entry`:
- `BasestationCoordinator` — polls power state at `power_state_scan_interval` (default 60s). Switches to
  `fast_polling_interval` (default 5s) automatically while the device is mid-boot
  (`STARTING_UP`/`BOOTING_1`/`BOOTING_2`), and falls back to the normal interval if that boot state goes
  stale without being reconfirmed by an actual read (`last_power_state_age`) — otherwise a device that
  stopped responding mid-boot would poll at the fast interval forever.
- `BasestationInfoCoordinator` — polls static info (firmware/model/hardware/manufacturer/channel/pair_id) at
  `info_scan_interval` (default 1800s). Falls back to cached info on timeout/error rather than failing the
  update, so slow/flaky reads don't flap the info sensors.

**BLE connection management lives entirely in `device.py`**, in the `BasestationDevice` ABC (subclassed by
`ValveBasestationDevice` / `ViveBasestationDevice`). All reads/writes go through `async_ble_operation()`,
which holds a single `asyncio.Lock` per device (`_client_lock`) so state polling, info reads, and user
commands (turn on/off/standby/identify) never race each other on the same physical connection. V1 has no
readable state characteristic — `ViveBasestationDevice.update()` infers availability from advertisement
visibility, and `_is_on` is just tracked locally from the last command sent since there's nothing to read
back.

**Connections are shared/deduplicated per device**, not per-call: `_await_connection()` reuses one in-flight
`establish_connection()` task across concurrent-ish callers instead of racing multiple connection attempts
at the same device. See "Known issues" below for why this exists.

## Known issues

### ESPHome Bluetooth-proxy connection slots getting permanently stuck (multi-user reported, long-running)

Symptom: over time (hours), connection slots on a nearby ESPHome Bluetooth proxy get permanently occupied
and never freed, eventually starving all base stations behind that proxy and making them (and the whole
integration) unavailable. Reported by multiple users. Chased across many commits — see `git log` for
`coordinator.py`/`device.py` history, most notably `eb75c37`, `86a71e3`, `7477cae`.

**Root cause (confirmed against real HA + bleak DEBUG logs, 2026-07-27):** the integration wrapped
`establish_connection()` (from `bleak-retry-connector`) in its own `asyncio.timeout(connection_timeout)`.
`bleak-retry-connector` has its own internal retry/cleanup logic specifically designed to avoid this exact
failure mode (see its `BLEAK_SAFETY_TIMEOUT` comment referencing
[esp-idf#17452](https://github.com/espressif/esp-idf/issues/17452) — ESPHome proxies can free a slot too
early, racing a reconnect) — but that cleanup only runs if `establish_connection()` is allowed to fail
*naturally*. `asyncio.CancelledError` (what our own timeout injects) is not one of the exceptions its retry
loop catches, so cancelling it from outside skips that cleanup entirely and can strand the proxy slot no
matter how generous the timeout is. This was confirmed with an exact timestamp match in `bleak.log`: our
"Timeout contacting" log line fired ~200ms after bleak's own internal failure log, i.e. while bleak was still
inside its mandatory post-failure backoff (`wait_for_disconnect`), cutting it off mid-cleanup.

Two non-obvious `bleak_retry_connector` internals worth remembering here:
- `BLEAK_TIMEOUT = 20.0` is the timeout for one raw `client.connect()` call. `BLEAK_SAFETY_TIMEOUT = 60.0` is
  a separate, larger backstop wrapping connect *+ its own cleanup* — and it's applied **per attempt inside
  the retry loop**, not once across the whole call. An earlier fix (`eb75c37`) raised our own timeout to
  match `BLEAK_TIMEOUT` (20s) on the mistaken assumption that was the relevant constant; it wasn't — the
  outer 60s safety net was, and 20s left the outer timeout racing bleak's mandatory cleanup on essentially
  every slow connect.
- `establish_connection(..., max_attempts=1)` does **not** actually cap it at one internal try for transient
  / out-of-slots-style errors (e.g. `ESP_GATT_CONN_CONN_CANCEL`) — those increment a separate
  `transient_errors` counter capped at `MAX_TRANSIENT_ERRORS = 9`, which `max_attempts` doesn't touch. A
  single `establish_connection()` call can legitimately retry internally many times under sustained proxy
  congestion, for longer than any reasonably-sized external timeout.

**Current fix (unverified against real hardware yet — watch proxy slot counts after deploying):** stop
racing `establish_connection()` with an external timeout at all. `BasestationDevice._await_connection()`
launches it as a tracked background task and awaits it via `asyncio.shield()` — if our wait times out, only
*our waiting* is abandoned, the connect keeps running to a natural conclusion no matter what. Concurrent/
subsequent callers reuse the same in-flight task instead of racing a second connection. If a connection
succeeds after everyone gave up on it, `_on_connect_task_done()` / `_close_abandoned_client()` disconnect it
immediately so it can never sit there holding a slot. `cleanup()` (entry unload) gives any still-pending
connect a short grace period to finish and be reaped rather than tearing the entry down mid-connect.

Because giving up no longer risks stranding a slot, `connection_timeout` (`const.py`:
`MIN_CONNECTION_TIMEOUT` / `DEFAULT_CONNECTION_TIMEOUT`) is now purely a responsiveness knob (how long a
poll waits before trying again / reporting unavailable), not a safety floor — it was deliberately reverted
from the 65s/75s an earlier fix attempt used back down to 10s/20s.

**Still open / worth checking if this recurs:** the shield-based fix protects the actual `establish_connection()`
call from cancellation, but if this class of bug resurfaces, check first whether some *other* code path
(not `_await_connection`) is calling `establish_connection()` or otherwise connecting without going through
it, and whether disconnects on the read/write path (after a successful connect, inside
`_execute_single_ble_attempt`/`_attempt_device_info_read`) can themselves still be cancelled mid-disconnect
by an enclosing timeout — that path isn't shielded the same way, only the initial connect is.

## Conventions

- User-facing strings live in `translations/{en,de,es,fr,it,nl}.json` and must be kept in sync across all
  six — several past commits exist solely to backfill a string that was added to `en.json` but missed in the
  others. Check all six whenever `config_flow.py` option descriptions/errors/keys change.
- `const.py` has no third-party imports by design (clean separation from `bleak`/HA internals); constants
  that need to reference library internals (e.g. `bleak_retry_connector` timeout values) do so via a comment
  citing the exact constant name rather than importing it there, but `device.py` itself does import
  `bleak_retry_connector` internals directly when needed (e.g. `BLEAK_OUT_OF_SLOTS_BACKOFF_TIME`,
  `BleakOutOfConnectionSlotsError`) even though they're not in that library's `__all__` — there's precedent
  for this in the codebase, just be aware such imports aren't guaranteed stable across `bleak-retry-connector`
  version bumps.
