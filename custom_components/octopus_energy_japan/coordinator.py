"""DataUpdateCoordinator for the Octopus Energy Japan integration."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Agreement,
    KrakenAuthError,
    KrakenClient,
    KrakenConnectionError,
    KrakenError,
)
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    TZ_TOKYO,
)


class OctopusJapanCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates polling and rate-state computation for OEJP."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: KrakenClient,
        *,
        entry: ConfigEntry,
        account_number: str | None,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self.account_number = account_number
        self.agreement: Agreement | None = None
        self._tokyo_tz = ZoneInfo(TZ_TOKYO)
        self._unsub_boundary = None

    async def _async_setup(self) -> None:
        """One-shot setup: resolve account + active agreement."""
        try:
            if not self.account_number:
                self.account_number = await self.client.get_account_number()
            self.agreement = await self.client.get_active_agreement(self.account_number)
        except KrakenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (KrakenConnectionError, KrakenError) as err:
            raise UpdateFailed(f"Setup failed: {err}") from err

        # Persist newly-discovered fields back into the entry, including the
        # latest refresh token so we don't always re-login from password.
        new_data = dict(self.entry.data)
        new_data[CONF_ACCOUNT_NUMBER] = self.account_number
        if self.client.refresh_token:
            new_data[CONF_REFRESH_TOKEN] = self.client.refresh_token
        if new_data != self.entry.data:
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    async def _async_update_data(self) -> dict[str, Any]:
        if not self.account_number:
            raise UpdateFailed("Account number not resolved")

        try:
            if self.agreement is None:
                self.agreement = await self.client.get_active_agreement(self.account_number)

            tariff_data: dict[str, Any] = {"rates": [], "basic_charge": None, "raw": {}}
            if self.agreement is not None:
                tariff_data = await self.client.fetch_tariff_rates(
                    self.account_number, self.agreement
                )

            daily_readings = await self.client.get_daily_consumption_readings(self.account_number)

            # Pull a rolling 48 h window of half-hour readings so the energy
            # dashboard has overlap with prior polls.
            now_utc = datetime.now(timezone.utc)
            readings = await self.client.get_half_hourly_readings(
                self.account_number,
                now_utc - timedelta(hours=48),
                now_utc,
            )
        except KrakenAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (KrakenConnectionError, KrakenError) as err:
            raise UpdateFailed(str(err)) from err

        rates_by_slug = {r["slug"]: r for r in tariff_data["rates"]}
        current_rate = self._resolve_current_rate(tariff_data["rates"])

        # Persist the latest refresh token if it changed.
        if self.client.refresh_token and self.client.refresh_token != self.entry.data.get(
            CONF_REFRESH_TOKEN
        ):
            new_data = dict(self.entry.data)
            new_data[CONF_REFRESH_TOKEN] = self.client.refresh_token
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

        latest = readings[-1] if readings else None
        cumulative_today = _sum_readings_since_local_midnight(readings, self._tokyo_tz)
        rolling_24h = _sum_readings_since_utc(
            readings,
            datetime.now(timezone.utc) - timedelta(hours=24),
        )

        daily_today = daily_readings[0] if daily_readings else None
        daily_yesterday = daily_readings[1] if len(daily_readings) > 1 else None
        if daily_today and daily_today.get("value") is not None:
            cumulative_today = daily_today.get("value")

        # Schedule a one-shot refresh at the next TOU boundary so the
        # current-rate sensor flips on the dot.
        self._schedule_next_boundary(tariff_data["rates"])

        return {
            "tariff": _tariff_summary(self.agreement),
            "rates": rates_by_slug,
            "basic_charge": tariff_data["basic_charge"],
            "current_rate": current_rate,
            "consumption": readings,
            "latest_consumption": latest,
            "cumulative_consumption_today_kwh": cumulative_today,
            "cumulative_consumption_yesterday_kwh": daily_yesterday.get("value") if daily_yesterday else None,
            "daily_consumption_today": daily_today,
            "daily_consumption_yesterday": daily_yesterday,
            "rolling_24h_consumption_kwh": rolling_24h,
            "account_number": self.account_number,
            "mpan": self.agreement.mpan if self.agreement else None,
        }

    def _resolve_current_rate(self, rates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not rates:
            return None
        now_local = datetime.now(self._tokyo_tz)
        for rate in rates:
            if _time_in_ranges(now_local.time(), rate.get("time_ranges") or []):
                return {
                    "slug": rate["slug"],
                    "label": rate["label"],
                    "value": rate["value"],
                    "unit": rate["unit"],
                }
        # If no time_ranges matched (e.g., a flat-rate tariff with no windows),
        # fall back to the only rate we have.
        first = rates[0]
        return {
            "slug": first["slug"],
            "label": first["label"],
            "value": first["value"],
            "unit": first["unit"],
        }

    @callback
    def _schedule_next_boundary(self, rates: list[dict[str, Any]]) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None

        boundaries = sorted({
            t
            for rate in rates
            for tr in rate.get("time_ranges") or []
            for t in tr
            if t
        })
        if not boundaries:
            return

        now_local = datetime.now(self._tokyo_tz)
        next_dt: datetime | None = None
        for boundary in boundaries:
            try:
                hour, minute = (int(p) for p in boundary.split(":")[:2])
            except (ValueError, AttributeError):
                continue
            candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now_local:
                candidate += timedelta(days=1)
            if next_dt is None or candidate < next_dt:
                next_dt = candidate
        if next_dt is None:
            return

        self._unsub_boundary = async_track_point_in_time(
            self.hass,
            self._handle_boundary,
            dt_util.as_utc(next_dt),
        )

    @callback
    def _handle_boundary(self, _now: datetime) -> None:
        self._unsub_boundary = None
        self.hass.async_create_task(self.async_request_refresh())


def _tariff_summary(agreement: Agreement | None) -> dict[str, Any] | None:
    if agreement is None:
        return None
    return {
        "display_name": agreement.tariff_display_name,
        "product_code": agreement.product_code,
        "tariff_code": agreement.tariff_code,
        "typename": agreement.tariff_typename,
        "valid_from": agreement.valid_from,
        "valid_to": agreement.valid_to,
    }


def _time_in_ranges(now_t: time, ranges: list[tuple[str | None, str | None]]) -> bool:
    if not ranges:
        return False
    for start, end in ranges:
        if not start or not end:
            continue
        try:
            sh, sm = (int(p) for p in start.split(":")[:2])
            eh, em = (int(p) for p in end.split(":")[:2])
        except ValueError:
            continue
        s = time(sh, sm)
        e = time(eh, em)
        if s <= e:
            if s <= now_t < e:
                return True
        else:  # wraps over midnight (e.g. 23:00–07:00)
            if now_t >= s or now_t < e:
                return True
    return False


def _sum_readings_since_local_midnight(
    readings: list[dict[str, Any]],
    tz: ZoneInfo,
) -> float | None:
    if not readings:
        return None
    midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_local.astimezone(timezone.utc)
    total = 0.0
    found = False
    for r in readings:
        start = r.get("start_at")
        value = r.get("value")
        if value is None or not start:
            continue
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if start_dt >= midnight_utc:
            total += value
            found = True
    return total if found else None


def _sum_readings_since_utc(
    readings: list[dict[str, Any]],
    start_utc: datetime,
) -> float | None:
    if not readings:
        return None
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)

    total = 0.0
    found = False
    for r in readings:
        start = r.get("start_at")
        value = r.get("value")
        if value is None or not start:
            continue
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if start_dt >= start_utc:
            total += value
            found = True
    return total if found else None
