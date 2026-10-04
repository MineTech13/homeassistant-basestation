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
4. Only then restart — preferably **one proxy at a time** (the one with the last failed attempt for the
   station first, see the fifth incident below) rather than `script.restart_ble_proxys`, so the culprit is
   identifiable.
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
- **Restart result:** _pending. Plan: ble-tracker first (last Couch attempt), then nspanel. Record which restart
  brought back which station._

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
  `_add_connect_failure(address)`. Two failures always outweigh the RSSI gap between the top two paths, so HA picks the
  other proxy next time. The penalty clears when that proxy next succeeds for the station. Uses private habluetooth
  internals with `getattr` fallbacks (degrades to a no-op), so **re-check on habluetooth bumps**. It logs a WARNING
  once per proxy in a row. Visible as `proxy_penalised` / `Outcome.PROXY_PENALISED`. It cannot help a station only one
  proxy can reach, and does nothing for the Couch-type phantom link (the station is gone from every proxy).

Both are unverified on hardware. After deploying, expect: after a connect timeout, at most one connect per 3 min to
that station. On a recurrence of the PC Ecke mode, one WARNING naming nspanel, then its connects moving to
`ble-tracker` in the proxy logbook, with no sustained teardown loop for that address.

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

## Releasing

- Set the exact version in **both** `custom_components/basestation/manifest.json` and `pyproject.toml` before
  tagging — for pre-releases too, so HA/HACS show which alpha is actually installed.
- Pre-release versions use `X.Y.Z-alpha.N` **without leading zeros** (e.g. `2.1.0-alpha.5`). The tag spelling
  `alpha.005` is not valid SemVer (leading zero), AwesomeVersion classifies it as UNKNOWN and hassfest rejects
  it. `X.Y.Z-alpha.N` sorts above the previous stable and below the final `X.Y.Z`. Git tags keep the existing
  `vX.Y.Z-alpha.00N` spelling.
- Order: lint gate (`ruff format --check . && ruff check . && mypy custom_components/basestation`) → bump
  commit → push `dev` → tag → `gh release create <tag> --prerelease --target dev`.
- Pre-releases are cut from `dev`. Exception: `v2.1.0-alpha.004` was tagged on the since-deleted `alt-test`
  branch (keep-alive connection reuse + verified-command loop, never merged) — it is not an ancestor of
  `alpha.005` or later, so compare links should go from `alpha.003`.
