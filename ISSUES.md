# Known issues

Problems we know about, what causes them, and what you can do. If yours isn't here, please
[open an issue](https://github.com/MineTech13/homeassistant-basestation/issues/new/choose) and attach the diagnostics
(see [Reporting a problem](#reporting-a-problem) below).

## A station disappears and only comes back after restarting a Bluetooth proxy

The station is unavailable, and Home Assistant's Bluetooth page no longer lists it from any proxy. Restarting one of
the ESPHome proxies brings it back within a minute.

The proxy gave up on a connection attempt, but its Bluetooth chip finished the connection anyway. The station is
now connected to the proxy, so it stops advertising and nothing else can see it. The proxy's log shows
`OPEN_EVT in unexpected state` (or `status=133`) around that time. This happens inside the ESPHome proxy, the
integration can't see or close that connection.

What helps:
- Restart the proxies one at a time to find the one holding the connection.
- Use a dedicated ESP32 proxy. So far every case we could trace was on a proxy that also ran a display and a lot of
  other components.
- `wifi: power_save_mode: none` on the proxy.

## A second station keeps flapping with `Not connected`

Often right after the problem above, another station behind the same proxy goes on and off, and the log shows
`Bluetooth GATT Error … Not connected`. The proxy's log shows `Timeout waiting for teardown, forcing IDLE`.

The proxy reports connections that aren't really there. The integration notices this and makes Home Assistant
prefer another proxy for that station (one warning in the log: `… reported a connection … that was not really
there`). This only helps if another adapter or proxy can reach the station, and it is a preference, not a hard
switch. Restarting the misbehaving proxy fixes it.

## A station is unavailable for a few minutes after a Home Assistant restart

All stations connect at the same moment after a restart. If one of those connects times out, the integration waits
3 minutes before trying that station again, so it doesn't pile up connection attempts on the proxy. It comes back on
its own after about 4 minutes.

## Stations are seen in Home Assistant but stay unavailable

Home Assistant can hear a station through a passive scanner (for example an ESPHome proxy without `active: true`, or
some gateways), but it needs an adapter or proxy that can **connect**. Set `active: true` on the ESPHome proxy or add
a local Bluetooth adapter.

## V2 station shows "Starting Up" for a long time after leaving standby

Some stations keep reporting `Starting Up` for hours after leaving standby. The
integration polls faster while a station boots, but only for 2 minutes per boot, then falls back to the normal
interval and logs one warning.

## V1 (HTC Vive) stations

V1 stations can't report whether they are actually on, only the last on/off command they received. The switch shows
that, so:
- Switching with another app shows up after the next poll, as long as that app sends the same commands (BS Companion
  does). Anything else is ignored and the switch keeps its state until you switch the station from Home Assistant.
- After a station lost power it shows as on, which assumes the station came back on by itself.
- A wrong pair ID can't be detected this way. Setup rejects an ID that doesn't match the station's Bluetooth name. If a
  command still fails with "did not take the command" after 3 tries, check the pair ID on the back of the station.
- Every poll connects to the station (default every 60 s). If that is too often for your Bluetooth proxy, raise the
  **State Update Interval** in the options.

Testing and background: [#4](https://github.com/MineTech13/homeassistant-basestation/issues/4).

## Reporting a problem

1. Open the base station's device page (**Settings** → **Devices & Services** → **VR Basestation** → the device).
2. Three-dot menu → **Download diagnostics**. Do this **before** reloading the integration or restarting Home
   Assistant or a proxy, because that resets the history in the file.
3. [Open an issue](https://github.com/MineTech13/homeassistant-basestation/issues/new/choose) and attach the file.

The diagnostics contain the recent connection history, error counters and which adapters and proxies see the
station. The MAC address and pair ID are removed automatically. If you use ESPHome proxies, their logs from around
the same time help a lot too.
