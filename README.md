# VR Basestation Integration for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/MineTech13/homeassistant-basestation?style=for-the-badge)](https://github.com/MineTech13/homeassistant-basestation/releases)
[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2+-blue.svg?style=for-the-badge)](https://home-assistant.io)
[![License](https://img.shields.io/github/license/MineTech13/homeassistant-basestation?style=for-the-badge)](LICENSE)

A comprehensive Home Assistant integration for managing **Valve Index® Base Stations (V2)** and **HTC Vive Base Stations (V1)** ([UNTESTED](https://github.com/MineTech13/homeassistant-basestation/issues/4)) through Bluetooth Low Energy (BLE). Control power states, monitor device information, and automate your VR setup with ease.

---

## ✨ Key Features

### 🎮 **Universal VR Lighthouse Support**
- **Valve Index Base Stations (V2)** - Full feature support
- **HTC Vive Base Stations (V1)** - UNTESTED/WIP, see [#4](https://github.com/MineTech13/homeassistant-basestation/issues/4)

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
- **Connection Management** - Advanced BLE connection pooling and retry logic
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

If a base station goes unavailable and stays that way, open its device page and use the
three-dot menu → **Download diagnostics**. The file contains the recent connection history,
lifetime counters, whether a connection attempt is still in flight, and which Bluetooth proxy the
station is reachable through — enough to diagnose the problem without having had debug logging
enabled beforehand. Grab it *before* reloading the integration, which resets the counters.

For a live view, enable debug logging:

```yaml
logger:
  logs:
    custom_components.basestation: debug
```

Availability changes, lock contention, and anything that risks leaving a Bluetooth proxy
connection slot occupied are logged at warning level regardless, so they show up without debug
logging enabled.

---

## 🔧 Advanced Configuration

### Device Options

Access advanced settings via **Settings** → **Devices & Services** → **VR Basestation** → **Configure**:

- **Device Name** - Custom friendly name
- **Scan Intervals** - Adjust update frequencies
- **Connection Timeout** - BLE connection timeout
- **Power State Monitoring** - Control detailed state tracking

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

## 🛠️ Troubleshooting

### Common Issues

**Base stations not discovered:**
- Ensure Bluetooth is enabled and working
- Check base stations are powered and not in sleep mode
- Verify Home Assistant has Bluetooth access

**Connection timeouts:**
- Increase connection timeout in device options
- Check Bluetooth adapter range and interference
- Ensure base stations aren't in use by SteamVR

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
- **[Home Assistant Community](https://community.home-assistant.io)**
- **[HACS](https://hacs.xyz)**

---

*Transform your VR setup into a smart, automated experience with the VR Basestation Integration! 🎮✨*