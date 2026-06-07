"""Data update coordinator for the Tekmar Gateway integration.

The coordinator fetches all thermostat state and system info on a single
poll, then all entities derive their state from the cached result. This is
the standard HA pattern for cloud-polling integrations - it avoids
hammering the API with N requests for N entities.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import TekmarApiClient, TekmarApiError, TekmarAuthError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TekmarCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the Tekmar gateway and exposes parsed state to entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TekmarApiClient,
        building_id: str,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.building_id = building_id

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch a fresh snapshot of devices + system status.

        Returns a dict shaped:
          {
              "system":  <result of /system>,
              "devices": {addr: <result of /devices/{addr}?filter=curr>},
          }
        We fetch all device-detail endpoints in parallel because the gateway
        handles them concurrently fine and serialized polls would multiply
        latency by N.
        """
        try:
            system, devices_list = await asyncio.gather(
                self.client.get_system(self.building_id),
                self.client.list_devices(self.building_id),
            )
            # Then fetch full detail for each device in parallel.
            details = await asyncio.gather(
                *(
                    self.client.get_device(self.building_id, d["addr"])
                    for d in devices_list
                )
            )
        except TekmarAuthError as err:
            # Tells HA to start a reauth flow rather than retry forever.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TekmarApiError as err:
            raise UpdateFailed(str(err)) from err

        devices_by_addr: dict[str, dict[str, Any]] = {}
        for summary, detail in zip(devices_list, details, strict=True):
            # Carry the summary fields (which include `error: true`) onto
            # the detail dict so entities can read both from one source.
            merged = {**summary, **detail}
            devices_by_addr[summary["addr"]] = merged

        return {"system": system, "devices": devices_by_addr}
