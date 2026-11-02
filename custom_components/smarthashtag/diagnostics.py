"""Diagnostics support for the Smart integration."""

from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import SmartHashtagConfigEntry

from .const import CONF_VEHICLE

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_VEHICLE,
    "vin",
    "vehicles",
    "userId",
    "user_id",
    "plateNo",
    "engineNo",
    "latitude",
    "longitude",
    "altitude",
    "position",
    "start_position",
    "end_position",
    "start_address",
    "end_address",
    "startAddress",
    "endAddress",
    "startLatitude",
    "startLongitude",
    "endLatitude",
    "endLongitude",
    "access_token",
    "api_access_token",
    "refresh_token",
    "token",
    "deviceId",
    "device_id",
    "iccid",
    "imsi",
    "msisdn",
    "tboxSerial",
    "temId",
    "ihuId",
    "matCode",
    "sessionId",
    "iccId",
    "imei",
    "serialNumber",
    "loginUid",
}

_SKIP_ATTRS = {"account", "data"}


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Turn library objects into plain JSON data."""
    if depth > 6:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, (datetime.datetime, datetime.date, datetime.timedelta)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in value]
    if dataclasses.is_dataclass(value):
        return {
            f.name: _jsonable(getattr(value, f.name, None), depth + 1)
            for f in dataclasses.fields(value)
        }
    if hasattr(value, "__dict__"):
        return {
            k: _jsonable(v, depth + 1)
            for k, v in vars(value).items()
            if not k.startswith("_") and k not in _SKIP_ATTRS and not callable(v)
        }
    return repr(value)


def _scrub(value: Any, secrets: set[str]) -> Any:
    """Blank out secrets that appear inside string values (e.g. embedded JSON)."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    return value


def _vehicle_diagnostics(vehicle: Any) -> dict[str, Any]:
    return {
        "raw": _jsonable(getattr(vehicle, "data", {})),
        "parsed": {
            name: _jsonable(getattr(vehicle, name, None))
            for name in (
                "battery",
                "tires",
                "maintenance",
                "running",
                "climate",
                "safety",
                "last_trip",
                "state",
                "engine_state",
                "service",
            )
        },
        "controls": sorted(
            name
            for name in dir(vehicle)
            if name.endswith("_control") and getattr(vehicle, name, None) is not None
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SmartHashtagConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    vehicles = coordinator.account.vehicles or {}
    # Identifiers that can also hide inside free-text or embedded JSON values
    secrets = {
        str(value)
        for value in (
            *vehicles,
            entry.data.get(CONF_USERNAME),
            entry.data.get(CONF_PASSWORD),
        )
        if value
    }
    return _scrub(
        {
            "entry": async_redact_data(
                {"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
            ),
            "coordinator": {
                "update_interval": str(coordinator.update_interval),
                "active_intervals": {
                    key: str(interval)
                    for key, interval in coordinator._update_intervals.items()
                },
                "last_update_success": coordinator.last_update_success,
            },
            "vehicles": [
                async_redact_data(_vehicle_diagnostics(vehicle), TO_REDACT)
                for vehicle in vehicles.values()
            ],
        },
        secrets,
    )
