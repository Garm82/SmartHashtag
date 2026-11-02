"""Tests for the trip, TBox state, software and diagnostics entities."""

import json
from datetime import timedelta

import pytest
import respx
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pysmarthashtag.vehicle.vehicle_state import VehicleState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthashtag.const import DOMAIN, PENDING_STATE_TIMEOUT
from custom_components.smarthashtag.diagnostics import (
    async_get_config_entry_diagnostics,
)

VIN = "TestVIN0000000001"


async def _setup_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "sample_user",
            "password": "sample_password",
            "vehicle": VIN,
        },
        options={},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    return entry, coordinator, coordinator.account.vehicles[VIN]


def _entity_id(hass: HomeAssistant, platform: str, unique_suffix: str) -> str:
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.platform == DOMAIN and entry.unique_id.endswith(unique_suffix):
            if entry.domain == platform:
                return entry.entity_id
    raise AssertionError(f"No {platform} entity ending in {unique_suffix}")


@pytest.mark.asyncio()
async def test_last_trip_sensors(hass: HomeAssistant, smart_fixture: respx.Router):
    """The most recent journal entry is exposed as last trip sensors."""
    await _setup_entry(hass)

    distance = hass.states.get(_entity_id(hass, "sensor", "_last_trip_distance"))
    assert float(distance.state) == pytest.approx(15.4)
    assert distance.attributes["unit_of_measurement"] == "km"

    speed = hass.states.get(_entity_id(hass, "sensor", "_last_trip_average_speed"))
    assert float(speed.state) == pytest.approx(45.0)

    consumption = hass.states.get(
        _entity_id(hass, "sensor", "_last_trip_average_consumption")
    )
    assert float(consumption.state) == pytest.approx(20.8)

    regen = hass.states.get(_entity_id(hass, "sensor", "_last_trip_regenerated_energy"))
    assert float(regen.state) == pytest.approx(0.8)

    trips = hass.states.get(_entity_id(hass, "sensor", "_recorded_trips"))
    assert trips.state == "42"

    end = hass.states.get(_entity_id(hass, "sensor", "_last_trip_end"))
    assert end.state.startswith("2024-12-15T07:20:30")


@pytest.mark.asyncio()
async def test_last_trip_sensors_empty_without_trips(
    hass: HomeAssistant, smart_fixture: respx.Router
):
    """Without a recorded trip the trip sensors are unknown, not zero."""
    _, coordinator, vehicle = await _setup_entry(hass)
    vehicle.last_trip = type(vehicle.last_trip)(total_trips=0)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    distance = hass.states.get(_entity_id(hass, "sensor", "_last_trip_distance"))
    assert distance.state == "unknown"
    trips = hass.states.get(_entity_id(hass, "sensor", "_recorded_trips"))
    assert trips.state == "0"


@pytest.mark.asyncio()
async def test_raw_status_sensors(hass: HomeAssistant, smart_fixture: respx.Router):
    """Parking time, range to 20 % and alarm codes come from the raw status."""
    await _setup_entry(hass)

    parked = hass.states.get(_entity_id(hass, "sensor", "_parked_since"))
    assert parked.state.startswith("2024-01-17T16:03:54")

    range_20 = hass.states.get(
        _entity_id(hass, "sensor", "_remaining_range_to_20_percent")
    )
    assert range_20.state == "66.0"

    trigger = hass.states.get(_entity_id(hass, "sensor", "_vehicle_alarm_trigger"))
    assert trigger.state == "0"
    # The fixture carries no alrmSt field
    status = hass.states.get(_entity_id(hass, "sensor", "_vehicle_alarm_status"))
    assert status.state == "unknown"


@pytest.mark.asyncio()
async def test_state_binary_sensors(hass: HomeAssistant, smart_fixture: respx.Router):
    """TBox state flags are unknown until reported, then follow the flags."""
    _, coordinator, vehicle = await _setup_entry(hass)
    camping = _entity_id(hass, "binary_sensor", "_camping_mode")
    valet = _entity_id(hass, "binary_sensor", "_valet_mode")

    vehicle.state = None
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(camping).state == "unknown"

    vehicle.state = VehicleState(camping_mode_active=1, valet_mode_state=0)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(camping).state == "on"
    assert hass.states.get(valet).state == "off"


@pytest.mark.asyncio()
async def test_trip_recording_switch(
    hass: HomeAssistant, smart_fixture: respx.Router, freezer
):
    """The switch sends the journal toggle and waits for the TBox flag."""
    _, coordinator, vehicle = await _setup_entry(hass)
    entity_id = _entity_id(hass, "switch", "_trip_recording_switch")
    vehicle.state = VehicleState(journal_log_state=0)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()

    toggles = [
        json.loads(call.request.content)
        for call in smart_fixture.calls
        if call.request.method == "PUT"
        and "/vehicle/status/journalLog/" in call.request.url.path
    ]
    assert toggles, "No journal toggle was sent"
    assert hass.states.get(entity_id).state == "on"
    assert "trip_recording_switch" in coordinator._update_intervals

    # A refresh may have replaced the state; the car confirms recording
    vehicle.state = VehicleState(journal_log_state=1)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"
    assert "trip_recording_switch" not in coordinator._update_intervals

    # Unconfirmed command falls back after the timeout
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()
    vehicle.state = VehicleState(journal_log_state=1)
    freezer.tick(timedelta(seconds=PENDING_STATE_TIMEOUT + 1))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"
    assert "trip_recording_switch" not in coordinator._update_intervals


@pytest.mark.asyncio()
async def test_software_update_entity(hass: HomeAssistant, smart_fixture: respx.Router):
    """The OTA versions are reported; an offered version shows as update."""
    _, coordinator, vehicle = await _setup_entry(hass)
    entity_id = _entity_id(hass, "update", "_vehicle_software")

    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["installed_version"] == "smart OS 1.5.2 EU"

    vehicle.data["ota"] = {
        "current_version": "smart OS 1.5.2 EU",
        "target_version": "smart OS 1.6.0 EU",
    }
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["latest_version"] == "smart OS 1.6.0 EU"

    vehicle.data.pop("ota")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"


@pytest.mark.asyncio()
async def test_diagnostics_redact_personal_data(
    hass: HomeAssistant, smart_fixture: respx.Router
):
    """Diagnostics carry the raw data without VIN, account or position."""
    entry, _, vehicle = await _setup_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diagnostics)

    assert VIN not in dumped
    assert "sample_user" not in dumped
    assert "sample_password" not in dumped
    assert "ABCDEFABC" not in dumped  # engine number
    assert "123 Test Street" not in dumped  # trip address

    raw = diagnostics["vehicles"][0]["raw"]
    assert raw["vin"] == "**REDACTED**"
    assert "vehicleStatus" in raw
    assert diagnostics["vehicles"][0]["parsed"]["last_trip"]["total_trips"] == 42
    assert "charging_control" in diagnostics["vehicles"][0]["controls"]
