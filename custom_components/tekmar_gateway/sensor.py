"""Sensor platform for the Tekmar Gateway integration.

Three families of sensors:

  1. Per-thermostat: humidity (where the device reports it).
  2. Per-building: outdoor temperature.
  3. Per-boiler: a small set of diagnostics (outlet temp, runtime hours,
     status) for each boiler in the /system response.

We deliberately keep this lean for the first release - it's easy to add
more sensors later as people identify ones they actually use. The system
endpoint exposes a lot (loops, tanks, schematic info) that most users
won't care about, so those are not entities yet.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TekmarCoordinator


# ----------------------------------------------------------------------
# Outdoor temperature
# ----------------------------------------------------------------------


class TekmarOutdoorTempSensor(
    CoordinatorEntity[TekmarCoordinator], SensorEntity
):
    """The /system endpoint's `outdoor` field, in °F."""

    _attr_has_entity_name = True
    _attr_name = "Outdoor temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: TekmarCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.building_id}_outdoor_temp"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data["system"].get("outdoor")

    @property
    def device_info(self) -> DeviceInfo:
        # Attach to a synthetic "Gateway" device shared by building-level
        # sensors so they group together in the HA UI.
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.building_id)},
            name="Tekmar Gateway",
            manufacturer="Tekmar",
            model="Gateway 486",
        )


# ----------------------------------------------------------------------
# Per-thermostat humidity
# ----------------------------------------------------------------------


class TekmarHumiditySensor(
    CoordinatorEntity[TekmarCoordinator], SensorEntity
):
    """Humidity reading from a 557-class thermostat."""

    _attr_has_entity_name = True
    _attr_name = "Humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: TekmarCoordinator, addr: str) -> None:
        super().__init__(coordinator)
        self._addr = addr
        safe = addr.strip().replace(" ", "_") or "unknown"
        self._attr_unique_id = (
            f"{coordinator.building_id}_{safe}_humidity"
        )

    def _device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._addr, {})

    @property
    def native_value(self) -> int | None:
        hum = (self._device().get("sensors") or {}).get("hum")
        if hum in (None, 0):
            return None
        return int(hum)

    @property
    def available(self) -> bool:
        return super().available and not self._device().get("error", False)

    @property
    def device_info(self) -> DeviceInfo:
        d = self._device()
        safe = self._addr.strip().replace(" ", "_") or "unknown"
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self.coordinator.building_id}_{safe}")
            },
            name=(d.get("name") or self._addr).strip() or self._addr,
            manufacturer="Tekmar",
            model=d.get("product")
            or f"tekmarNet Thermostat {d.get('id', '')}",
            sw_version=d.get("version"),
            via_device=(DOMAIN, self.coordinator.building_id),
        )


# ----------------------------------------------------------------------
# Boiler sensors
# ----------------------------------------------------------------------


_BOILER_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="outlet",
        translation_key="boiler_outlet_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="inlet",
        translation_key="boiler_inlet_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="output",
        translation_key="boiler_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="runtime",
        translation_key="boiler_runtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="status",
        translation_key="boiler_status",
    ),
)


class TekmarBoilerSensor(
    CoordinatorEntity[TekmarCoordinator], SensorEntity
):
    """A diagnostic sensor from a boiler in /system."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TekmarCoordinator,
        boiler_index: int,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._boiler_index = boiler_index
        self._attr_unique_id = (
            f"{coordinator.building_id}_boiler{boiler_index}_{description.key}"
        )

    def _boiler(self) -> dict[str, Any] | None:
        boilers = self.coordinator.data["system"].get("boilers") or []
        if self._boiler_index < len(boilers):
            return boilers[self._boiler_index]
        return None

    @property
    def native_value(self) -> Any:
        b = self._boiler()
        if not b:
            return None
        val = b.get(self.entity_description.key)
        # Tekmar reports 0 for "no reading" on temp sensors that aren't wired.
        if (
            self.entity_description.device_class == SensorDeviceClass.TEMPERATURE
            and val == 0
        ):
            return None
        return val

    @property
    def device_info(self) -> DeviceInfo:
        b = self._boiler() or {}
        return DeviceInfo(
            identifiers={
                (DOMAIN,
                 f"{self.coordinator.building_id}_boiler{self._boiler_index}")
            },
            name=b.get("name", f"Boiler {self._boiler_index + 1}"),
            manufacturer="Tekmar",
            model="Boiler",
            via_device=(DOMAIN, self.coordinator.building_id),
        )


# ----------------------------------------------------------------------
# Platform setup
# ----------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tekmar sensor entities."""
    coordinator: TekmarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    # Building-level
    if coordinator.data["system"].get("outdoor") is not None:
        entities.append(TekmarOutdoorTempSensor(coordinator))

    # Per-thermostat humidity (only if the device reports a real value)
    for addr, device in coordinator.data["devices"].items():
        sensors = device.get("sensors") or {}
        if sensors.get("hum") not in (None, 0):
            entities.append(TekmarHumiditySensor(coordinator, addr))

    # Per-boiler diagnostics
    boilers = coordinator.data["system"].get("boilers") or []
    for idx in range(len(boilers)):
        for desc in _BOILER_SENSORS:
            entities.append(TekmarBoilerSensor(coordinator, idx, desc))

    async_add_entities(entities)
