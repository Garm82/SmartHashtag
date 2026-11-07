"""DataUpdateCoordinator for Smart #1/#3."""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pysmarthashtag.account import SmartAccount
from pysmarthashtag.models import SmartAPIError, SmartAuthError, SmartRemoteServiceError

try:
    # Typed "VIN no longer bound to this account" (cloud code 8040), added in the
    # token-lifecycle pysmarthashtag. Fall back to a never-raised placeholder on
    # older library versions so the except clause stays harmless.
    from pysmarthashtag.models import SmartVehicleUnboundError
except ImportError:  # pragma: no cover - depends on installed pysmarthashtag

    class SmartVehicleUnboundError(SmartAPIError):  # noqa: N818
        """Placeholder for older pysmarthashtag without the typed unbound error."""


from .const import (
    CONF_CHARGING_INTERVAL,
    CONF_DRIVING_INTERVAL,
    DEFAULT_CHARGING_INTERVAL,
    DEFAULT_DRIVING_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    UNBOUND_VIN_AUTH_MESSAGE,
)

# Maximum consecutive transient failures before raising UpdateFailed
# Set high enough to tolerate multiple internal API calls failing within a single refresh
MAX_TRANSIENT_FAILURES = 10

# Consecutive 8040s before the VIN is considered genuinely unbound.
# A single 8040 is routinely transient: the cloud answers "no vehicle
# information bounded with this VIN and UID" for a moment right after a
# session refresh, before it re-associates the VIN with the new session,
# and the next poll succeeds. Escalating on the first one turns a blip
# into a reauth prompt that keeps every entity unavailable until someone
# reloads the entry by hand.
UNBOUND_FAILURES_BEFORE_REAUTH = 3

