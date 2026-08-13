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
coordinator data as entities via `CoordinatorEntity`. `connection_log.py` (in-memory BLE event history) and
`diagnostics.py` (downloadable snapshot) are observability only — nothing reads them for control flow.

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

**Status as of 2026-07-31:** running the shield-based fix for several days is a large improvement (no more
cascading multi-station deadlock), but one station was still seen wedged with a single blocked slot. The
cause of that one is unknown — logging was at INFO, where nothing on the failure path was visible. The
instrumentation below was added specifically to catch it next time; the unshielded-disconnect theory above
is *deliberately not fixed yet*, so that the next occurrence either confirms or eliminates it.

### Diagnosing it next time

`connection_log.py` keeps per-device counters and a rolling window of noteworthy BLE events in memory —
deliberately stdlib-only, no HA/bleak imports. Two sizing decisions there matter and should not be undone
casually: routine successes are counted but not stored (`record(..., store=False)`), and repeats are
collapsed against a short lookback window (`COLLAPSE_WINDOW`) rather than only the previous event, because
one failing poll emits several *interleaved* events. Without both, a 50-entry buffer is exhausted in well
under an hour and the events that explain a failure are the ones evicted.

- **`diagnostics.py`** — device page → Download diagnostics. Counters, event history, whether a connect is
  still in flight and for how long, what holds the client lock, and which proxy sees the device
  (`async_scanner_devices_by_address`). Ask the user for this first; it needs no prior log configuration.
  Counters reset on entry reload, so it must be grabbed before reloading.
- **The number that settles the open question above** is `suspected_stranded_slots` (=
  `disconnect_cancelled + abandoned_close_failed`). If it climbs when a station goes unavailable, the
  unshielded disconnect path is implicated and shielding it is the fix. If it stays 0, look elsewhere.
- **Log levels**: availability transitions (with reason, consecutive failures, last error, in-flight
  connect age), lock acquisition timeouts (with what holds the lock and for how long), a connect still
  in flight after `PENDING_CONNECT_WARN_AGE`, and any disconnect that was cancelled or left a connection
  open are all WARNING now. Everything else stayed DEBUG. Transitions are logged on change only, so this
  is self-limiting rather than per-poll noise.
- Six connection-health sensors exist, `entity_registry_enabled_default = False`. Enabled, the recorder
  keeps their history for days — far longer than the logs — which is how a slow degradation gets spotted
  after the fact.

### Second, distinct root cause found (2026-08-13): ESP32 proxy firmware crash, not slot exhaustion

A user (4× V2 base stations, `couch_ecke`/`pc_ecke`/`bett_ecke`/`tur_ecke`, 2 ESPHome proxies including one
named `ble-tracker`) supplied a full day of connection-health sensor history, a unified logbook export, the
HA error log, and the `ble-tracker` proxy's own ESPHome boot log for 2026-08-11/12. Findings, in order of
how the investigation went:

- **`suspected_stranded_slots` stayed 0 all day, on all 4 devices.** Per the diagnosis note above, this
  rules out the unshielded-disconnect theory as the explanation for *this* incident — the shield-based fix
  is doing its job. The instability had a different cause.
- **One station (`bett_ecke`) accounted for ~10x the errors/abandoned attempts of the other three**, flapping
  `on`/`off`/`unavailable` ~25 times in one day in tight ~90–140s retry bursts, with by far the most
  `BleakOutOfConnectionSlotsError` occurrences. Classic proxy congestion signature.
