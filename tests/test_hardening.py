"""Tests for reauth, duplicate entries, option ranges and smaller fixes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pysmarthashtag.models import SmartAPIError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthashtag.const import (
    CONF_CONDITIONING_TEMP,
    DOMAIN,
    MAX_CONDITIONING_TEMP,
)
from custom_components.smarthashtag.sensor import SmartHashtagBatteryRangeSensor
from custom_components.smarthashtag.sensor_groups import ENTITY_BATTERY_DESCRIPTIONS

VIN = "TestVIN0000000001"


def _entry(**data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"Smart {VIN}",
        data={
            "username": "sample_user",
            "password": "old_password",
            "vehicle": VIN,
            **data,
        },
        options={},
    )


def _mock_account(vehicles: dict | None = None, login_error=None) -> MagicMock:
    account = MagicMock()
    account.login = AsyncMock(side_effect=login_error)
    account.get_vehicles = AsyncMock()
    account.vehicles = vehicles if vehicles is not None else {VIN: object()}
    return account


@pytest.mark.asyncio()
async def test_reauth_updates_password_and_reloads(hass: HomeAssistant):
    """A new password is stored and the entry reloaded."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == "sample_user"

    with (
        patch(
            "custom_components.smarthashtag.config_flow.SmartAccount",
            return_value=_mock_account(),
        ),
        patch(
            "custom_components.smarthashtag.async_setup_entry", return_value=True
        ) as mock_setup,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new_password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new_password"
    assert entry.data["username"] == "sample_user"
    assert len(mock_setup.mock_calls) == 1


@pytest.mark.asyncio()
async def test_reauth_wrong_password_shows_error(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.smarthashtag.config_flow.SmartAccount",
        return_value=_mock_account(login_error=SmartAPIError("bad login")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "auth"}
    assert entry.data["password"] == "old_password"


@pytest.mark.asyncio()
async def test_reauth_vehicle_gone_shows_error(hass: HomeAssistant):
    """Valid login but the VIN is no longer in the account (cloud code 8040)."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.smarthashtag.config_flow.SmartAccount",
        return_value=_mock_account(vehicles={"OtherVIN": object()}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new_password"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "vehicle_not_found"}
    assert entry.data["password"] == "old_password"


@pytest.mark.asyncio()
async def test_same_vehicle_cannot_be_added_twice(hass: HomeAssistant):
    _entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.smarthashtag.config_flow.SmartAccount",
        return_value=_mock_account(),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "sample_user", "password": "pw"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.asyncio()
async def test_conditioning_temp_option_is_limited(
    hass: HomeAssistant, smart_fixture: respx.Router
):
    """The options flow keeps the temperature in the range the car accepts."""
    entry = _entry(password="sample_password")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONDITIONING_TEMP: 45}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CONDITIONING_TEMP] == MAX_CONDITIONING_TEMP


@pytest.mark.asyncio()
async def test_battery_sensor_without_unit_reports_no_unit(
    hass: HomeAssistant, smart_fixture: respx.Router
):
    """Plain battery values (flags, raw codes) must not report themselves as unit."""
    entry = _entry(password="sample_password")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    vehicle = coordinator.account.vehicles[VIN]
    vehicle.battery.charging_status_raw = 2
    vehicle.battery.is_charger_connected = True

    descriptions = {d.key: d for d in ENTITY_BATTERY_DESCRIPTIONS}
    for key in ("charging_status_raw", "is_charger_connected"):
        description = descriptions[key]
        sensor = SmartHashtagBatteryRangeSensor(
            coordinator,
            type(description)(**{**description.__dict__, "key": f"{VIN}_{key}"}),
        )
        assert sensor.native_unit_of_measurement is None, key


@pytest.mark.asyncio()
async def test_window_ventilation_does_not_poll_fast(
    hass: HomeAssistant, smart_fixture: respx.Router
):
    """The car reports no ventilation state, so there is nothing to poll for."""
    entry = _entry(password="sample_password")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    entity_id = next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith("_window_ventilation")
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "on"
    assert "window_ventilation" not in coordinator._update_intervals


@pytest.mark.asyncio()
async def test_setup_fetches_vehicle_data_once(
    hass: HomeAssistant, smart_fixture: respx.Router
):
    """Setting up the entry runs a single full refresh against the cloud."""
    entry = _entry(password="sample_password")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    status_calls = [
        call
        for call in smart_fixture.calls
        if call.request.method == "GET"
        and call.request.url.path == f"/remote-control/vehicle/status/{VIN}"
    ]
    assert len(status_calls) == 1
