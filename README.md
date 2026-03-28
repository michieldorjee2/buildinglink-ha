# BuildingLink for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that fetches package/delivery notifications from [BuildingLink](https://www.buildinglink.com).

## Features

- Tracks open deliveries as a sensor (`sensor.buildinglink_deliveries`)
- Shows delivery count as the sensor state
- Exposes delivery details (description, type, location, date) as attributes
- Polls every 5 minutes (configurable)
- Runs entirely within Home Assistant — no external server needed

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > **Custom repositories**
3. Add this repository URL and select **Integration** as the category
4. Search for "BuildingLink" and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/buildinglink` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services**
2. Click **Add Integration**
3. Search for **BuildingLink**
4. Enter your BuildingLink username and password
5. The integration will validate your credentials and create a delivery sensor

## Sensor

| Entity | Description |
|---|---|
| `sensor.buildinglink_deliveries` | Number of open/pending deliveries |

### Attributes

Each delivery in the `deliveries` attribute list contains:

| Field | Description |
|---|---|
| `id` | Delivery ID |
| `description` | Package description |
| `type` | Delivery type (e.g., "Amazon Package", "FedEx") |
| `location` | Where the package is stored (e.g., "Lobby") |
| `open_date` | When the delivery was logged |

## Automations

Example automation to notify when a new delivery arrives:

```yaml
automation:
  - alias: "New BuildingLink Delivery"
    trigger:
      - platform: numeric_state
        entity_id: sensor.buildinglink_deliveries
        above: 0
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "Package Delivered"
          message: >
            You have {{ states('sensor.buildinglink_deliveries') }} package(s) waiting.
```

## Credits

Based on the [buildinglink-mcp](https://github.com/johnagan/buildinglink) TypeScript client by John Agan.

## License

MIT
