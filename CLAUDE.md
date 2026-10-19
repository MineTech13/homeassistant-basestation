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
  stopped responding mid-boot would poll at the fast interval forever. Each boot episode is additionally
  capped at `MAX_FAST_POLLING_DURATION` (120s), because a station can keep *answering* `STARTING_UP` for hours
  (see the 2026-10-01 evening incident below).
- `BasestationInfoCoordinator` — polls static info (firmware/model/hardware/manufacturer/channel/pair_id) at
  `info_scan_interval` (default 1800s). Falls back to cached info on timeout/error rather than failing the
  update, so slow/flaky reads don't flap the info sensors. Until the first read has succeeded it retries sooner, from
  `INFO_RETRY_INTERVAL` (connect cooldown + 30 s) doubling up to `info_scan_interval`, so a failed first read doesn't
  leave them `unknown`.

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

## Known issues: Bluetooth proxy trouble

Most of the 2.1.0 work went into one recurring report: over hours, stations behind an ESPHome Bluetooth proxy go
unavailable until the proxy is restarted. It turned out to be several different problems. The full incident log
(July–October 2026, ten incidents on the maintainer's 4× V2 / two-proxy setup) was compacted for the 2.1.0 release;
read it with `git show v2.1.0-alpha.008:CLAUDE.md` or in the history of this file.

### What was found, and what the code does about it

- **Stranded proxy slots from cancelled connects (fixed).** We wrapped `establish_connection()` in our own
  `asyncio.timeout()`. `CancelledError` isn't caught by `bleak-retry-connector`'s retry loop, so its post-failure cleanup
  was skipped and the proxy kept the slot. `_await_connection()` now runs the connect as a tracked task and waits with
  `asyncio.wait({task})`, which never cancels it (not `asyncio.shield()`: on Python 3.14 that logs every abandoned
  failure as `exception in shielded future`). Concurrent callers share the task. A connect that succeeds after
  everyone gave up is closed by `_on_connect_task_done()` / `_close_abandoned_client()`. `connection_timeout`
  (`MIN_`/`DEFAULT_CONNECTION_TIMEOUT`, 10/20 s) is therefore only a responsiveness knob, not a safety floor.
- **Fast polling flood (fixed).** After leaving standby a station can answer `STARTING_UP` for hours. Every fresh read
  reconfirmed it, so it polled at the 5 s fast interval the whole time (~40 connects/min for four stations through one
  proxy). Each boot episode is now capped at `MAX_FAST_POLLING_DURATION` (120 s), with one WARNING on fallback.
- **Repeated connects after a timeout (mitigated).** `CONNECT_TIMEOUT_COOLDOWN` (180 s): after a connect times out,
  state polls and info reads skip connecting until it passes (`connect_skipped_cooldown`). User writes are never
  skipped. Side effect: after an HA restart, a station whose first connect times out stays unavailable ~4 min.
- **Proxy faking connections (mitigated).** A proxy can report a connect as successful while the first read fails with
  `Not connected` (`PHANTOM_CONNECTION_MARKERS`), and HA keeps picking it because the fake success clears its failure
  count. `_penalise_connected_proxy()` charges that proxy `PHANTOM_CONNECTION_PENALTY` (2) entries in habluetooth's
  `scanner._connect_failures[address]`, so HA prefers the other proxy. The scanner must be snapshotted right after the
  connect (`_connected_path()`), because habluetooth 7 clears `_connected_scanner` on disconnect. Confirmed working
  on hardware with alpha.8: the station moved to the other proxy and stayed there.
- **Phantom link inside the proxy (not fixable here).** A connect that bleak_esphome gives up on after 20 s completes
  in the ESP controller anyway (`OPEN_EVT in unexpected state` / `status=133` in the proxy log, exactly 20 s after
  `Connecting`). The station is then connected to the proxy, stops advertising, and disappears from every proxy
  (`seen_by: []`) until that proxy is restarted (once it cleared on its own after ~6 h). Often a second station goes into the fake-connection mode on the
  same proxy within a minute (`Timeout waiting for teardown, forcing IDLE` loop). All attributed cases sat on one
  heavily loaded proxy (an NSPanel); a dedicated ESP32 proxy replacing it is being tested.
- **ESP32 proxy crash (outside our reach).** One proxy hard-crashed in Bluedroid while serving a GATT read
  (`LoadStoreError` via `esp_ble_gattc_read_char`). Proxy-restart errors (`ConnectionState.` / `not ready yet`,
  `PROXY_RESTARTING_MARKERS`) are counted separately. They are matched by message text, because bleak_esphome turns
  aioesphomeapi's errors into a bare `BleakError`.

Not implicated so far: an unshielded disconnect on the read/write path. `suspected_stranded_slots`
(`disconnect_cancelled + abandoned_close_failed`) stayed 0 in every incident. If it ever climbs, shield that path.

### habluetooth behaviour that matters here (read from 6.1.0, re-check on bumps)

- The wrapper re-picks the proxy on every connect and ignores the `BLEDevice` we pass. Score = RSSI minus
  `0.51 × rssi_diff` per connect failure for the address, `1.01 × rssi_diff` per connect in progress on that scanner,
  and `0.76 × rssi_diff` when it is on its last free slot. Only scanners with a fresh advert are scored.
  `_connect_failures` doesn't decay; it clears on a success on that scanner or on scanner (un)register. This count
  alone already steers connects off a proxy whose connects keep timing out, so a wedged proxy no longer takes
  stations down as long as another proxy reaches them. An HA restart resets the counts, so HA routes back to the
  wedged proxy for a while: restart the wedged proxy together with HA.
- A +2 penalty can lose to any one of those on the other proxy, so it is a nudge, not a hard switch. If stations
  keep bouncing back to a bad proxy, the penalty size is the thing to tune.
- `habluetooth.wrappers: info` in HA's `logger:` logs `Found N connection path(s), preferred order:` with each
  proxy's score on every connect. That line explains why a proxy was picked.

### Diagnosing a recurrence

Collect evidence **before** restarting anything; a restart destroys it.

1. Diagnostics for the affected entries (device page → Download diagnostics, no log config needed): `seen_by`,
   counters, `connect_in_flight`, `lock_held_by`, `proxy_penalised`, `suspected_stranded_slots`. Counters reset on
   entry reload. `seen_by: []` means no proxy hears the station (phantom link or out of range).
2. The proxies' own logs around the failure: `OPEN_EVT in unexpected state`, a `Timeout waiting for teardown` loop,
   slot warnings, API/Wi-Fi reconnects, a proxy that went silent, a reboot with a non-software reset reason.
3. HA log at WARNING: availability transitions (with the proxies that see the station), lock timeouts, a connect
   still in flight after `PENDING_CONNECT_WARN_AGE`, proxy penalties, and any disconnect that was cancelled.
4. Then restart **one proxy at a time**, starting with one that shows a teardown loop. The proxy that made the last
   attempt is often not the one holding the phantom link.

`connection_log.py` keeps the counters and a rolling event window in memory (stdlib only). Routine successes are
counted, not stored, and repeats are collapsed over `COLLAPSE_WINDOW` events; without both, the 50-entry buffer
evicts the events that explain a failure within an hour. Six connection-health sensors exist, disabled by default;
enabled, the recorder keeps their history far longer than the logs.

## Conventions

- User-facing strings live in `translations/{en,de,es,fr,it,nl}.json` and must be kept in sync across all
  six — several past commits exist solely to backfill a string that was added to `en.json` but missed in the
  others. Check all six whenever `config_flow.py` option descriptions/errors/keys change.
- Config entries are at version 1.2. Changes to what an existing entry stores (data or options) go into
  `async_migrate_entry` in `__init__.py` with a `MINOR_VERSION` bump in `config_flow.py`. A minor bump keeps
  downgrades working: HA still loads an entry with a newer minor version if the old code has no migration for it.
- `const.py` has no third-party imports by design (clean separation from `bleak`/HA internals); constants
  that need to reference library internals (e.g. `bleak_retry_connector` timeout values) do so via a comment
  citing the exact constant name rather than importing it there, but `device.py` itself does import
  `bleak_retry_connector` internals directly when needed (e.g. `BLEAK_OUT_OF_SLOTS_BACKOFF_TIME`,
  `BleakOutOfConnectionSlotsError`) even though they're not in that library's `__all__` — there's precedent
  for this in the codebase, just be aware such imports aren't guaranteed stable across `bleak-retry-connector`
  version bumps.
- Code that touches private habluetooth internals (`_connected_scanner`, `_connect_failures`, …) must be checked against
  **the habluetooth version the user's HA actually ships** (`homeassistant/components/bluetooth/manifest.json` at that HA
  tag), using the compiled wheel. The dev box's `homeassistant` pin lags behind and has twice hidden a silent no-op (the proxy
  penalty did nothing under the compiled 6.x build, and again under 7.x). A scratch venv with just `habluetooth==<pin>` is enough; the wrapper is plain Python and the scanner
  is Cython.

