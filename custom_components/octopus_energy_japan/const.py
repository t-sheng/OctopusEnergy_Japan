"""Constants for the Octopus Energy Japan integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "octopus_energy_japan"

GRAPHQL_URL = "https://api.oejp-kraken.energy/v1/graphql/"
TZ_TOKYO = "Asia/Tokyo"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=30)
TOKEN_REFRESH_LEEWAY = timedelta(seconds=60)

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCOUNT_NUMBER = "account_number"

PLATFORMS: list[Platform] = [Platform.SENSOR]

LOGGER = logging.getLogger(__package__)

UNIT_JPY_PER_KWH = "JPY/kWh"
UNIT_JPY = "JPY"
UNIT_JPY_PER_DAY = "JPY/day"
UNIT_JPY_PER_MONTH = "JPY/month"
UNIT_KWH = "kWh"
