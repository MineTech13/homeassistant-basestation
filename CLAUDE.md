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
launches it as a tracked background task and awaits it via `asyncio.wait({task})` (originally `asyncio.shield()`,
see the eighth incident for why that changed) — if our wait times out, only *our waiting* is abandoned, the
connect keeps running to a natural conclusion no matter what. Concurrent/
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

### Third incident (2026-10-01): stations unavailable until the proxies are restarted — persistent logging set up

Same user/setup as above (4× V2, proxies `ble-tracker` and `nspanel`, HA OS, integration 2.1.0). Two stations
(`bett_ecke`, `pc_ecke`) were unavailable; the other two fine. **Restarting both proxies (the user's
`script.restart_ble_proxys`, which just presses the two ESPHome restart buttons) brought all four back within
about a minute.** The user says this is recurring and that *which* station drops is random — i.e. a proxy-side
stuck state, not a per-station or integration problem.

What the integration's own diagnostics showed while it was broken (grabbed before the restart, via
`ha_get_integration(..., include_diagnostics=True)`):
- `suspected_stranded_slots` 0 and `out_of_slots` 0 on all four → not the unshielded-disconnect theory and not
  slot exhaustion as the integration sees it. Any slot held on the ESP side is invisible to us.
- `pc_ecke`: `seen_by: []`, no last advertisement, `connect_started: 0` → HA's Bluetooth stack received no
  adverts from it at all, so the integration never even tried to connect. (Whether the integration should
  behave differently when a device isn't advertising was not looked at in code — unverified.)
- `bett_ecke`: 160 abandoned connects, connect timeouts at 20s, `disconnect timed out...` in the bleak error,
  some `ESP_GATT_ERROR ... Interference/range`, and lock timeouts (state read vs. device-info read blocking
  each other). RSSI was -103 dBm via `ble-tracker` but -80 dBm via `nspanel`. Weak signal alone does not explain
  "a proxy restart fixes all stations at once".
- After the restart everything recovered, including `pc_ecke` which no proxy had been seeing — so the proxy's
  scanner/BLE stack was wedged, not the station.

Observed afterwards: **every station connection goes through `nspanel`** (`[0]` slot, one at a time, ~once a
minute per station); `ble-tracker` made none. So the NSPanel is the proxy that matters day to day, even though
`ble-tracker` is the one with the known hard crash. The NSPanel is also heavily loaded (Nextion UI, the
Blackymas package): at boot it logs `script took 216 ms (max 50)` / `api took 96 ms (max 50)`.

**Unconfirmed hypotheses** (none proven — the point of the logging below is to settle it):
- Wi-Fi power saving: neither proxy set `power_save_mode`, ESPHome recommends `none` for BLE proxies. Now set
  to `none` on both.
- NSPanel CPU/heap starvation of the Bluetooth stack.
- Bluedroid / ESP-IDF GATT stack wedging on the proxy (same family as the 2026-08-13 crash).
- `ble-tracker` heap: free ~66 KB, largest block ~39 KB shortly after boot — check whether it trends down.

**What was set up for next time (all outside this repo, in the user's ESPHome + HA):**
- Both proxies publish their device log over MQTT (`esphome/<name>/log`, Mosquitto app, `discovery: false`).
  Needs `mqtt_host` / `mqtt_user` / `mqtt_password` in the ESPHome secrets.
- HA automation `automation.esphome_ble_proxy_logs_to_logbook` subscribes to `esphome/+/log`, strips ANSI
  colour codes, keeps `[W]`/`[E]`/`[C]` lines plus anything mentioning `bluetooth_proxy`, and writes them to
  the **logbook** (`logbook.log`, name `ESPHome log <proxy>`). The recorder keeps ~10 days, so it survives
  proxy reboots. Read it back with `ha_get_logs(source="logbook", search="ESPHome log", hours_back=N)`.
- `ble-tracker` also has `Uptime`, `Heap Free`, `Heap Largest Block` and `Reset Reason` entities
  (`sensor.flur_ble_tracker_*`; the `debug:` component). Not added on the NSPanel — its package may already
  define the same entity names. A `Reset Reason` other than `software via esp_restart` means a crash/watchdog.
- Proxy logger level is `INFO` on both. ESPHome rejects a per-tag level more verbose than the global one, so
  `bluetooth_proxy: DEBUG` with a global `INFO` does not compile; connect/disconnect detail is therefore not
  captured. Raising the global level to DEBUG on the NSPanel is expensive — only do it deliberately.
- The `Connecting v3 ...` lines are ~4/min (~5k logbook rows/day). They are kept on purpose during
  diagnosis (they show whether the proxy was still attempting connects during a hang); exclude them in the
  automation's regex if the logbook bloat becomes a problem.
- The ESPHome yaml for the two proxies (`ble-tracker.esphome.yml`, `nspanel.esphome.yml`) was dropped in the
  repo root as untracked scratch copies of what is flashed. They hold the API/OTA keys — **do not commit them**.

**When it recurs — do this BEFORE restarting the proxies** (the restart destroys the evidence):
1. Pull the integration diagnostics for the affected entries (`seen_by`, counters, `connect_in_flight`,
   `lock_held_by`). `seen_by` empty = the proxies stopped scanning that device.
2. Read the logbook around the failure for both proxies. Look for: a `ble-tracker`/`nspanel` gap in
   `Connecting` lines (proxy went silent), `Too many connections`/slot warnings, `api`/`mqtt` buffer-full or
   long-operation warnings, Wi-Fi/API reconnects, and a proxy that stopped logging entirely.
3. Check `sensor.flur_ble_tracker_reset_reason` / `uptime` history: a reboot with a non-software reason, or an
   uptime reset, means a crash, not a wedge. Check the heap sensors for a downward trend.
4. Only then restart — preferably **one proxy at a time** rather than `script.restart_ble_proxys`, so the culprit
   is identifiable. Start with a proxy showing a `Timeout waiting for teardown` loop, if any; otherwise the one with
   the last failed attempt for the station. The sixth incident showed the last-attempt heuristic can pick the wrong
   proxy.
If logs show the proxy kept running and logging normally while stations were unreachable, the wedge is inside
the Bluedroid GATT stack, and the realistic options are ESPHome/ESP-IDF updates or moving traffic off that
proxy — nothing in `device.py` can fix it.

### Fourth incident (2026-10-01 evening): integration's own fast polling flooded the NSPanel

Same setup. `pc_ecke` went unavailable at 18:53 local. Evidence was collected per the runbook above before
restarting; restart brought it back ~20s later.

- **Proxy side (logbook):** `nspanel` logged `[E] OPEN_EVT in unexpected state` + `[W] Connection open failed,
  status=133` four times 18:49–18:52, then `[E] Timeout waiting for teardown, forcing IDLE` every ~70s
  continuously from 18:49:55 until restarted. The last connect to `pc_ecke` (18:52:22) failed with that same
  error; afterwards no proxy saw its adverts at all (`seen_by: []`), so the integration correctly stopped
  attempting. Not a crash: `ble-tracker` reset reason `software via esp_restart`, heap ~94 KB free.
  `suspected_stranded_slots` 0 everywhere — the unshielded-disconnect theory is still not implicated.
- **The finding — this was integration-caused load:** after the stations were switched out of standby at
  16:22, **all four reported `STARTING_UP` (0x01) for 4.5 hours** and never reached `ON`. Because each 5s fast
  poll freshly re-read `STARTING_UP`, the staleness fallback in `coordinator.py` never fired, so every station
  polled at `fast_polling_interval` the whole time: `tur_ecke`'s last-successful-connection sensor shows 140
  connects in 15 min (one per ~6.5s), i.e. ~40 connect/disconnect cycles/min through the NSPanel for ~2.5h
  before it wedged. This also explains the 2026-08-13 `couch_ecke` "stuck in Starting Up for 11h, ~6,776
  connects/day" observation — same mechanism, it was never a separate mystery. (Note: a cold boot from
  `SLEEP` does reach `ON` within seconds — 13:00 the same day; the long `STARTING_UP` was after leaving
  `STANDBY`.)
- **Fix:** `BasestationCoordinator` now caps each boot episode at `MAX_FAST_POLLING_DURATION` (120s, `const.py`)
  of fast polling, tracked from when the boot state was first seen and reset once the state leaves the boot
  set; it logs one WARNING when it falls back. Unverified whether this alone stops the proxy wedges — the
  flood is the strongest suspect but causation isn't proven. If wedges continue at ~1 connect/min/station,
  the proxy-side hypotheses in the third incident are back in play.
- **Open:** whether `STARTING_UP` after standby is really "on" for these stations (i.e. whether the
  switch/power-state sensor should treat it as on rather than booting). Not changed.
- **Test plan agreed with the user (one variable at a time):**
  1. Run `2.1.0-alpha.6` (the fast-poll cap) with the NSPanel still as the main proxy for a few days. Check
     that connects stay at ~1/min/station even while stations sit in `STARTING_UP` (one WARNING per episode
     in the HA log), and whether the NSPanel still wedges.
  2. Only if it still wedges: add a dedicated ESP32 proxy (Ethernet preferred, else `power_save_mode: none`;
     bare proxy + MQTT log + `debug:` sensors) in the room and **remove `bluetooth_proxy` from the NSPanel
     entirely**, so HA can't keep routing through it. Confirm via diagnostics `seen_by` that all four
     stations moved. Wedges stopping after step 2 but not step 1 → NSPanel overload was the cause.
  When reading later incidents, first establish which of these stages was live at the time.

### Fifth incident (2026-10-02 night): one station vanished from both proxies, proxy restart fixed it

Same setup, **test-plan stage 1 live** (`2.1.0-alpha.6`, NSPanel still the main proxy). `tur_ecke` unavailable
from 00:18 local until the proxies were restarted at 06:53; back ~64s after the restart. Evidence collected
before the restart:

- **Fast-poll cap confirmed working:** `tur_ecke` was contacted ~1×/min the whole evening (proxy logbook), even
  though it sat in `Starting Up` 16:52–20:46. No flood this time — so the flood was not the only trigger.
- `suspected_stranded_slots` 0, `out_of_slots` 0 on all four. Other three stations healthy throughout (~600
  successful connects each, `couch_ecke` zero failures).
- **Proxies not wedged:** no `Timeout waiting for teardown` loop on the NSPanel, `ble-tracker` uptime unbroken
  (no crash), both kept serving the other stations normally.
- Sequence: 23:04–00:18 occasional 20s connect timeouts (`disconnect timed out`), then 00:14–00:18 five
  `[E] OPEN_EVT in unexpected state` + `[W] Connection open failed, status=133` for `tur_ecke` — three on
  `nspanel`, then two on `ble-tracker` (HA failed over). Last attempt 00:17:52 via `ble-tracker`. From then on
  **neither proxy received a single advert from it** (`seen_by: []`), while both still saw the other three.
  Station was in `Standby`.
- Restarting both proxies brought it straight back — so the station itself was fine.

**Working hypothesis (unproven): phantom link-layer connection.** `OPEN_EVT in unexpected state` means the
ESP controller completed a connection *after* the host side had already given up on it. If that link stays up
in the controller, the station is connected (to the proxy) and a connected BLE peripheral **stops
advertising** — which explains "vanished from *both* proxies at once while they see everything else", and why
only a proxy restart (dropping the link) cures it. The 2026-10-01 evening `pc_ecke` case (`OPEN_EVT in
unexpected state` on `nspanel`, then `seen_by: []`) fits the same pattern. Nothing in `device.py` can see or
drop a link the proxy no longer reports. This would be an ESPHome `bluetooth_proxy` / Bluedroid issue.

**Next time, to test it:** collect evidence as usual, then restart **only the proxy that made the last
`OPEN_EVT in unexpected state` attempt** for that station (not the script that restarts both). If the station
reappears from that alone, the phantom-connection holder is identified. If not, restart the other one.

### Sixth incident (2026-10-04): two stations, two failure modes, at the same moment

Same setup, **test-plan stage 1 still live** (`2.1.0-alpha.6`). All four stations were switched on at 03:41 local and
polled at ~1 connect/min each (fast-poll cap held, no flood). From ~04:10 local two stations went bad. Evidence was
pulled via the HA MCP before any restart.

- **`couch_ecke` (F8:…:F6:2F): phantom-link pattern, now attributed exactly.** Every `[E] OPEN_EVT in unexpected
  state` / `status=133` between 04:10 and 04:13:32 landed **~20.0s after a `Connecting v3` to this station**: seven
  in 3.5 min, four on `nspanel` and **three on `ble-tracker`**. 20s is bleak_esphome's own connect timeout (`Timeout
  waiting for connect response … after 20.0s, disconnect timed out`), not ours, so the link is completing in the
  controller just as the host cancels it. The last attempt was via `ble-tracker` (04:13:12 → OPEN_EVT 04:13:32). After
  that neither proxy saw the station again (`seen_by: []`). The attempts came from the state poll's retry, the state poll
  and the info read, in quick succession. **Not NSPanel-specific**, so test-plan stage 2 alone may not prevent it.
- **`pc_ecke` (DD:28:…:58:C1): new mode, a proxy faking connections.** Starting with the same storm, every connect via
  `nspanel` "succeeded" in ~0.18s. The first read then failed with `BleakError … error=-1 description=Not connected`
  (~780×), and exactly 10s later `nspanel` logged `[E] [0] Timeout waiting for teardown, forcing IDLE`. Every
  teardown timeout in the logbook followed a DD:28 connect. The other stations used the same slot fine. Both proxies
  still saw the station (nspanel -74, ble-tracker -86). It flapped ~40× in 8h and only recovered when HA happened to route
  via `ble-tracker`. **Why HA kept picking nspanel** (habluetooth 6.1.0): the wrapper re-picks the proxy on every
  connect by RSSI minus `0.51 × rssi_diff × connect_failures[addr]`, ignoring the BLEDevice we pass. The fake
  success calls `_finished_connecting(addr, True)`, which *clears* that proxy's failure count, so it always won.
- `suspected_stranded_slots` 0 and `out_of_slots` 0. `ble-tracker` up ~55h, healthy heap (83 KB free / 55 KB block).
  The unshielded-disconnect theory is still not implicated.
- **Stage-1 verdict:** wedges happen at ~1 connect/min/station, so the flood was not required. Stage 2 (dedicated
  proxy, NSPanel proxy removed) is back in play, with the caveat above.
- **Restart result (one at a time, 15:15 local):** restarting `ble-tracker` alone (it made the last Couch attempt)
  changed nothing: Couch was still `seen_by: []` 2.5 min later. Restarting `nspanel` brought **both** back within
  ~25s (PC Ecke 15:18:31, Couch 15:18:40, seen by both proxies again). So **nspanel held Couch's phantom link** even
  though ble-tracker made the last attempt. Its earlier OPEN_EVT for Couch (04:13:10, slot `[1]`) is the likely
  holder. It also cured the PC Ecke "Not connected" mode, so both symptoms were one wedge on nspanel. Runbook update:
  the "last attempt" heuristic is unreliable; restart first the proxy showing a `Timeout waiting for teardown` loop,
  if any.

**What changed in response (`device.py`, unreleased at time of writing):**
- **Connect-timeout cooldown** (`CONNECT_TIMEOUT_COOLDOWN` = 180s). After a connect fails by timing out (our own wait
  or `CONNECT_TIMEOUT_MARKERS`), state polls and info reads skip connecting until it passes. Retries inside
  the same poll stop too. The check runs after the lock is taken, so a poll queued behind the failing attempt
  doesn't fire straight after it. User writes (on/off/standby/identify) are never skipped. Skips don't count as
  failures. Visible as `connect_skipped_cooldown` / `Outcome.SKIPPED` and `live.connect_cooldown_remaining` in
  diagnostics.
- **Phantom-connection proxy penalty** (`_penalise_connected_proxy`). When an operation on a just-established connection
  fails with `PHANTOM_CONNECTION_MARKERS` (`Not connected`), the proxy that "connected" is charged with
  `PHANTOM_CONNECTION_PENALTY` (2) connect failures via habluetooth's private `client._connected_scanner` →
  `_connect_failures[address]` (originally `_add_connect_failure(address)`, which was a silent no-op under HA's
  compiled habluetooth; see the seventh incident). Two failures always outweigh the RSSI gap between the top two paths, so HA picks the
  other proxy next time. The penalty clears when that proxy next succeeds for the station. Uses private habluetooth
  internals with `getattr` fallbacks (warns once if they're missing), so **re-check on habluetooth bumps, against
  the compiled build**. It logs a WARNING
  once per proxy in a row. Visible as `proxy_penalised` / `Outcome.PROXY_PENALISED`. It cannot help a station only one
  proxy can reach, and does nothing for the Couch-type phantom link (the station is gone from every proxy).

Both are unverified on hardware. After deploying, expect: after a connect timeout, at most one connect per 3 min to
that station. On a recurrence of the PC Ecke mode, one WARNING naming nspanel, then its connects moving to
`ble-tracker` in the proxy logbook, with no sustained teardown loop for that address.


### Seventh incident (2026-10-05): same pairing again, first run of the sixth incident's mitigations

Same setup, test-plan stage 1, now running the sixth incident's `dev` build (cooldown + proxy penalty). Evidence pulled
on 2026-10-06 ~01:05 local, before any restart.

- **`tur_ecke` (F5:55): phantom link from a single attempt.** Last connect via `nspanel` 07:58:07, one `OPEN_EVT in
  unexpected state` at 07:58:27 (exactly 20s), and the station was gone from both proxies from then on
  (unavailable 17h+, `seen_by: []`). The cooldown fired as designed (`connect_skipped_cooldown` 4, about 3 attempts
  in total instead of a storm), but **one attempt was enough**, so the cooldown can't prevent this, only reduce
  how many chances there are.
- **`pc_ecke` (DD:28): "Not connected" mode again,** starting 58s later (07:59:25), with the `nspanel` `Timeout waiting for
  teardown` loop tied to DD:28 connects. It was still running at 01:06 the next night (1313 failures, 57
  unavailable flips). This is the second time in a row that a phantom link on nspanel was followed by exactly this mode
  on PC Ecke.
- **The proxy penalty never fired (`proxy_penalised` 0). Bug:** HA ships habluetooth **compiled with Cython**.
  `BaseHaScanner._add_connect_failure` is a `cdef` method there, invisible from Python, so the `getattr` fallback
  turned the penalty into a silent no-op (a DEBUG line only). The dev box had the same compiled build; the first version
  was just never exercised against it. **Fix:** write `scanner._connect_failures[address]` (declared `cdef public
  dict`, reachable in both builds) and log a WARNING once if the internals are missing. Verified against the
  compiled habluetooth 6.1.0: nspanel's path score for DD:28 drops from -68 to -87.4, below ble-tracker's -87.
- **Test-plan status after this incident (agreed 2026-10-06):** stage **1b** is live: `dev` `dd27c4e` (the
  cooldown plus the *working* proxy penalty), NSPanel still the main proxy, installed and HA restarted at 01:15 local.
  Wait for one more failure under 1b, then go to stage 2: the user replaces the NSPanel's Bluetooth proxy with a
  dedicated ESP32 BLE proxy in the room. Known 1b side effect: after an HA restart a station whose first connect times
  out (all four connect at once) stays unavailable ~4 min until the cooldown expires. Seen once (Tür/Bett, back at
  01:19), harmless.
- **Restart result:** restarting **only `nspanel`** (01:10 local) brought both back within ~45s (PC Ecke 01:10:16,
  Tür Ecke 01:10:48, seen by both proxies again). `ble-tracker` was not restarted. That confirms nspanel held Tür's
  phantom link too.
- Every phantom link so far that could be attributed to a proxy (sixth incident, and this one) sits on `nspanel`.
  Together with the teardown loops, that is the strongest case yet for test-plan stage 2 (take `bluetooth_proxy` off the
  NSPanel).

### Eighth incident (2026-10-06 early morning): the stage-1b failure, same pairing with the roles swapped

Same setup, **test-plan stage 1b live** (`dev` `dd27c4e`, shows as `2.1.0-alpha.6`, NSPanel still the main proxy). This is
the "one more failure under 1b" the seventh incident was waiting for. It happened ~3h after 1b went live and was not
looked at until 2026-10-11, so nothing was collected live: no diagnostics from the time, and HA's own log for that day is
gone (see the logging notes below). Reconstructed afterwards from the proxy logbook and the recorder.

**Proxy placement (from the user, matches RSSI):** `nspanel` is in the room with the stations, `ble-tracker` is far away.
RSSI nspanel / ble-tracker: Tür -63/-95, Bett -74/-97, Couch -77/-92, PC -85/-97. So "steer away from nspanel" means
"steer onto a link at the edge of range". Keep this in mind for any penalty tuning.

- **`pc_ecke` (DD:28): phantom link on `nspanel`.** `Connecting v3` to DD:28 on `nspanel` slot `[0]` at 04:23:43.5, then
  `[E] [0] OPEN_EVT in unexpected state` + `status=133` at 04:24:03.6, i.e. 20.1s later (bleak_esphome's timeout again). After
  that **no proxy attempted DD:28 again** until the restart, consistent with it no longer advertising (`seen_by` was not
  captured). Unavailable 04:29 → 06:51.
- **`tur_ecke` (F5:55): "Not connected" mode on `nspanel`, starting 54s later.** From 04:24:47 every F5:55 connect via
  `nspanel` was followed ~10s later by `[E] [0] Timeout waiting for teardown, forcing IDLE`, about twice a minute (the
  poll plus its retry) until the restart. That's 201 teardown lines 04:24–06:48, all tied to F5:55. Its last-error sensor
  confirms `Bluetooth GATT Error … error=-1 description=Not connected`. This is the **third time in a row** that a
  phantom link on nspanel was followed within about a minute by this mode for another station through nspanel. Tür
  and PC swapped roles compared with the seventh incident, so which station gets which mode is not fixed.
- **The proxy penalty fired, but didn't stick.** Tür did get moved to `ble-tracker` at 05:01, 05:13, 05:28, 05:37,
  05:57 and 06:13. Each move succeeded (switch back to `on`), but it lasted only 1–3 polls before HA routed it back
  to `nspanel`. Most in-poll retries straight after a penalised failure also went back to nspanel (e.g. 04:24:47 →
  04:24:59, 04:26:09 → 04:26:21). Tür went `unavailable` 8 times. The constant's comment says two failures
  "flip the choice whatever the RSSI gap", but that only holds when the other path carries no penalty of its own.
  In habluetooth 6.1.0 `_score_connection_paths` also subtracts `1.01 × rssi_diff` per connect in progress on that
  scanner, `0.51 × rssi_diff` per failure that scanner has for the address, and `0.76 × rssi_diff` when it is on its last free slot. It
  also only scores scanners that currently have a fresh advert for the address. A +2 penalty on nspanel
  (−1.02 × diff) loses to any one of those on `ble-tracker`. `_connect_failures` is only cleared on scanner
  (un)register or a success on that scanner, so the count does not decay. Which of these sent Tür back here is
  **unknown**: it needs habluetooth's own INFO line (below). Tür's own `ble-tracker` connects 04:18–04:23 had
  succeeded, so it was not a failure count on ble-tracker.
- **Recovery:** `script.restart_ble_proxys` (both proxies, not one at a time) at 06:50:25. NSPanel was booted by 06:50:38,
  and all four were connecting again by 06:51:54. Since both were restarted, the restart itself does not
  identify the holder, but the logbook does: the only `OPEN_EVT` was on nspanel.
- **Afterwards, through 2026-10-11 ~20:30:** no teardown loop at all, and nothing in the logbook pattern-matches this
  incident. There were four `OPEN_EVT`s, all on `ble-tracker` (10-06 19:28, 10-11 02:03, 15:44, 15:51), and none led to
  a vanish. `ble-tracker` has been up since the 06:50 restart (reset reason `Reboot request from api`, heap ~93 KB free /
  56 KB largest block). The other unavailable blips (PC 10-07 07:03–07:08, PC 10-11 01:16–01:20, all four at 10-06
  01:15) are ~4 min each. The 10-11 01:16 one follows an HA restart at 01:15:40: the known 1b cooldown side effect.
  `suspected_stranded_slots` / out-of-slots counters 0 everywhere.

**Stage-1b verdict:** cooldown and penalty reduce the damage but don't prevent it. One 20s connect timeout on nspanel was again
enough to create a phantom link and, with it, the fake-connection mode for a second station. All three phantom links
that could be attributed to a proxy (sixth, seventh, eighth incidents) sit on `nspanel`. **Next step per the test plan is
stage 2:** a dedicated ESP32 proxy in the room, and `bluetooth_proxy` removed from the NSPanel.

**Logging gaps found while reconstructing this, fix before the next incident:**
- HA's journal only reached back to 2026-10-07 13:19 when read on 10-11. `starlink` logs a traceback every 5s
  (~20k lines/h) and pushes everything else out within ~4 days. Either fix or disable that integration, or pull HA's log
  promptly after an incident. The penalty WARNING for this incident could not be checked because of it.
- Add `habluetooth.wrappers: info` to HA's `logger:`. On every connect it logs `Found N connection path(s), preferred order:`
  with each proxy's RSSI, `failures=`, `in_progress=`, `slots=free/total` and `score=`. That is the one line that shows why
  a penalised proxy was still picked. It costs about one line per connect (~4/min for four stations).

**New log noise, not a fault:** HA 2026.10 runs Python 3.14.6, whose `asyncio.shield()` now attaches
`_log_on_exception` to the inner future once the outer is cancelled. Every connect `_await_connection()` stopped waiting
for that later fails is now logged as `ERROR … Error doing job: BleakNotFoundError exception in shielded future (task:
None)` with a traceback. The traceback includes `device.py … in _on_connect_task_done` only because that callback's
`task.result()` re-raised it. The failure is already recorded in `connection_log` (`context="nobody waiting"`), so
nothing is lost, but each abandoned connect now produced an ERROR (6 more between 10-11 and 10-12). **Fixed on
`dev` 2026-10-12:** `_await_connection()` and `cleanup()` now wait with `asyncio.wait({task})` instead of `shield()`.
That also never cancels the task, but it doesn't register the logging callback. Checked with a stand-in script on
Python 3.14.6 and 3.13: `shield` logs `… exception in shielded future`, `wait` logs nothing, the task is never
cancelled in either, and a waiter still present gets the exception from `task.result()`.

**Check-in 2026-10-10 ~02:57 local (before a user-requested HA restart): clean.** Covers 31.5h since the 10-08 19:22 HA
restart, still stage 1b.
- Each station ~1,930 connects, all succeeded. `became_unavailable`, `proxy_penalised`, `suspected_stranded_slots`,
  `out_of_slots`, `lock_timeout` all 0. The only failure is Bett Ecke's post-restart connect timeout (cooldown handled it).
  All four seen by both proxies. Switch history shows only user on/off, never `unavailable`.
- Proxy logbook: no `[E]` lines at all (no `OPEN_EVT in unexpected state`, no teardown loop). ble-tracker uptime unbroken,
  heap ~93 KB. No basestation errors in the HA log.
- New, minor: nspanel logged `[W] … Connected reply deferred, TCP buffer full` 3× (10-09 17:32, 17:41, 23:42), once
  for each of different stations. That is its API send buffer to HA filling briefly during a connect. Harmless alone,
  another sign of NSPanel load.
- **Tooling gotcha:** `ha_get_logs(source="logbook", hours_back=N)` returns at most ~24h even for larger N. A 32h search
  silently ended at 10-09 18:56 and looked like the proxy MQTT logging had stopped; it hadn't (the forwarding automation is
  `automation.ble_proxy_logs_ins_logbuch`, not the name given in the third incident). Read longer windows in ≤24h chunks
  via `end_time`.
- Stable since 2026-10-06 06:51. The plan is unchanged: weekend on 1b, then stage 2.

**Follow-up check, 2026-10-12 ~19:15:** no failure since the eighth incident. No teardown loop and no new `OPEN_EVT` after
10-11 15:51. The only unavailable blip (PC + Couch, 10-11 20:42–20:46) followed an HA restart at 20:42:19, which is the
known 1b cooldown side effect; another restart at 10-12 02:50 caused none. Counters on all four are low (≤2 connection
failures since that restart), `suspected_stranded_slots` 0. Stage 1b is still live, NSPanel still the main proxy.

### Ninth incident (2026-10-13/14): alpha.7 on HA 2026.10, and the proxy penalty had been dead since the HA update

Same setup, now on the public **`2.1.0-alpha.7`** (installed with the HA restart at 10-12 19:30), HA **2026.10.1**, NSPanel still
the main proxy (functionally stage 1b). Two episodes with the usual pairing, both starting with a Couch Ecke (F8:…:F6:2F)
connect on `nspanel` slot `[0]` and an `OPEN_EVT in unexpected state` / `status=133` exactly 20s later:

- **10-13 18:31:19.7 → 18:31:39.7.** Couch went unavailable 18:35 (phantom link). Bett Ecke (C4:C1) went into the "Not
  connected" mode on nspanel (`Timeout waiting for teardown` loop) and flapped until 20:55. The user restarted both proxies
  at 10-14 00:23:05, together with an HA restart; all four were back by 00:23:29.
- **10-14 08:30:33.6 → 08:30:53.7.** Couch unavailable from 08:36 with `seen_by: []`. PC Ecke (DD:28) went into the "Not
  connected" mode from 08:31:53: 640 `GATT Error … Not connected`, 39 unavailable flips by 17:13. Tür and Bett stayed fine.
  Evidence was pulled live via the HA MCP before any restart.
- `suspected_stranded_slots` 0, `out_of_slots` 0 on all four again.

**The penalty never fired (`proxy_penalised` 0), and the HA log said why:** `Cannot steer basestation DD:28… away from a
Bluetooth proxy that faked a connection: habluetooth's internals are not what this integration expects (scanner:
NoneType)`. Cause: HA 2026.10.0 moved to **habluetooth 7.1.2**, whose `HaBleakClientWrapper.disconnect()` calls `_untrack()`
and clears `_connected_scanner` / `_connected_device`. In `_execute_single_ble_attempt` / `_attempt_device_info_read`,
`async with client` has already disconnected when the failed read reaches the `except` block, so the penalty always found
no scanner. In 6.x nothing cleared those attributes, which is why it partly worked in the eighth incident.

| HA | habluetooth | penalty |
|---|---|---|
| 2026.9.x (until 10-08) | 6.26.11 | fired (eighth incident) |
| 2026.10.0 / 2026.10.1 (from 10-08 19:22) | 7.1.2 | silent no-op |

So it broke with the HA update, not with alpha.7 (alpha.7's only code change was shield → `asyncio.wait`). The 10-08 → 10-12
"stable" stretch on `dd27c4e` had no `OPEN_EVT` at all, so the penalty was never exercised there. The dev box still has
habluetooth 6.1.0, which is how the seventh-incident fix was "verified" against the wrong version.

**Fix (`dev`, unreleased at time of writing):** `_connected_path()` snapshots the scanner and address straight after the
connect, and `_penalise_connected_proxy(scanner, address)` uses that. Verified against the compiled habluetooth 7.1.2 in a
scratch venv, with the real `device.py`, HA stubbed out: a `Not connected` raised inside `async with client` charges nspanel
with 2 failures (`proxy_penalised` 1). With today's RSSI (nspanel -86, ble-tracker -93) the path score goes -86 / -93 →
-93.14 / -93.0, so ble-tracker wins, **but only by 0.14**. Any connect in progress on ble-tracker flips it back (see the eighth
incident). `PHANTOM_CONNECTION_PENALTY` was left at 2 on purpose until `proxy_penalised` shows real numbers.

**Side finding, not causal:** `ble-tracker` dropped its API connection to HA every 1–10 min from 10-14 01:17 to 04:09 local.
Its `uptime` kept counting through it and the reset reason stayed `Reboot request from api`, so it did not reboot. It had been
OTA-flashed three times on 10-13 18:39–18:44. Not investigated further.

**Restart result:** restarting **only `nspanel`** (10-14 17:22:42 local, `ble-tracker` untouched) cured both. Last teardown
line 17:22:41, nspanel back up 17:22:54, Couch back at 17:23:16 (first connect via nspanel 17:23:13), PC Ecke back at
17:23:42, no `[E]` lines afterwards. So nspanel held Couch's phantom link: **four out of four phantom links that could be
attributed (sixth, seventh, eighth, ninth) sit on nspanel**, and each time the "Not connected" mode on the second station
went away with the same restart. alpha.8 (the penalty fix) was not installed yet at this point.

**Plan agreed 2026-10-14: test-plan stage 1c** = `2.1.0-alpha.8` (released 10-14, the first build where the penalty works on
HA 2026.10), NSPanel still the main proxy. Wait for the next nspanel episode before stage 2. **Live since 2026-10-14 ~17:28
local** (alpha.8 installed + HA restart; all four available within ~30s, no basestation warnings). Confirm
`integration_manifest.version` in the diagnostics says `2.1.0-alpha.8` before reading any incident as 1c.
What to check on the next "Not connected" episode:
- `proxy_penalised` > 0 on the affected station, one `… reported a connection … that was not really there` WARNING naming
  nspanel, and **no** `Cannot steer` WARNING (if that comes back, the snapshot in `_connected_path` is broken again).
- In the proxy logbook, that station's `Connecting v3` lines moving to `ble-tracker`, and how long they stay there before
  HA routes back to nspanel (the 0.14-point margin, see above). If they keep bouncing back, the penalty size is the next
  thing to tune.
- The phantom link itself (the station that vanishes with `seen_by: []`) is not expected to change: it still needs an nspanel
  restart. That is what stage 2 is for.

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
  tag), using the compiled wheel. The dev box's `homeassistant` pin lags behind and has twice hidden a silent no-op (seventh
  and ninth incidents). A scratch venv with just `habluetooth==<pin>` is enough; the wrapper is plain Python and the scanner
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
- **V1 test builds** (for #4) are cut from `v1-test`, not `dev` (`--target v1-test`): version
  `<dev version>+v1test.N` (e.g. `2.1.0-alpha.7+v1test.1`), tag `vX.Y.Z-alpha.00N-v1test.00M`. N counts up across
  all V1 builds and does not restart when the dev base changes, so testers can name a build by it. Build metadata
  rather than a fourth pre-release segment because `pyproject.toml` must be PEP 440: ruff's RUF200 rejects
  `2.1.0-alpha.7.v1test.1`, while `+v1test.1` is valid PEP 440 and valid SemVer for hassfest. Release notes must say
  the V1 work stays on its branch for at least one to two months and that later `dev` pre-releases don't contain
  the V1 changes.
- Pre-releases are cut from `dev`. Exception: `v2.1.0-alpha.004` was tagged on the since-deleted `alt-test`
  branch (keep-alive connection reuse + verified-command loop, never merged) — it is not an ancestor of
  `alpha.005` or later, so compare links should go from `alpha.003`.
