"""Sensor entities for the Octopus Energy Japan integration."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    UNIT_JPY,
    UNIT_JPY_PER_DAY,
    UNIT_JPY_PER_KWH,
    UNIT_JPY_PER_MONTH,
    UNIT_KWH,
)
from .coordinator import OctopusJapanCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OctopusJapanCoordinator = hass.data[DOMAIN][entry.entry_id]

    known_rate_slugs: set[str] = set()

    base_entities: list[SensorEntity] = [
        OctopusJapanCurrentRateSensor(coordinator),
        OctopusJapanLatestConsumptionSensor(coordinator),
        OctopusJapanCumulativeConsumptionSensor(coordinator),
        OctopusJapanYesterdayConsumptionSensor(coordinator),
        OctopusJapanLifetimeConsumptionSensor(coordinator),
    ]

    rate_entities: list[SensorEntity] = []
    for slug in sorted((coordinator.data or {}).get("rates", {}), key=_rate_sort_key):
        rate_entities.append(OctopusJapanRateSensor(coordinator, slug))
        known_rate_slugs.add(slug)

    async_add_entities([*base_entities, *rate_entities, OctopusJapanBasicChargeSensor(coordinator)])

    @callback
    def _maybe_add_new_rate_sensors() -> None:
        rates = (coordinator.data or {}).get("rates", {})
        new_slugs = sorted([s for s in rates if s not in known_rate_slugs], key=_rate_sort_key)
        if not new_slugs:
            return
        new_entities = [OctopusJapanRateSensor(coordinator, slug) for slug in new_slugs]
        known_rate_slugs.update(new_slugs)
        async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_maybe_add_new_rate_sensors))


def _device_info(coordinator: OctopusJapanCoordinator) -> DeviceInfo:
    account = coordinator.account_number or coordinator.entry.entry_id
    mpan = coordinator.agreement.mpan if coordinator.agreement else None
    identifier = f"{account}_{mpan}" if mpan else account
    return DeviceInfo(
        identifiers={(DOMAIN, identifier)},
        name=f"Octopus Energy Japan ({account})",
        manufacturer="Octopus Energy Japan",
        model=(coordinator.agreement.tariff_display_name if coordinator.agreement else None) or "Account",
    )


class _OEJPBaseSensor(CoordinatorEntity[OctopusJapanCoordinator], SensorEntity):
    """Common base for OEJP sensors."""

    _attr_has_entity_name = True
    _attr_attribution = "Data provided by Octopus Energy Japan"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = _device_info(coordinator)


class OctopusJapanRateSensor(_OEJPBaseSensor):
    """One sensor per electricity unit rate (JPY/kWh)."""

    _attr_native_unit_of_measurement = UNIT_JPY_PER_KWH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-jpy"

    def __init__(self, coordinator: OctopusJapanCoordinator, slug: str) -> None:
        super().__init__(coordinator)
        self._slug = slug
        rate = (coordinator.data or {}).get("rates", {}).get(slug, {})
        label = _format_rate_label(rate.get("label") or slug.replace("_", " ").title())
        self._attr_name = f"{label} Rate"
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_rate_{slug}"

    def _rate(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("rates", {}).get(self._slug, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._rate())

    @property
    def native_value(self) -> float | None:
        return _round_currency_2(_to_float(self._rate().get("value")))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rate = self._rate()
        ranges = rate.get("time_ranges") or []
        return {
            "label": rate.get("label"),
            "time_ranges": [{"start": s, "end": e} for s, e in ranges],
            "valid_from": rate.get("valid_from"),
            "valid_to": rate.get("valid_to"),
            "tariff_code": (self.coordinator.data or {}).get("tariff", {}).get("tariff_code"),
            "product_code": (self.coordinator.data or {}).get("tariff", {}).get("product_code"),
        }


class OctopusJapanCurrentRateSensor(_OEJPBaseSensor):
    """Current unit rate with a fixed half-hour basic-charge adder.

    The sensor reflects the active TOU unit rate plus a fixed per-slot share
    of the daily basic charge:
        basic_charge_jpy_per_day / 48
    """

    _attr_native_unit_of_measurement = UNIT_JPY_PER_KWH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-jpy"
    _attr_name = "Current Rate"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_current_rate"

    def _pricing_parts(self) -> tuple[float | None, float | None, float | None]:
        data = self.coordinator.data or {}
        current = data.get("current_rate") or {}
        raw_rate = _to_float(current.get("value"))

        basic_charge = data.get("basic_charge") or {}
        basic_daily = _basic_charge_to_daily_jpy(
            _to_float(basic_charge.get("value")),
            basic_charge.get("unit"),
        )

        adder = None
        if basic_daily is not None:
            adder = basic_daily / 48.0

        effective: float | None
        if raw_rate is None:
            effective = None
        elif adder is None:
            effective = raw_rate
        else:
            effective = raw_rate + adder

        return (
            _round_currency_2(effective),
            _round_currency_2(basic_daily),
            _round_currency_2(adder),
        )

    @property
    def native_value(self) -> float | None:
        effective, _, _ = self._pricing_parts()
        return effective

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        current = (self.coordinator.data or {}).get("current_rate") or {}
        effective, basic_daily, adder = self._pricing_parts()
        raw_rate = _round_currency_2(_to_float(current.get("value")))
        return {
            "active_slug": current.get("slug"),
            "active_label": current.get("label"),
            "raw_rate_jpy_per_kwh": raw_rate,
            "basic_charge_jpy_per_day": basic_daily,
            "basic_charge_component_jpy_per_30min": adder,
            "effective_rate_jpy_per_kwh": effective,
        }


class OctopusJapanBasicChargeSensor(_OEJPBaseSensor):
    """Basic / standing charge for the active tariff."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-clock"
    _attr_name = "Basic Charge"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_basic_charge"

    def _data(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get("basic_charge")

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def native_value(self) -> float | None:
        data = self._data()
        return _round_currency_2(_to_float(data.get("value"))) if data else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        data = self._data()
        if not data:
            return None
        unit = data.get("unit") or ""
        # Normalize to one of our known units; otherwise pass through.
        normalized = unit.replace("¥", "JPY").replace("yen", "JPY")
        if "/" in normalized:
            head, tail = normalized.split("/", 1)
            if "month" in tail.lower():
                return UNIT_JPY_PER_MONTH
            if "day" in tail.lower():
                return UNIT_JPY_PER_DAY
        if normalized.upper().startswith("JPY"):
            return UNIT_JPY
        return normalized or UNIT_JPY


class OctopusJapanLatestConsumptionSensor(_OEJPBaseSensor):
    """Most recent half-hourly consumption reading (kWh)."""

    _attr_native_unit_of_measurement = UNIT_KWH
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Latest Consumption"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_latest_consumption"

    def _latest(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get("latest_consumption")

    @property
    def available(self) -> bool:
        return super().available and bool(self._latest())

    @property
    def native_value(self) -> float | None:
        latest = self._latest()
        return latest.get("value") if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        latest = self._latest() or {}
        return {
            "start_at": latest.get("start_at"),
            "end_at": latest.get("end_at"),
        }


class OctopusJapanCumulativeConsumptionSensor(_OEJPBaseSensor):
    """Cumulative consumption since local midnight (kWh).

    Suitable for the Energy dashboard since it resets daily and is non-decreasing
    within a day.
    """

    _attr_native_unit_of_measurement = UNIT_KWH
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_name = "Consumption Today"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_consumption_today"
        self._last_update_time: datetime | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        value = (self.coordinator.data or {}).get("cumulative_consumption_today_kwh")
        if value is not None:
            self._last_update_time = dt_util.now()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("cumulative_consumption_today_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_update_time": self._last_update_time.isoformat() if self._last_update_time else None,
        }

    @property
    def last_reset(self) -> datetime | None:  # pragma: no cover
        # TOTAL_INCREASING handles resets implicitly; return None.
        return None


class OctopusJapanYesterdayConsumptionSensor(_OEJPBaseSensor):
    """Previous day's consumption total (kWh)."""

    _attr_native_unit_of_measurement = UNIT_KWH
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Consumption Yesterday"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_consumption_yesterday"

    def _data(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get("daily_consumption_yesterday")

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def native_value(self) -> float | None:
        data = self._data()
        return _to_float(data.get("value")) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data() or {}
        return {
            "start_at": data.get("start_at"),
            "end_at": data.get("end_at"),
        }


class OctopusJapanLifetimeConsumptionSensor(_OEJPBaseSensor, RestoreSensor):
    """Accumulated total consumption (kWh) since the sensor was first added.

    Accumulates the previous day's mature total once it has stabilized, then
    persists the running lifetime total across Home Assistant restarts via
    RestoreSensor.
    """

    _attr_native_unit_of_measurement = UNIT_KWH
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_name = "Lifetime Consumption"

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_lifetime_consumption"
        self._lifetime_kwh: float = 0.0
        self._last_seen_previous_day_key: str | None = None
        self._last_applied_previous_day_key: str | None = None

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore persisted lifetime total and the last matured day value."""
        await super().async_added_to_hass()

        # Restore the accumulated lifetime total from the last known state.
        last_state = await self.async_get_last_sensor_data()
        if last_state is not None and last_state.native_value is not None:
            try:
                self._lifetime_kwh = float(last_state.native_value)
            except (TypeError, ValueError):
                self._lifetime_kwh = 0.0

        attributes = getattr(last_state, "attributes", {}) if last_state is not None else {}
        last_applied = attributes.get("last_applied_previous_day_key")
        if last_applied is not None:
            self._last_applied_previous_day_key = str(last_applied)

    # ------------------------------------------------------------------
    # Coordinator updates
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Add the previous day's total only after it has stabilized."""
        data = self.coordinator.data or {}
        yesterday_data = data.get("daily_consumption_yesterday") or {}
        previous_day_value = _to_float(data.get("cumulative_consumption_yesterday_kwh"))
        yesterday_sensor_value = _to_float(yesterday_data.get("value"))
        previous_day_key = str(yesterday_data.get("end_at") or yesterday_data.get("start_at") or "")

        if previous_day_value is not None and previous_day_value == yesterday_sensor_value:
            if (
                self._last_seen_previous_day_key is not None
                and previous_day_key == self._last_seen_previous_day_key
                and previous_day_key != self._last_applied_previous_day_key
            ):
                self._lifetime_kwh += previous_day_value
                self._last_applied_previous_day_key = previous_day_key
            self._last_seen_previous_day_key = previous_day_key
        super()._handle_coordinator_update()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def native_value(self) -> float | None:
        return round(self._lifetime_kwh, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_seen_previous_day_key": self._last_seen_previous_day_key,
            "last_applied_previous_day_key": self._last_applied_previous_day_key,
        }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_rate_label(label: str) -> str:
    if label.startswith("Ev "):
        return "EV " + label[3:]
    if label == "Ev":
        return "EV"
    return label


def _rate_sort_key(slug: str) -> tuple[int, str]:
    priority = {
        "standard": 0,
        "ev_day_time": 1,
        "ev_night_time": 2,
    }
    return priority.get(slug, 99), slug


def _basic_charge_to_daily_jpy(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    normalized = (unit or "").replace("¥", "JPY").replace("yen", "JPY").upper()
    if "/MONTH" in normalized:
        return value / 30.0
    if "/DAY" in normalized:
        return value
    return None


def _round_currency_2(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)