# Timeout (seconds) for a full vehicle data refresh. A healthy refresh is a
# chain of sequential Smart/Geely cloud calls that can legitimately take ~20s.
API_TIMEOUT = 30


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class SmartHashtagDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Smart Web API."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, account: SmartAccount, *, entry: ConfigEntry
    ) -> None:
        """
        Initialize a SmartHashtagDataUpdateCoordinator instance.

        This constructor sets up the data update coordinator with the provided Home Assistant
        instance, Smart account, and configuration entry. It initializes the coordinator with the
        scan interval from the entry options (default 5 minutes) and prepares an internal
        dictionary to track update intervals for various keys.

        Parameters:
            hass (HomeAssistant): The Home Assistant instance.
            account (SmartAccount): An instance used to interact with the Smart Web API.
            entry (ConfigEntry): The configuration entry containing integration settings.
        """
        self.account = account
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
            config_entry=entry,
        )
        self._update_intervals: dict[str, timedelta] = {}
        self._interval_expiry: dict[str, datetime] = {}
        self._consecutive_failures = 0
        self._unbound_failures = 0
        self._last_error: str | None = None

    async def _async_update_data(self):
        """
        Asynchronously fetch vehicle data from the Smart API.

        This coroutine retrieves the latest vehicle data by calling the account's
        asynchronous get_vehicles method. Authentication and remote service issues are
        handled by raising appropriate exceptions, while any API errors are logged and
        return the last known data to keep entities available.

        Returns:
            Any: The vehicle data as returned by self.account.get_vehicles(), or the
                 last known data if a SmartAPIError or HTTP error is encountered.

        Note:
            self.data can be None in the following scenarios:
            - On first run if the API call fails (SmartAPIError or HTTP error)
            - During entity setup before the first successful data fetch
            - If coordinator initialization fails
            All entity properties that access self.data or self.account.vehicles
            must handle None values gracefully to prevent AttributeError during
            entity registration.

        Raises:
            ConfigEntryAuthFailed: If a SmartAuthError is caught, indicating an authentication failure.
            UpdateFailed: If a SmartRemoteServiceError is raised during the data retrieval.
        """
        # Runs on every refresh, also while the cloud keeps failing
        self._expire_update_intervals()
        try:
            async with asyncio.timeout(API_TIMEOUT):
                await self.account.get_vehicles()
                # Reset failure counter on success
                if self._consecutive_failures > 0:
                    LOGGER.info(
                        "Smart API connection restored after %d failed attempts",
                        self._consecutive_failures,
                    )
                self._consecutive_failures = 0
                self._unbound_failures = 0
                self._last_error = None
                self._update_activity_intervals()
                return self.account.vehicles
        except SmartVehicleUnboundError as exception:
            # Only terminal once it repeats: a lone 8040 is usually the cloud
            # catching up after a session refresh. Below the threshold it goes
            # through the normal transient path so cached data keeps the
            # entities alive and the next poll can clear it.
            self._unbound_failures += 1
            if self._unbound_failures < UNBOUND_FAILURES_BEFORE_REAUTH:
                LOGGER.warning(
                    "Vehicle reported as unbound (8040) %d/%d, treating as transient: %s",
                    self._unbound_failures,
                    UNBOUND_FAILURES_BEFORE_REAUTH,
                    exception,
                )
                return self._handle_transient_failure(exception)
            LOGGER.error("%s (8040): %s", UNBOUND_VIN_AUTH_MESSAGE, exception)
            raise ConfigEntryAuthFailed(UNBOUND_VIN_AUTH_MESSAGE) from exception
        except SmartAuthError as exception:
            LOGGER.error(
                "Authentication failed for Smart API: %s. "
                "Please check your credentials.",
                exception,
            )
            raise ConfigEntryAuthFailed(exception) from exception
        except SmartRemoteServiceError as exception:
            LOGGER.error(
                "Smart remote service error: %s",
                exception,
            )
            raise UpdateFailed(f"Remote service error: {exception}") from exception
        except (
            SmartAPIError,
            httpx.HTTPStatusError,
            asyncio.TimeoutError,
            TimeoutError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            OSError,
            ConnectionError,
        ) as exception:
            return self._handle_transient_failure(exception)
        except Exception as exception:
            error_type = type(exception).__name__
            error_msg = str(exception) or "No error message"
            LOGGER.error(
                "Unexpected error fetching Smart API data: %s: %s\n%s",
                error_type,
                error_msg,
                traceback.format_exc(),
            )
            raise UpdateFailed(
                f"Unexpected error ({error_type}): {error_msg}"
            ) from exception

    def _handle_transient_failure(self, exception: Exception) -> Any:
        """Serve cached data for a transient failure, or fail once it persists.

        Keeps entities alive across a cloud blip and only raises UpdateFailed
        when there is nothing cached or the failures stop looking transient.
        """
        self._consecutive_failures += 1
        error_type = type(exception).__name__
        error_msg = f"{error_type}: {exception}" if str(exception) else error_type

        # Only log if error changed or first occurrence
        if self._last_error != error_msg:
            LOGGER.warning(
                "Smart API request failed (attempt %d/%d): %s",
                self._consecutive_failures,
                MAX_TRANSIENT_FAILURES,
                error_msg,
            )
            self._last_error = error_msg

        # Return last known data to keep entities available if we have it
        # and haven't exceeded max failures
        if (
            self.data is not None
            and self._consecutive_failures < MAX_TRANSIENT_FAILURES
        ):
            LOGGER.debug("Returning cached data to keep entities available")
            return self.data

        # If no cached data or too many failures, raise UpdateFailed
        if self.data is None:
            LOGGER.info(
                "Smart API unavailable and no cached data exists: %s",
                error_msg,
            )
            raise UpdateFailed(
                f"API unavailable with no cached data: {error_msg}"
            ) from exception

        LOGGER.error(
            "Smart API unavailable after %d consecutive failures: %s",
            self._consecutive_failures,
            error_msg,
        )
        raise UpdateFailed(
            f"API unavailable after {self._consecutive_failures} attempts: {error_msg}"
        ) from exception

    def _update_activity_intervals(self) -> None:
        """Poll faster while a vehicle is charging or driving.

        Runs once per successful refresh. Deciding this here instead of in the
        sensors' state getters keeps rendering free of side effects and stops
        every state write from requesting yet another refresh.
        """
        options = self.config_entry.options if self.config_entry else {}
        vehicles = list((self.account.vehicles or {}).values())

        charging = [_is_charging(vehicle) for vehicle in vehicles]
        if any(state is True for state in charging):
            self.set_update_interval(
                "charging",
                timedelta(
                    seconds=options.get(
                        CONF_CHARGING_INTERVAL, DEFAULT_CHARGING_INTERVAL
                    )
                ),
            )
        elif any(state is None for state in charging):
            # No usable charging data in this refresh: keep the current interval
            pass
        else:
            self.reset_update_interval("charging")

        if any(_is_driving(vehicle) for vehicle in vehicles):
            self.set_update_interval(
                "driving",
                timedelta(
                    seconds=options.get(CONF_DRIVING_INTERVAL, DEFAULT_DRIVING_INTERVAL)
                ),
            )
        else:
            self.reset_update_interval("driving")

    def set_update_interval(
        self,
        key: str,
        deltatime: timedelta,
        expires_after: timedelta | None = None,
    ) -> None:
        """Request an update interval under a key; the shortest one wins.

        With ``expires_after`` the request lapses on its own. Entities that
        poll fast after a command use it, because their own timeout only runs
        when the coordinator notifies them, which Home Assistant stops doing
        while refreshes keep failing.
        """
        if expires_after is None:
            self._interval_expiry.pop(key, None)
        else:
            self._interval_expiry[key] = dt_util.utcnow() + expires_after
        if self._update_intervals.get(key) == deltatime:
            return
        LOGGER.info("Update frequency set for %s: %s", key, deltatime)
        self._update_intervals[key] = deltatime
        self._apply_shortest_interval()

    def reset_update_interval(self, key: str):
        """Remove interval for this key and select shortest remaining or default"""
        self._interval_expiry.pop(key, None)
        if key in self._update_intervals:
            del self._update_intervals[key]
            LOGGER.info("Update frequency reset for %s", key)
        self._apply_shortest_interval()

    def _expire_update_intervals(self) -> None:
        """Drop interval requests whose time is up."""
        now = dt_util.utcnow()
        for key, expiry in list(self._interval_expiry.items()):
            if now >= expiry:
                LOGGER.debug("Update frequency request for %s expired", key)
                self.reset_update_interval(key)

    def _scan_interval(self) -> timedelta:
        if self.config_entry is None:
            LOGGER.warning("Using fallback update interval due to missing config_entry")
            return timedelta(seconds=DEFAULT_SCAN_INTERVAL)
        return timedelta(
            seconds=self.config_entry.options.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            )
        )

    def _apply_shortest_interval(self) -> None:
        # The configured scan interval is a ceiling: a requested interval may
        # only make polling faster, never slower.
        self.update_interval = min(
            [*self._update_intervals.values(), self._scan_interval()]
        )


_CHARGING_STATES = ("CHARGING", "DC_CHARGING")


def _is_charging(vehicle: Any) -> bool | None:
    """Return whether the vehicle is charging, None if it does not say.

    The charging current decides; without one the charging status does.
    None only when the refresh carries neither.
    """
    battery = getattr(vehicle, "battery", None)
    current = getattr(getattr(battery, "charging_current", None), "value", None)
    if current is not None:
        return bool(current)
    status = getattr(battery, "charging_status", None)
    if status is not None:
        return str(status).upper() in _CHARGING_STATES
    return None


def _is_driving(vehicle: Any) -> bool:
    """Return true if the vehicle reports a running engine."""
    return getattr(vehicle, "engine_state", None) == "engine_running"
