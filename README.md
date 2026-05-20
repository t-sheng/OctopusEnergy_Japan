# Octopus Energy Japan — Home Assistant Integration

A HACS-compatible Home Assistant custom component that exposes electricity rates and consumption from your [Octopus Energy Japan](https://octopusenergy.co.jp) account, using the public Kraken GraphQL API at `https://api.oejp-kraken.energy/v1/graphql/`.

Each unit rate the API returns for your active tariff is exposed as its own sensor, plus a few convenience sensors for the currently-active rate, the basic charge, and recent half-hourly consumption.

## Features

- UI-based config flow (no YAML required) — sign in with your Octopus Japan email + password.
- One sensor per unit rate on your active tariff (e.g. EV Octopus day / night rates) in `JPY/kWh`.
- A `Current Rate` sensor that resolves to whichever time-of-use period is active in `Asia/Tokyo` right now, refreshes the moment a TOU window flips, and includes a fixed per-slot basic-charge component (`basic_charge_per_day / 48`).
- A `Basic Charge` sensor in the unit the API returns (per month / per day).
- A `Latest Consumption` sensor (kWh) showing the most recent half-hourly reading.
- A `Consumption Today` sensor (kWh, `total_increasing`) wired up for the Home Assistant Energy dashboard.
- Token refresh handled transparently; reauth flow prompts for the password if the refresh token is rejected.

## Installation

### Via HACS (custom repository)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Install **Octopus Energy Japan** and restart Home Assistant.

### Manual

Copy `custom_components/octopus_energy_japan/` into your Home Assistant config directory:

```
<ha-config>/custom_components/octopus_energy_japan/
```

Then restart Home Assistant.

## Configuration

**Settings → Devices & Services → Add Integration → Octopus Energy Japan**, then enter the email and password you use for the Octopus Japan customer portal.

## Sensors created

| Sensor | Unit | Notes |
| --- | --- | --- |
| `sensor.octopus_energy_japan_<label>_rate` | `JPY/kWh` | One per TOU rate component returned by the API. Attributes include `time_ranges`, `valid_from`, `valid_to`, `tariff_code`. |
| `sensor.octopus_energy_japan_current_rate` | `JPY/kWh` | Active Asia/Tokyo unit rate with an added fixed per-30-minute basic-charge component (`basic_charge_per_day / 48`). |
| `sensor.octopus_energy_japan_basic_charge` | `JPY/month` or `JPY/day` | Basic / standing charge. |
| `sensor.octopus_energy_japan_latest_consumption` | `kWh` | Most recent half-hourly reading. |
| `sensor.octopus_energy_japan_consumption_today` | `kWh` | Cumulative since local midnight; `total_increasing` for the Energy dashboard. |

Rate-like sensor values are exposed rounded to 2 decimal places.

## Troubleshooting

The OEJP GraphQL schema is not publicly documented. If your tariff returns an unfamiliar shape and rate sensors are missing, enable debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.octopus_energy_japan: debug
```

The integration logs the resolved tariff field names, raw GraphQL responses for the tariff query, and any field-discovery fallbacks. Open an issue with that DEBUG output and we can teach the integration about the new shape.

## Disclaimer

Not affiliated with TG Octopus Energy K.K. Use at your own risk; respect the Octopus Energy Japan API terms of service.
