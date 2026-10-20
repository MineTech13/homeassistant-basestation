# VR Basestation Integration for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/MineTech13/homeassistant-basestation?style=for-the-badge)](https://github.com/MineTech13/homeassistant-basestation/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.12+-blue.svg?style=for-the-badge)](https://home-assistant.io)
[![License](https://img.shields.io/github/license/MineTech13/homeassistant-basestation?style=for-the-badge)](LICENSE)

A comprehensive Home Assistant integration for managing **Valve Index® Base Stations (V2)** and **HTC Vive Base Stations (V1)** through Bluetooth Low Energy (BLE). Control power states, monitor device information, and automate your VR setup with ease.

---

## ✨ Key Features

### 🎮 **Universal VR Lighthouse Support**
- **Valve Index Base Stations (V2)** - Full feature support
- **HTC Vive Base Stations (V1)** - On/off, confirmed working by users (see [#4](https://github.com/MineTech13/homeassistant-basestation/issues/4)). Needs the station's pair ID, see below

### 🔄 **Advanced Power Management**
- **Power Control** - Turn base stations on/off remotely
- **Standby Mode** - Turns off lasers while keeping motors spinning for short VR breaks, reducing motor wear from excessive spin-ups (V2 only)
- **Power State Monitoring** - Regular polling-based status tracking (default 60s)
- **Identify Function** - Blink LEDs to locate specific base stations

### 🛠️ **Modern Integration Features**
- **Automatic Discovery** - Zero-configuration setup via Bluetooth discovery
- **Config Flow UI** - Complete graphical configuration (no YAML required)
- **Device Information** - Firmware, model, hardware, and manufacturer details
- **Multiple Entity Types** - Switches, sensors, and buttons for comprehensive control

### ⚙️ **Professional Features**
- **Connection Management** - Shared BLE connections, retry logic and automatic steering away from misbehaving Bluetooth proxies
- **Configurable Timeouts** - User-adjustable connection and scan intervals
- **Device Registry Integration** - Proper Home Assistant device management
- **Translation Support** - Multi-language interface
- **Options Flow** - Advanced settings without reconfiguration

---

## ℹ️ How it Works

This integration communicates directly with your VR base stations using Bluetooth Low Energy (BLE).

- **Direct Connection**: No SteamVR or additional software required. Home Assistant connects directly to the base stations.
- **State Polling**: The integration periodically polls the base stations to check their power state (Sleep, Standby, On).
- **Command Queueing**: Commands (like turning on/off) are queued and sent efficiently to minimize connection attempts.
- **Auto-Discovery**: Uses Home Assistant's Bluetooth integration to automatically detect nearby base stations.

---

## 🚀 Installation

### Via HACS (Recommended)

1. **Install HACS** - Ensure HACS is installed and configured.
2. **Search for Integration** - Go to **HACS** → **Integrations** → **Explore & Download Repositories** and search for **"VR Basestation"**.
3. **Install** - Click **Download** on the integration card.
4. **Restart Home Assistant**

### Via HACS (Custom Repository)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=MineTech13&repository=homeassistant-basestation&category=integration)

1. **Add Custom Repository**:
   - Go to **HACS** → **Integrations** → **⋮** (menu) → **Custom repositories**
   - Add: `https://github.com/MineTech13/homeassistant-basestation`
   - Category: **Integration**
2. **Install Integration** - Find "VR Basestation" and click **Install**
3. **Restart Home Assistant**

### Manual Installation

1. Download the latest release from [GitHub Releases](https://github.com/MineTech13/homeassistant-basestation/releases)
2. Extract to `custom_components/basestation/` in your Home Assistant config directory
3. Restart Home Assistant

---

## ⚡ Quick Setup

### Automatic Discovery (Easiest)

1. Navigate to **Settings** → **Devices & Services**
2. Look for automatically discovered VR base stations
3. Click **Configure** and follow the setup wizard

### Manual Setup

1. **Settings** → **Devices & Services** → **Add Integration**
2. Search for **"VR Basestation"**
3. Enter MAC address and device type

### Finding MAC Addresses

If you need to find your base station MAC addresses manually:

```bash
# Using hcitool (Linux)
sudo hcitool lescan
# Look for devices starting with "LHB-" (V2) or "HTC BS" (V1)

# Using bluetoothctl
bluetoothctl
scan on
# Wait for devices to appear
```

**Alternative Methods:**
- **Android**: Use "BLE Scanner" or "nrf Connect" app from Play Store
- **Windows**: Use "Microsoft Bluetooth LE Explorer" from Windows Store
- **SteamVR**: Check device serial numbers in SteamVR settings

### V1 (HTC Vive) Pair ID

V1 base stations need their **pair ID** to accept on/off commands: the 8-character ID printed on the back of the
station (sometimes called BSID), e.g. `1A2B3C4D`. Its last 4 characters also end the station's Bluetooth name
(`HTC BS …`), which setup uses to reject a mistyped ID. With a wrong ID the station silently ignores commands.

The station also needs a Bluetooth adapter or proxy in range that can **connect** to it: a local adapter or
an ESPHome Bluetooth proxy with `active: true`. Passive scanners (e.g. Shelly gateways, ESPHome proxies without
`active: true`) still list the station in Home Assistant's Bluetooth pages, but can't send it anything. In that case
the switch stays unavailable and the log shows a warning naming the scanners that see it.

V1 stations can't report whether they are actually on. What they do report is the last on/off command they received,
and that is what the switch shows. Switching a station with SteamVR or another app (confirmed with BS Companion)
shows up after the next poll (default every 60 s). After a station lost power it shows as on, assuming it came back on by itself. If a
command doesn't show up in that read-back, the integration sends it again, and reports an error if it still doesn't
arrive.

---

## 📊 Entity Overview

Each base station creates multiple entities for comprehensive control:

### 🔘 **Switches**
- **Power Switch** - Main on/off control
- **Standby Switch** *(V2 only)* - Standby mode for short breaks (lasers off, motors spinning)

### 📈 **Sensors** *(Optional)*
- **Firmware Version** - Current firmware information
- **Model Number** - Device model details
- **Hardware Version** - Hardware revision
- **Manufacturer** - Device manufacturer
- **Channel** *(V2 only)* - Communication channel
- **Power State** *(V2 only)* - Detailed power status
- **Pair ID** *(V1 only)* - Pair identification

### 🔵 **Buttons**
- **Identify Button** *(V2 only)* - Blink LED for device identification

### 🩺 **Connection Health Sensors** *(disabled by default)*

Disabled out of the box, since they exist for tracking down Bluetooth problems rather than for
everyday use. Enable them via **Settings** → **Devices & Services** → **VR Basestation** → the
device → the entity → **Enable**, and Home Assistant will keep their history — so if a base station
starts misbehaving later, you can see exactly when it began instead of relying on logs that have
already rotated away.

- **Connection failures** - Total failed connections and operations
- **Abandoned connection attempts** - Connects that took too long to wait for (a rising count is an
  early warning that the covering Bluetooth proxy is struggling)
- **Suspected stranded proxy slots** - Connections that could not be confirmed as closed; the
  number to watch if base stations become unavailable over time
- **Out of connection slots errors** - Times the Bluetooth proxy reported it had no slots left
- **Last connection error** - Most recent error, with its type
- **Last successful connection** - When the base station was last reached

---

## 🩺 Troubleshooting

### Common issues

**Base stations not discovered:**
- Make sure Bluetooth works in Home Assistant (**Settings** → **Devices & Services** → **Bluetooth**)
- Check that the base stations are powered and in range of an adapter or proxy

**Connection timeouts or a station that keeps going unavailable:**
- Close SteamVR or other apps that talk to the base stations. A station only accepts one connection at a time
- Check range: a closer adapter or an ESPHome Bluetooth proxy in the same room helps most
- Increase the connection timeout in the device options

### ESPHome Bluetooth proxies

Most reported problems come from the proxy, not the base station:

- Use `active: true` for the proxy. Passive scanners can see a station but can't connect to it
- `wifi: power_save_mode: none` is recommended by ESPHome for Bluetooth proxies
- Busy devices that also run a display or many other components can struggle as proxies. A dedicated ESP32 proxy works best
- If a station disappears and only comes back after restarting a proxy, the proxy is still holding a connection to it.
  Restart one proxy at a time to find out which one. The proxy's own log usually shows `OPEN_EVT in unexpected state`
  around the time the station disappeared

### Reporting a problem

Known problems and workarounds are listed in [ISSUES.md](ISSUES.md). When you open an issue, please attach the diagnostics:

1. Open the base station's device page (**Settings** → **Devices & Services** → **VR Basestation** → the device)
2. Three-dot menu → **Download diagnostics**
3. Attach the file to the issue

Download them **before** reloading the integration or restarting Home Assistant or a proxy. A reload resets the
counters and the connection history, which are the useful part. The file shows the recent connection history,
error counters, whether a connection attempt is still running, and which Bluetooth adapters and proxies can see
the station. The MAC address and pair ID are removed automatically (only the last characters of the MAC are kept).

If you can, also add the Home Assistant log from around the time it happened. Availability changes, connection
problems and anything that could leave a proxy connection slot occupied are logged as warnings, so no extra
logging setup is needed. For more detail, enable debug logging:

```yaml
logger:
  logs:
    custom_components.basestation: debug
```

---

## 🔧 Advanced Configuration

### Device Options

Access advanced settings via **Settings** → **Devices & Services** → **VR Basestation** → **Configure**:

- **Device Name** - Custom friendly name
- **State Update Interval** - How often the power state is read (default 60 s)
- **Fast Polling Interval** - Faster state reads while a station boots, capped at 2 minutes (default 5 s, V2 only)
- **Device Info Scan Interval** - How often firmware, model etc. are read (default 1800 s)
- **Connection Timeout** - How long to wait for a Bluetooth connection (default 20 s, minimum 10 s)

Leave a field blank to use the default.

### Automation Integration

```yaml
# Example: Turn on base stations when lights turn on
automation:
  - alias: "VR Room Activated"
    trigger:
      - platform: state
        entity_id: light.vr_room
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id:
            - switch.valve_basestation_1
            - switch.valve_basestation_2

# Example: Auto-standby after 30 minutes of inactivity
  - alias: "VR Auto Standby"
    trigger:
      - platform: state
        entity_id: binary_sensor.vr_room_motion
        to: "off"
        for: "00:30:00"
    action:
      - service: switch.turn_on
        target:
          entity_id:
            - switch.valve_basestation_1_standby_mode
            - switch.valve_basestation_2_standby_mode
```

### Grouping Base Stations

Create groups for easy control:

1. **Settings** → **Devices & Services** → **Helpers**
2. **Create Helper** → **Group**
3. Add all base station switches
4. Control all base stations with one entity

---

## 💡 Automation Ideas

Transform your VR setup with smart automations:

- **🌡️ Climate Control** - Auto-adjust AC when VR session starts
- **💡 Lighting** - Sync base stations with room lighting
- **🏠 Presence Detection** - Turn off when leaving home
- **💻 System Integration** - Wake PC, start SteamVR, control displays
- **⏰ Scheduled Power** - Auto-standby during sleep hours
- **🔋 Energy Management** - Smart power saving based on usage patterns

---

## 🏆 Credits & Acknowledgments

### Primary Developers
- **[@MineTech13](https://github.com/MineTech13)** - Complete v2.0 architecture, config flow, device abstraction, and feature development
- **[@Invisi](https://github.com/Invisi)** - V2.0 development collaboration and testing

### Original Foundation
- **[@jariz](https://github.com/jariz)** - Original basic implementation and BLE communication foundation
- **[@TCL987](https://github.com/TCL987)** - Early improvements and community contributions
- **[@Azelphur](https://github.com/Azelphur)** - Patches and compatibility fixes

### Technical References
- **[jeroen1602/lighthouse_pm](https://github.com/jeroen1602/lighthouse_pm)** (GPLv3) - BLE protocol reference, including the V1 (Vive) pairing/power command structure
- **[ShayBox/Lighthouse](https://github.com/ShayBox/Lighthouse)** (MIT) - Reference for V1 power control that users confirmed working (write timing, matching a station to its pair ID by Bluetooth name)
- **[Home Assistant Community](https://community.home-assistant.io)** - Testing, feedback, and feature requests

---

## 📄 License

This project is licensed under the **GNU General Public License v3.0 (or later)** - see [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 MineTech13 and contributors.

---

## 📝 Technical Notes

- **BLE Range Limitation** - Bluetooth Low Energy has limited range; consider BLE proxies for extended coverage
- **Power Management** - V2 base stations support multiple power states (On, Standby, Sleep)
- **Concurrent Access** - Base stations can only be controlled by one application at a time
- **Firmware Updates** - Update base station firmware through SteamVR for best compatibility

---

## 🔗 Links

- **[GitHub Repository](https://github.com/MineTech13/homeassistant-basestation)**
- **[Issue Tracker](https://github.com/MineTech13/homeassistant-basestation/issues)**
- **[Known Issues](ISSUES.md)**
- **[Home Assistant Community](https://community.home-assistant.io)**
- **[HACS](https://hacs.xyz)**

---

*Transform your VR setup into a smart, automated experience with the VR Basestation Integration! 🎮✨*