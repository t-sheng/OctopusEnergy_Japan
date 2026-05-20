"""The Octopus Energy Japan integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KrakenAuthError, KrakenClient
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    LOGGER,
    PLATFORMS,
)
from .coordinator import OctopusJapanCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octopus Energy Japan from a config entry."""
    session = async_get_clientsession(hass)
    client = KrakenClient(
        session,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
    )

    coordinator = OctopusJapanCoordinator(
        hass,
        client,
        entry=entry,
        account_number=entry.data.get(CONF_ACCOUNT_NUMBER),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except KrakenAuthError as err:
        LOGGER.warning("Authentication failed for Octopus Energy Japan: %s", err)
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
