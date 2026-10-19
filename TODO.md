# TODO

Maintainer work list. Delete an entry once it is fixed.

## 2.1.1: open findings from the 2.1.0 release review

From the review of [PR #18](https://github.com/MineTech13/homeassistant-basestation/pull/18), each checked against `dev`
at `b890651` (2026-10-19). The five blockers from that review were fixed before 2.1.0 shipped. These are the rest,
ordered by impact. Function names are given instead of line numbers, since those drift.

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