- **A second station (`couch_ecke`) instead had one clean ~11-hour continuous outage**, and — separately —
  its `power_state` sensor never once changed value the entire day (`Starting Up` from first row to last),
  even though `letzte_erfolgreiche_verbindung` kept advancing ~6,776 times that day, meaning connects were
  frequently succeeding, just always reading back `Starting Up`. This matches the fast-poll-while-booting
  behavior in `coordinator.py` working exactly as designed (the state kept being *freshly reconfirmed*, so
  the staleness fallback never triggered) — but leaves open why the device's own read never left `Starting
  Up` for 11+ hours. Not yet explained; worth another look if it recurs on a station whose proxy is otherwise
  healthy.
- **Cross-device correlation was the key clue.** A unified chronological export of all 4 devices' last-error
  sensor showed 25 clusters where ≥2 devices failed within 15 seconds of each other, including one moment
  where all 4 failed within a 10-second window. Independent per-device polling landing on the same few
  seconds repeatedly is very unlikely by chance — pointed at a cause shared across devices, i.e. proxy-level,
  not per-station.
- **Confirmed: the `ble-tracker` ESP32 proxy hard-crashed.** Its own ESPHome boot log showed `*** CRASH
  DETECTED ON PREVIOUS BOOT ***`, `Reason: Fault - LoadStoreError`, with a backtrace through
  `gatt_get_tcb_by_idx` (Bluedroid GATT stack) ← `esp_ble_gattc_read_char` ←
  `BluetoothProxy::bluetooth_gatt_read` ← `on_bluetooth_gatt_read_request` — i.e. it crashed while servicing
  a GATT characteristic **read**, exactly the operation this integration issues on every poll. This is a
  firmware-level ESP-IDF/Bluedroid bug on the proxy, not something reachable from `device.py`. The crash
  timestamp (proxy log, ~15:11:52–53) lines up closely with `couch_ecke`'s switch flipping back to `on` at
  15:15:01 — consistent with the proxy's reboot-and-reconnect cycle being what ended that station's outage.
  Also visible in the HA error log during this period: `BleakError: Authenticated connection not ready yet
  for ble-tracker @ <ip>; current state is ConnectionState.HOST_RESOLVED!` (the proxy mid-reconnect to HA's
  own API) and, in the out-of-slots messages, moments of `2 scanner(s) registered, 0 scanning` — i.e. total,
  if brief, loss of BLE scanning across both proxies, not just one being congested.
- **Config check before reflashing `ble-tracker`:** its yaml had `esp32_ble_tracker.scan_parameters.active:
  true` but the crashed firmware's own boot dump reported `Scan Type: PASSIVE` — the running firmware
  predates that yaml edit (config-cache log line pointed at a validated-but-stale build). Also flagged:
  `logger: level: DEBUG` piles UART/formatting overhead onto a chip that logged `api took a long time for an
  operation (57 ms), max is 50 ms` right after boot — i.e. it's already missing its own timing budget: worth
  turning down to INFO for normal operation. NimBLE is not an available alternative to Bluedroid here — this
  board (`esp32dev`) has classic-BT hardware, and ESPHome's NimBLE option is for the BLE-only chips (C3/S3/
  C6/H2) that lack it.

**What changed here in response:** two observability additions in `device.py`/`connection_log.py`, both
purely diagnostic (no behavior/retry-logic change, since the crash itself is outside this integration's
reach):
- A new `Outcome.PROXY_RESTARTING` / `ConnectionStats.proxy_restarting` counter, detected in
  `_record_connect_exception()` by matching the `ConnectionState.` / `not ready yet` substrings in the error
  message (`PROXY_RESTARTING_MARKERS`, `_is_proxy_restarting_error()`). This exists purely as a message-text
  match because `bleak_esphome` collapses aioesphomeapi's structured `APIConnectionError` down to a bare
  `bleak.exc.BleakError` (`raise BleakError(str(err)) from err`) before it reaches us, so there's no
  exception type left to check — same fragility caveat as the `bleak_retry_connector` internals mentioned in
  Conventions below, and worth re-checking if an aioesphomeapi update changes that wording.
- `_set_available()`'s "now unavailable" WARNING now includes `_seen_by_proxies()` — the scanner/proxy
  name(s) currently seeing the device's advertisements. The point is to make a shared-proxy incident
  self-evident from the HA log alone next time (matching WARNING lines across stations, same proxy name),
  rather than requiring the CSV-export-plus-diagnostics-plus-manual-correlation process this investigation
  actually needed.

Firmware update / upstream report for the ESP-IDF crash itself is still pending as of this writing — status
unverified.

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
