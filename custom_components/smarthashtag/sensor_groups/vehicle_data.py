"""Sensors computed directly from the vehicle object.

Unlike the other sensor groups, these do not map one attribute of one data
class; each description carries a ``value_fn`` that reads the vehicle. This
covers data pysmarthashtag already fetches but did not reach Home Assistant:
the last recorded trip, the TBox state flags and a few raw status fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)


@dataclass(frozen=True, kw_only=True)
class SmartVehicleSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor whose value is read from the vehicle object."""

    value_fn: Callable[[Any], Any]


def raw_value(vehicle: Any, *path: str) -> Any:
    """Return a nested value of the raw vehicle status, or None."""
    node: Any = getattr(vehicle, "data", None)
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _status(vehicle: Any, *path: str) -> Any:
    return raw_value(vehicle, "vehicleStatus", *path)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _from_millis(value: Any) -> datetime | None:
    """Turn a millisecond epoch into an aware datetime, None if unusable."""
    millis = _as_int(value)
    if not millis:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _trip(vehicle: Any) -> Any:
    """Return the last trip if the journal holds one."""
    trip = getattr(vehicle, "last_trip", None)
    if trip is None or not getattr(trip, "trip_id", ""):
        return None
    return trip


def _trip_value(attr: str) -> Callable[[Any], Any]:
    def _value(vehicle: Any) -> Any:
        trip = _trip(vehicle)
        if trip is None:
            return None
        value = getattr(trip, attr, None)
        return getattr(value, "value", value)

    return _value


def _state_value(attr: str) -> Callable[[Any], Any]:
    def _value(vehicle: Any) -> Any:
        return getattr(getattr(vehicle, "state", None), attr, None)

    return _value


def _total_trips(vehicle: Any) -> int | None:
    trip = getattr(vehicle, "last_trip", None)
    return getattr(trip, "total_trips", None)


ENTITY_VEHICLE_DATA_DESCRIPTIONS: tuple[SmartVehicleSensorEntityDescription, ...] = (
    # --- Last recorded trip (needs trip recording switched on in the car) ---
    SmartVehicleSensorEntityDescription(
        key="last_trip_distance",
        translation_key="last_trip_distance",
        name="Last trip distance",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=1,
        value_fn=_trip_value("distance"),
    ),
    SmartVehicleSensorEntityDescription(
        key="last_trip_duration",
        translation_key="last_trip_duration",
        name="Last trip duration",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_trip_value("duration"),
    ),
    SmartVehicleSensorEntityDescription(
        key="last_trip_energy",
        translation_key="last_trip_energy",
        name="Last trip energy",
        icon="mdi:lightning-bolt",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_trip_value("energy_consumption"),
    ),
    SmartVehicleSensorEntityDescription(
        key="last_trip_average_consumption",
        translation_key="last_trip_average_consumption",
        name="Last trip average consumption",
        icon="mdi:gauge",
        native_unit_of_measurement="kWh/100km",
        suggested_display_precision=1,
        value_fn=_trip_value("avg_energy_consumption"),
    ),
    SmartVehicleSensorEntityDescription(
        key="last_trip_average_speed",
        translation_key="last_trip_average_speed",
        name="Last trip average speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        value_fn=_trip_value("avg_speed"),
    ),
    SmartVehicleSensorEntityDescription(
        key="last_trip_regenerated_energy",
        translation_key="last_trip_regenerated_energy",
        name="Last trip regenerated energy",
        icon="mdi:battery-arrow-up",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_trip_value("regenerated_energy"),
    ),
    SmartVehicleSensorEntityDescription(
        key="last_trip_end",
        translation_key="last_trip_end",
        name="Last trip end",
        icon="mdi:flag-checkered",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_trip_value("end_time"),
    ),
    SmartVehicleSensorEntityDescription(
        key="recorded_trips",
        translation_key="recorded_trips",
        name="Recorded trips",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_total_trips,
    ),
    # --- Raw status fields the library does not parse ---
    SmartVehicleSensorEntityDescription(
        key="parked_since",
        translation_key="parked_since",
        name="Parked since",
        icon="mdi:car-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: _from_millis(_status(v, "parkTime", "status")),
    ),
    SmartVehicleSensorEntityDescription(
        key="remaining_range_to_20_percent",
        translation_key="remaining_range_to_20_percent",
        name="Range to 20 %",
        icon="mdi:road-variant",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        value_fn=lambda v: _as_float(
            _status(
                v,
                "additionalVehicleStatus",
                "electricVehicleStatus",
                "distanceToEmptyOnBattery20Soc",
            )
        ),
    ),
    # The meaning of the alarm codes is not documented, so they are shown raw
    SmartVehicleSensorEntityDescription(
        key="vehicle_alarm_status",
        translation_key="vehicle_alarm_status",
        name="Alarm system status code",
        icon="mdi:shield-car",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda v: _as_int(
            _status(
                v,
                "additionalVehicleStatus",
                "drivingSafetyStatus",
                "vehicleAlarm",
                "alrmSt",
            )
        ),
    ),
    SmartVehicleSensorEntityDescription(
        key="vehicle_alarm_trigger",
        translation_key="vehicle_alarm_trigger",
        name="Alarm trigger source code",
        icon="mdi:shield-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda v: _as_int(
            _status(
                v,
                "additionalVehicleStatus",
                "drivingSafetyStatus",
                "vehicleAlarm",
                "alrmTrgSrc",
            )
        ),
    ),
    # --- TBox state flags ---
    SmartVehicleSensorEntityDescription(
        key="next_wakeup",
        translation_key="next_wakeup",
        name="Next TBox wakeup",
        icon="mdi:alarm",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_state_value("next_wakeup_time"),
    ),
    SmartVehicleSensorEntityDescription(
        key="power_mode",
        translation_key="power_mode",
        name="Power mode",
        icon="mdi:power",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_state_value("power_mode"),
    ),
)
