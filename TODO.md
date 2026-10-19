# TODO

Maintainer work list. Delete an entry once it is fixed.

## 2.1.1: open findings from the 2.1.0 release review

From the review of [PR #18](https://github.com/MineTech13/homeassistant-basestation/pull/18), each checked against `dev`
at `b890651` (2026-10-19). The five blockers from that review were fixed before 2.1.0 shipped. These are the rest,
ordered by impact. Function names are given instead of line numbers, since those drift.

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
