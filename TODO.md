# TODO

Maintainer work list. Delete an entry once it is fixed.

## 2.1.1: open findings from the 2.1.0 release review

From the review of [PR #18](https://github.com/MineTech13/homeassistant-basestation/pull/18), each checked against `dev`
at `b890651` (2026-10-19). The five blockers from that review were fixed before 2.1.0 shipped. These are the rest,
ordered by impact. Function names are given instead of line numbers, since those drift.

### 1. One failed info refresh marks the station unavailable

- **Where:** `device.py`, `read_device_info()`: `_record_connection_failure()` is called inside the retry loop.
- **Problem:** It counts one failure per attempt (`INFO_READ_RETRIES` = 3), while a state poll counts one per cycle.
  `UNAVAILABLE_AFTER_CONSECUTIVE_FAILURES` is also 3, so a single failed refresh is enough.
- **When it happens:** Only connect timeouts start the cooldown that ends the retries early. Other failures run all
  three attempts within about 10 s: out of slots, proxy restarting, a station that stopped advertising. All
  entities flap to unavailable. On V1 it is worse: `ViveBasestationDevice.update()` keeps the station unavailable for
  `UNAVAILABLE_RETRY_COOLDOWN` (300 s), and HA doesn't run entity services on unavailable entities, so the station
  can't be switched for 5 minutes.
- **Fix:** Call `_record_connection_failure()` once, after the loop, only if every attempt failed.

### 2. Info sensors drop cached values after a partial read

- **Where:** `device.py`, `read_device_info()` returns `info` (only what this pass read) instead of the merged
  `self._info`. `BasestationInfoCoordinator` uses that as `coordinator.data`, and `BasestationInfoSensor.native_value`
  reads from it.
- **When it happens:** On a refresh the firmware read fails but model and channel succeed. Firmware shows `unknown`
  until the next complete read, 30 minutes later by default. On V1, `_read_specific_info()` always succeeds when a
  pair ID is set, so a refresh where every standard read failed still returns `{pair_id}` and blanks firmware,
  model, hardware and manufacturer.
- **Fix:** After `self._info |= info`, return `dict(self._info)`.

### 3. Info sensors stay `unknown` for 30 minutes after a failed first read

- **Where:** `__init__.py` starts one info refresh at setup. `read_device_info()` returns the empty `self._info`
  without raising when the lock times out or the connect cooldown is active. The coordinator counts that as a
  successful update.
- **When it happens:** After an HA restart the first connect times out, which starts the 180 s cooldown. The info
  refresh is skipped and returns `{}`. The next try is after `info_scan_interval` (1800 s by default, up to 24 h), so
  Firmware, Model and Channel stay `unknown` for 30 minutes although the station is back after about 4.
- **Fix:** In `BasestationInfoCoordinator._async_update_data()`, while `device.has_cached_info` is False, set a short
  `update_interval` (e.g. `CONNECT_TIMEOUT_COOLDOWN` + 30 s) and go back to `info_scan_interval` once info is in.
- Also mentioned as a known limitation in the 2.1.0 release notes.

### 4. Old 10 s connection timeout survives the upgrade from 2.0.1

- **Where:** `__init__.py`, `async_setup_entry()`, plus the config entry version in `config_flow.py` (`VERSION = 1`,
  no `async_migrate_entry` yet).
- **Problem:** The 2.0.1 options form pre-filled its default of 10 s and saved it on every submit. Entries that
  stored it keep 10 s (now the minimum), and the new default of 20 s never applies to them. Values of 5 to 9 are
  clamped to 10 at setup, but the options form then suggests the stored value and fails `vol.Range(min=10)` on a
  field the user never touched.
- **When it happens:** Through ESPHome proxies, connects often take 10 to 20 s. Each one is given up at 10 s, starts
  the 180 s cooldown, finishes anyway and is closed again. The station is then effectively read every 3 minutes.
- **Workaround (in the 2.1.0 release notes):** clear the field under **Configure**.
- **Fix:** Add `async_migrate_entry` with a `MINOR_VERSION` bump that removes `connection_timeout` from the options
  when it is 10 or less, so the entry follows the default.

### 5. Fake connections during info reads aren't penalised

- **Where:** `device.py`, `_read_standard_characteristics()` and V2 `_read_specific_info()` catch `BleakError` per
  characteristic.
- **Problem:** A phantom connection (`Not connected` on the first read) never reaches
  `_react_to_attempt_failure()`, so `_penalise_connected_proxy()` doesn't run. Every fake success also clears HA's
  failure count for that proxy (see CLAUDE.md, "Proxy faking connections"). An info refresh can make three of them,
  which undoes the penalty the state polls applied.
- **Fix:** In the per-characteristic handlers, re-raise when `_is_phantom_connection_error(err)` so the attempt
  fails through the normal path. Check it against the habluetooth version HA actually ships, as described in
  CLAUDE.md.

### 6. Fast polling continues when polls fail

- **Where:** `coordinator.py`, `BasestationCoordinator._async_update_data()`: the interval logic and the
  `MAX_FAST_POLLING_DURATION` cap only run in the `else:` (success) branch.
- **When it happens:** A station starts booting, so polling switches to 5 s. Then `update()` starts raising or
  exceeding the coordinator timeout (`connection_timeout * 3 + 10`), e.g. a GATT read that hangs, or a long lock wait
  plus two slow connects. The interval stays at 5 s and the 120 s cap is never checked. Normal failures don't hit
  this, because `async_ble_operation()` returns `False` instead of raising.
- **Fix:** Run the interval logic in a `finally`, or fall back to `default_interval` and reset `_boot_seen_at` in
  the `except` branches.

### 7. Firmware and hardware version missing on the device page

- **Where:** `device.py`, `device_info` property. Entities copy it in their `__init__`
  (`_attr_device_info = device.device_info`).
- **Problem:** The info read starts as a background task right before the platforms are set up, so `_info` is still
  empty and `sw_version`/`hw_version` are `None`. Nothing updates the device registry later, and every reload writes
  `None` again.
- **Fix:** Leave `sw_version`/`hw_version` out of `DeviceInfo` while unknown (a key set to `None` overwrites the
  registry). After a successful info read, update the device with
  `dr.async_get(hass).async_update_device(device_id, sw_version=..., hw_version=...)`, e.g. from a listener on the
  info coordinator in `__init__.py`.

### 8. Wrong outage duration in "available again after Xs"

- **Where:** `device.py`, `_set_available()` computes the downtime from `_last_failure_time`.
- **Problem:** Every failure updates `_last_failure_time`, so this measures from the last failure, not from when
  the station went unavailable. If it went unavailable because it stopped advertising (no failure recorded), the
  number comes from an older, unrelated failure. The log line and the connection history both get the wrong value.
- **Fix:** Store `_unavailable_since` when switching to unavailable and use it for the duration.

### Checking a fix

There is no test suite. Run the lint gate
(`ruff format --check . && ruff check . && mypy custom_components/basestation`), then try it in `scripts/develop`
or on real hardware. Items 1, 3 and 5 are easiest to trigger with a proxy switched off or a station out of range.
