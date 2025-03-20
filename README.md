# Valve Basestation integration for Homeassistant

Read and manage power states for your Valve Index® Base Stations (also referred to as 'Lighthouse V2') through [BLE](https://en.wikipedia.org/wiki/Bluetooth_Low_Energy).
This is a Combined fork from [@jariz](https://github.com/jariz/homeassistant-basestation), [@TCL987](https://github.com/TCL987/homeassistant-basestation) and the Patch from [@Azelphur](https://github.com/Azelphur/homeassistant-basestation)

![](https://jari.lol/TYc7q1qt9E.png)  

## Installation
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=minetech13&repository=homeassistant-basestation&category=integration)
- Ensure [HACS](https://hacs.xyz) is installed.
- Go to Community -> Frontend -> press the three dots (top right corner of screen) -> Custom repositories and add the following information: 
  - Add custom repository URL: https://github.com/minetech13/homeassistant-basestation 
  - Category: `Integration` 
  - Press add.
  - Now in the repository overview, click install next to this repo.

## Requirements

Before configuring Home Assistant you need a Bluetooth backend. Depending on your operating system, you may have to configure the proper Bluetooth backend for your system:

- On [Home Assistant](https://home-assistant.io/hassio/installation/): integration works out of the box.
- On [Home Assistant Container](https://home-assistant.io/docs/installation/docker/): Works out of the box with `--net=host` and properly configured Bluetooth on the host.
- On other Linux systems:
  - Ensure Bluetooth is properly configured and enabled

## Configuration

There are three ways to configure your base stations:

### 1. Automatic Setup (Recommended)
- Go to Settings -> Devices & Services
- Click "Add Integration"
- Search for "Valve Index Basestation"
- Select "Automatic Setup"
- Enter the device prefix (defaults to "LHB-")
- The integration will automatically discover and add all matching base stations
- New base stations will be automatically discovered and added

### 2. Selection from Discovered Devices
- Go to Settings -> Devices & Services
- Click "Add Integration"
- Search for "Valve Index Basestation"
- Select "Select from discovered devices"
- Choose your base station from the list of discovered Bluetooth devices
- Optionally provide a custom name

### 3. Manual Setup
- Go to Settings -> Devices & Services
- Click "Add Integration"
- Search for "Valve Index Basestation"
- Select "Manual Setup"
- Enter the MAC address of your base station
- Optionally provide a custom name

## Finding Your Base Station MAC Address

If you need to find your base station's MAC address, you can use one of these methods:

```bash
# Using hcitool
$ sudo hcitool lescan
LE Scan ...
F3:C7:68:BB:23:0B LHB-60B5777F
F7:8A:B0:FD:08:B5 LHB-F27AE376

# Using bluetoothctl
$ bluetoothctl
[bluetooth]# scan on
[NEW] Device F3:C7:68:BB:23:0B LHB-60B5777F
```

Alternatively:
- Android users can use 'BLE Scanner' from the Play Store
- Windows users can use 'Microsoft Bluetooth LE Explorer' from the Windows Store

## Automation Ideas

- Turn the airco on when your VR equipment activates
- Turn your base stations off/on when you turn off/on the lights
- Turn your base stations off if there's no motion detected in the room
- Turn off base stations when you leave the house
- Start your computer (wake on lan), VR equipment, and screen (power plug) all at once

## Grouping Base Stations

You can use Home Assistant's built-in Groups feature to control multiple base stations together:

1. Go to Settings -> Devices & Services
2. Click on "Helpers"
3. Click the "+ CREATE HELPER" button
4. Select "Group"
5. Add all your base station switches to the group

## Notes

- BLE communication range is limited
- The integration will automatically maintain connection and state
- Base stations are represented as switches in Home Assistant
- Automatic discovery will continue to look for new devices
- Integration inspired by [the miflora integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/miflora)