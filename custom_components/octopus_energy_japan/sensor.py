"""Sensor entities for the Octopus Energy Japan integration."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
from homeassistant.helpers.event import async_track_point_in_time
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
        OctopusJapanBasicChargeSensor(coordinator),
        OctopusJapanLatestConsumptionSensor(coordinator),
        OctopusJapanCumulativeConsumptionSensor(coordinator),
        OctopusJapanLifetimeConsumptionSensor(coordinator),
    ]

    rate_entities: list[SensorEntity] = []
    for slug in (coordinator.data or {}).get("rates", {}):
        rate_entities.append(OctopusJapanRateSensor(coordinator, slug))
        known_rate_slugs.add(slug)

    async_add_entities([*base_entities, *rate_entities])

    @callback
    def _maybe_add_new_rate_sensors() -> None:
        rates = (coordinator.data or {}).get("rates", {})
        new_slugs = [s for s in rates if s not in known_rate_slugs]
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
        label = rate.get("label") or slug.replace("_", " ").title()
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


class OctopusJapanLifetimeConsumptionSensor(_OEJPBaseSensor, RestoreSensor):
    """Accumulated total consumption (kWh) since the sensor was first added.

    Captures the last known value of "Consumption Today" before it resets at
    midnight, then adds it to the running lifetime total which is persisted
    across Home Assistant restarts via RestoreSensor.
    """

    _attr_native_unit_of_measurement = UNIT_KWH
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_name = "Lifetime Consumption"

    # Belt-and-suspenders: if today's value drops by more than this threshold
    # it is treated as a midnight rollover even if the scheduled callback was missed.
    _ROLLOVER_THRESHOLD = 0.1

    def __init__(self, coordinator: OctopusJapanCoordinator) -> None:
        super().__init__(coordinator)
        account = coordinator.account_number or coordinator.entry.entry_id
        mpan = coordinator.agreement.mpan if coordinator.agreement else "unknown"
        self._attr_unique_id = f"{account}_{mpan}_lifetime_consumption"
        self._lifetime_kwh: float = 0.0
        self._today_max_kwh: float = 0.0
        self._unsub_midnight = None
        self._tokyo_tz = ZoneInfo("Asia/Tokyo")

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restore persisted lifetime total and seed today's max."""
        await super().async_added_to_hass()

        # Restore the accumulated lifetime total from the last known state.
        last_state = await self.async_get_last_sensor_data()
        if last_state is not None and last_state.native_value is not None:
            try:
                self._lifetime_kwh = float(last_state.native_value)
            except (TypeError, ValueError):
                self._lifetime_kwh = 0.0

        # Seed today's running max from current coordinator data (best-effort on
        # restart — if we're mid-day this keeps the max roughly correct).
        today = (self.coordinator.data or {}).get("cumulative_consumption_today_kwh")
        if today is not None:
            self._today_max_kwh = float(today)

        self._schedule_midnight()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the scheduled midnight callback."""
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Midnight snapshot
    # ------------------------------------------------------------------

    def _schedule_midnight(self) -> None:
        """Schedule the next JST midnight snapshot."""
        if self._unsub_midnight is not None:
            self._unsub_midnight()
            self._unsub_midnight = None

        now_local = datetime.now(self._tokyo_tz)
        next_midnight_local = (now_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self._unsub_midnight = async_track_point_in_time(
            self.hass,
            self._handle_midnight,
            dt_util.as_utc(next_midnight_local),
        )

    @callback
    def _handle_midnight(self, _now: datetime) -> None:
        """Fold today's max consumption into the lifetime total at midnight."""
        self._unsub_midnight = None
        if self._today_max_kwh > 0:
            self._lifetime_kwh += self._today_max_kwh
        self._today_max_kwh = 0.0
        self.async_write_ha_state()
        self._schedule_midnight()

    # ------------------------------------------------------------------
    # Coordinator updates
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Track rolling maximum of today's consumption; detect missed rollovers."""
        today = (self.coordinator.data or {}).get("cumulative_consumption_today_kwh")
        if today is not None:
            today = float(today)
            if today < self._today_max_kwh - self._ROLLOVER_THRESHOLD:
                # The value dropped significantly — midnight rolled over but the
                # scheduled callback was missed (e.g. HA was restarting at midnight).
                self._lifetime_kwh += self._today_max_kwh
                self._today_max_kwh = today
            elif today > self._today_max_kwh:
                self._today_max_kwh = today
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
            "today_max_kwh": round(self._today_max_kwh, 3),
        }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
