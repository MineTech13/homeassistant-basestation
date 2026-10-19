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

## V1 (HTC Vive) support — work in progress on its own branch

Tracked in [#4](https://github.com/MineTech13/homeassistant-basestation/issues/4). Developed on the `v1-test` branch,
with no open PR (the first one, #17, was closed on 2026-10-14 and its branch deleted). `v1-test` is brought up to date
by **merging `dev` into it, never rebasing**: published `…-v1test.N` tags must stay in the branch history, and a rebase
would need a force push each time. Expect conflicts in `CLAUDE.md`, `manifest.json` and `pyproject.toml` on every sync
(take `dev`'s version, the `+v1test.N` bump comes with the next V1 build); `git config rerere.enabled true` helps. The
final move into `dev` is a **squash merge**, so the sync merges and the `+v1test.N` bumps never land on `dev`. That
happens only after `dev` itself has been merged to `main`
once the V2 long-term test is done, so **nothing on the V1 branch may change V2 behaviour**: V1 work stays in
`ViveBasestationDevice`, the `pair_id` config step, and V1 strings. Shared code only gets opt-in parameters
whose defaults keep the old behaviour (e.g. `BLEOperationWrite.settle_delay = 0.0`).

Reference implementation: [ShayBox/Lighthouse](https://github.com/ShayBox/Lighthouse) (Rust, btleplug). A user in #4
confirmed it switches V1 stations on Linux where other tools didn't. What it does, and how we compare:
- **Command bytes are identical to ours.** Characteristic `0000cb01-…`, 20 bytes: `12 00 00 00` (on) /
  `12 02 00 01` (off), then the BSID little-endian (`AABBCCDD` → `DD CC BB AA`), then zero padding. That is
  `build_v1_command()`; our `struct.pack("<I", pair_id)` already produced this. So the encoding was never the
  bug.
- **It writes without response and then waits 1 s before disconnecting.** We wrote 3× with 1 s between
  writes but disconnected right after the last one. A write without response is only queued locally when
  `write_gatt_char()` returns, so the V1 write now sets `settle_delay=1.0`.
- **It matches a station to a BSID by the last 4 characters of its name** (`HTC BS xxxxxx` ends in the
  BSID's last 4 hex digits). We never checked this, and a station ignores a command with the wrong ID without
  any error, so a typo looked like a working setup. The config flow now requires the full 8 digits (it used
  to accept 1–8) and rejects an ID whose last 4 digits don't match the advertised name
  (`v1_pair_id_matches_name()`). Existing entries get one WARNING from `update()` if they don't match, and
  diagnostics show `pair_id_matches_name`.
- It never reads anything from a V1 station; there is no readable state. Our info read still tries the
  standard Device Information characteristics. Nobody has reported whether V1 has them; the info read now
  records the station's GATT layout (`gatt_services` in diagnostics) and warns once if `0000cb01` is missing.

**Unverified on hardware.** Nobody on the team owns a V1 station; testers from #4 have to confirm. Ask
them for diagnostics after setup and after one on/off: `pair_id_matches_name`, `gatt_services` (does
`0000cb01` list `write-without-response`? is there a `180a` Device Information service?), and whether
the station actually changed state. "Everything unknown" in #4 is at least partly expected:
firmware/model/hardware/manufacturer stay unknown if V1 has no Device Information service, and the switch
state is only our own guess from the last command sent.

**First tester report (supertin, `2.1.0-alpha.7+v1test.1`, 2026-10-14):** both stations unavailable, no log output.
Diagnostics: `connectable_scanner_count` 0, `seen_by` [] but `pair_id_matches_name` true (that check uses
`connectable=False`), `connect_started` 0, `gatt_services` null. So HA heard the stations only through passive
scanners and the integration never tried to connect; the V1 protocol changes were never exercised, and the ID
check passed. It was silent because `_available` starts `False`, so `_set_available(False)` is no transition and
logs nothing. Response on `v1-test`: `_warn_if_only_passively_seen()` (one WARNING naming the passive scanners),
`scanner_count_total` / `seen_by_any` in diagnostics, README note. The same report exposed that `cached_info` in
diagnostics wasn't redacted, so both testers' pair IDs are public in #4 (fixed on `dev`, `105592c`).

**First confirmed working on hardware (supertin, `2.1.0-alpha.8+v1test.2`, 2026-10-16):** after switching to an ESP32 proxy
(connectable), on/off works. GATT layout of a real V1 (`gatt_services`):
- `0000cb00` service with `0000cb01` flagged **`read, write`**, not `write-without-response`. We still write without
  response (as Lighthouse does) and it works. The characteristic being readable is new.
- `180a` Device Information with only `2a29` (manufacturer, reads "Broadcom", the BLE chip vendor), `2a24` (model,
  "2.11" padded with NULs, now stripped on `dev` `eb858a9`) and `2a23` (system ID). No firmware/hardware revision,
  so those sensors stay unknown.
- An unknown service `9e5d1e47-5c13-43a0-8635-82ad38a1386f` (write/notify/indicate + write + read), probably
  firmware update. Don't touch it.

Open question being tested in `v1test.3`: does reading `cb01` reflect the power state? `_record_power_char()` reads it
after every on/off (via the opt-in `BLEOperationWrite.after_write` hook) and on the info read, and diagnostics show the
last 5 reads as `power_char_reads` (bytes 4–7 masked if they equal the pair ID). If the value follows on/off, V1 could
get a real state readback instead of the last-command guess.

**Result (supertin, `v1test.3`, 2026-10-18, both stations, identical):** `cb01` reads 20 bytes, never the pair ID.
- after `TURN_ON` (written `12 00 00 00` + BSID): `00 12 00 00 00…`
- after `TURN_OFF` (written `12 02 00 01` + BSID): `00 12 00 01 00…`
- info read at HA start, last command was off: `00 12 00 01 00…`

So the value follows on/off, but it is **not proven to be the real state**. Byte 3 matches the last byte of the command
prefix we wrote (`00` / `01`), and the mode byte (`00` / `02`) isn't in it, so it may just be "last command received".
The tester wasn't in the room, so the physical state wasn't checked either. To settle it: a state change that didn't come
from us (unplug and replug a station, which should come up on, then reload the entry) and a read compared with what the
station actually does. Until then nothing acts on the value.

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
- Code that touches private habluetooth internals (`_connected_scanner`, `_connect_failures`, …) must be checked against
  **the habluetooth version the user's HA actually ships** (`homeassistant/components/bluetooth/manifest.json` at that HA
  tag), using the compiled wheel. The dev box's `homeassistant` pin lags behind and has twice hidden a silent no-op (the proxy
  penalty did nothing under the compiled 6.x build, and again under 7.x). A scratch venv with just `habluetooth==<pin>` is enough; the wrapper is plain Python and the scanner
  is Cython.

## Open work

`TODO.md` lists the review findings planned for 2.1.1, with where each one is and a suggested fix.

## Releasing

- Set the exact version in **both** `custom_components/basestation/manifest.json` and `pyproject.toml` before
  tagging — for pre-releases too, so HA/HACS show which alpha is actually installed.
- Pre-release versions use `X.Y.Z-alpha.N` **without leading zeros** (e.g. `2.1.0-alpha.5`). The tag spelling
  `alpha.005` is not valid SemVer (leading zero), AwesomeVersion classifies it as UNKNOWN and hassfest rejects
  it. `X.Y.Z-alpha.N` sorts above the previous stable and below the final `X.Y.Z`. Git tags keep the existing
  `vX.Y.Z-alpha.00N` spelling.
- Order: lint gate (`ruff format --check . && ruff check . && mypy custom_components/basestation`) → bump
  commit → push `dev` → tag → `gh release create <tag> --prerelease --target dev`.
- **V1 test builds** (for #4) are cut from `v1-test`, not `dev` (`--target v1-test`): version
  `<dev version>+v1test.N` (e.g. `2.1.0-alpha.7+v1test.1`), tag `vX.Y.Z-alpha.00N-v1test.00M`. N counts up across
  all V1 builds and does not restart when the dev base changes, so testers can name a build by it. Build metadata
  rather than a fourth pre-release segment because `pyproject.toml` must be PEP 440: ruff's RUF200 rejects
  `2.1.0-alpha.7.v1test.1`, while `+v1test.1` is valid PEP 440 and valid SemVer for hassfest. Release notes must say
  the V1 work stays on its branch for at least one to two months and that later `dev` pre-releases don't contain
  the V1 changes.
- Stable releases: bump to `X.Y.Z` on `dev`, PR `dev` → `main` (protected, needs one approving review, merge commit),
  then tag the merge commit on `main` and `gh release create vX.Y.Z --target main` (not a pre-release).
- Pre-releases are cut from `dev`. Exception: `v2.1.0-alpha.004` was tagged on the since-deleted `alt-test`
  branch (keep-alive connection reuse + verified-command loop, never merged) — it is not an ancestor of
  `alpha.005` or later, so compare links should go from `alpha.003`.
