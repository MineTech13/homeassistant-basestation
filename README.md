# VR Basestation Integration for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/MineTech13/homeassistant-basestation?style=for-the-badge)](https://github.com/MineTech13/homeassistant-basestation/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.2+-blue.svg?style=for-the-badge)](https://home-assistant.io)

A comprehensive Home Assistant integration for managing **Valve Index® Base Stations (V2)** and **HTC Vive Base Stations (V1)** ([UNTESTED](https://github.com/MineTech13/homeassistant-basestation/issues/4)) through Bluetooth Low Energy (BLE). Control power states, monitor device information, and automate your VR setup with ease.

---

## ✨ Key Features

### 🎮 **Universal VR Lighthouse Support**
- **Valve Index Base Stations (V2)** - Full feature support
- **HTC Vive Base Stations (V1)** - UNTESTED/WIP, see [#4](https://github.com/MineTech13/homeassistant-basestation/issues/4)

### 🔄 **Advanced Power Management**
- **Power Control** - Turn base stations on/off remotely
- **Standby Mode** - Energy-efficient standby for V2 base stations
- **Power State Monitoring** - Real-time status tracking
- **Identify Function** - Blink LEDs to locate specific base stations

### 🛠️ **Modern Integration Features**
- **Automatic Discovery** - Zero-configuration setup via Bluetooth discovery
- **Config Flow UI** - Complete graphical configuration (no YAML required)
- **Device Information** - Firmware, model, hardware, and manufacturer details
- **Multiple Entity Types** - Switches, sensors, and buttons for comprehensive control
- **YAML Migration** - Automatic upgrade from legacy configurations

### ⚙️ **Professional Features**
- **Connection Management** - Advanced BLE connection pooling and retry logic
- **Configurable Timeouts** - User-adjustable connection and scan intervals
- **Device Registry Integration** - Proper Home Assistant device management
- **Translation Support** - Multi-language interface
- **Options Flow** - Advanced settings without reconfiguration

---

## 🚀 Installation

### Via HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=MineTech13&repository=homeassistant-basestation&category=integration)

1. **Install HACS** - Ensure [HACS](https://hacs.xyz) is installed and configured
2. **Add Custom Repository**:
   - Go to **HACS** → **Integrations** → **⋮** (menu) → **Custom repositories**
   - Add: `https://github.com/MineTech13/homeassistant-basestation`
   - Category: **Integration**
3. **Install Integration** - Find "VR Basestation" and click **Install**
4. **Restart Home Assistant**

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
- **Standby Switch** *(V2 only)* - Energy-efficient standby mode

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

---

## 🔧 Advanced Configuration

### Device Options

Access advanced settings via **Settings** → **Devices & Services** → **VR Basestation** → **Configure**:

- **Device Name** - Custom friendly name
- **Scan Intervals** - Adjust update frequencies
- **Connection Timeout** - BLE connection timeout
- **Sensor Control** - Enable/disable specific sensors
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

### Migration from YAML

The integration automatically migrates old YAML configurations:

1. **Backup** your configuration.yaml
2. **Install** the new integration
3. **Configure** devices through the UI
4. **Remove** old YAML entries after successful migration

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
- **[jeroen1602/lighthouse_pm](https://github.com/jeroen1602/lighthouse_pm)** - BLE protocol reference
- **[Home Assistant Community](https://community.home-assistant.io)** - Testing, feedback, and feature requests

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