## Releasing

- Set the exact version in **both** `custom_components/basestation/manifest.json` and `pyproject.toml` before
  tagging — for pre-releases too, so HA/HACS show which alpha is actually installed.
- Pre-release versions use `X.Y.Z-alpha.N` **without leading zeros** (e.g. `2.1.0-alpha.5`). The tag spelling
  `alpha.005` is not valid SemVer (leading zero), AwesomeVersion classifies it as UNKNOWN and hassfest rejects
  it. `X.Y.Z-alpha.N` sorts above the previous stable and below the final `X.Y.Z`. Git tags keep the existing
  `vX.Y.Z-alpha.00N` spelling.
- Order: lint gate (`ruff format --check . && ruff check . && mypy custom_components/basestation`) → bump
  commit → push `dev` → tag → `gh release create <tag> --prerelease --target dev`.
- Stable releases: bump to `X.Y.Z` on `dev`, PR `dev` → `main` (protected, needs one approving review, merge commit),
  then tag the merge commit on `main` and `gh release create vX.Y.Z --target main` (not a pre-release).
- Pre-releases are cut from `dev`. Exception: `v2.1.0-alpha.004` was tagged on the since-deleted `alt-test`
  branch (keep-alive connection reuse + verified-command loop, never merged) — it is not an ancestor of
  `alpha.005` or later, so compare links should go from `alpha.003`.
