"""Support for the Smart vehicle software version."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.update import UpdateEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .const import CONF_VEHICLE, LOGGER
from .coordinator import SmartHashtagDataUpdateCoordinator
from .entity import SmartHashtagEntity

if TYPE_CHECKING:
    from . import SmartHashtagConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: SmartHashtagConfigEntry, async_add_entities
):
    """Set up the vehicle software update entity."""
    coordinator = entry.runtime_data
    vehicle = coordinator.config_entry.data.get(CONF_VEHICLE)

    if vehicle not in (coordinator.account.vehicles or {}):
        LOGGER.error("Vehicle %s not available; skipping update setup", vehicle)
        return

    async_add_entities([SmartSoftwareUpdate(coordinator, vehicle)])


class SmartSoftwareUpdate(SmartHashtagEntity, UpdateEntity):
    """
    Shows the installed and the offered software version of the vehicle.

    The versions come from the OTA service the library polls. Installing is
    done in the car, so the entity only reports whether an update is waiting.
    """

    _attr_translation_key = "vehicle_software"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmartHashtagDataUpdateCoordinator,
        vin: str,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{self._attr_unique_id}_{vin}_vehicle_software"

    def _ota(self) -> dict:
        vehicle = (self.coordinator.account.vehicles or {}).get(self._vin)
        ota = getattr(vehicle, "data", {}).get("ota") if vehicle else None
        return ota if isinstance(ota, dict) else {}

    @property
    def installed_version(self) -> str | None:
        """Return the software version installed in the car."""
        return self._ota().get("current_version") or None

    @property
    def latest_version(self) -> str | None:
        """Return the newest version offered, or the installed one."""
        ota = self._ota()
        return ota.get("target_version") or ota.get("current_version") or None
