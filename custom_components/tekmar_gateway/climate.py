"""Climate platform for the Tekmar Gateway integration.

Exposes one ClimateEntity per thermostat on the account. Mapping notes:

  Tekmar mode (numeric `val` in settings.mode)
      0 = Off          -> HVACMode.OFF
      1 = Heat         -> HVACMode.HEAT
      2 = Auto         -> HVACMode.HEAT_COOL
      3 = Cool         -> HVACMode.COOL
      6 = Emergency    -> HVACMode.HEAT with preset "emergency_heat"

  Tekmar demand string -> HVACAction:
      "Off"            -> IDLE  (or OFF if mode is Off)
      "Heating"        -> HEATING
      "Cooling"        -> COOLING

The 552 is heat-only; its mode enum only contains Off and Heat.
The 557 supports the full set.
We discover capability per-device from the settings.mode.enum array rather
than hardcoding model numbers.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COOL_MAX_DEFAULT,
    COOL_MIN_DEFAULT,
    DOMAIN,
    HEAT_MAX_DEFAULT,
    HEAT_MIN_DEFAULT,
    MODE_AUTO,
    MODE_COOL,
    MODE_EMERGENCY,
    MODE_HEAT,
    MODE_OFF,
)
from .coordinator import TekmarCoordinator

# Tekmar numeric mode -> HA HVACMode
_TEKMAR_NUM_TO_HVAC: dict[int, HVACMode] = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.HEAT_COOL,
    3: HVACMode.COOL,
    6: HVACMode.HEAT,  # emergency reports as HEAT with preset
}

# HA HVACMode -> Tekmar string for PUT /settings/mode
_HVAC_TO_TEKMAR_STR: dict[HVACMode, str] = {
    HVACMode.OFF: MODE_OFF,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.COOL: MODE_COOL,
    HVACMode.HEAT_COOL: MODE_AUTO,
}

PRESET_EMERGENCY = "emergency_heat"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tekmar climate entities for a config entry."""
    coordinator: TekmarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TekmarThermostat(coordinator, addr)
        for addr, device in coordinator.data["devices"].items()
        if device.get("type") == "Thermostat"
    ]
    async_add_entities(entities)


class TekmarThermostat(CoordinatorEntity[TekmarCoordinator], ClimateEntity):
    """A single thermostat exposed as an HA climate entity."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_has_entity_name = True
    _attr_name = None  # use the device name as the entity name
    # Newer HA versions warn if this isn't set explicitly.
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coordinator: TekmarCoordinator, addr: str) -> None:
        super().__init__(coordinator)
        self._addr = addr
        # The device's addr can contain spaces (e.g. " 07") so the unique_id
        # gets normalized to underscores.
        safe_addr = addr.strip().replace(" ", "_") or "unknown"
        self._attr_unique_id = (
            f"{coordinator.building_id}_{safe_addr}"
        )
        self._set_capabilities()

    # ------------------------------------------------------------------
    # Capability discovery
    # ------------------------------------------------------------------

    def _device(self) -> dict[str, Any]:
        return self.coordinator.data["devices"].get(self._addr, {})

    def _set_capabilities(self) -> None:
        """Discover supported modes/features from the API's enum data.

        We read settings.mode.enum to determine which modes this device
        supports, rather than hardcoding 552 vs 557. If the device is
        currently in Off mode, the heat/cool branches of `settings` are
        absent — we fall back to a sensible defaults for min/max in that
        case. Real bounds are picked up as soon as the device is taken out
        of Off mode and the next poll completes.
        """
        device = self._device()
        settings = device.get("settings", {})

        # Modes
        hvac_modes: set[HVACMode] = {HVACMode.OFF}
        mode_enum = settings.get("mode", {}).get("enum", [])
        emergency_supported = False
        for entry in mode_enum:
            val = entry.get("val")
            if val == 1:
                hvac_modes.add(HVACMode.HEAT)
            elif val == 2:
                hvac_modes.add(HVACMode.HEAT_COOL)
            elif val == 3:
                hvac_modes.add(HVACMode.COOL)
            elif val == 6:
                emergency_supported = True
        self._attr_hvac_modes = sorted(hvac_modes, key=lambda m: m.value)

        # Setpoint bounds -- only from branches the device actually
        # exposes, so a heat-only thermostat's slider matches its real
        # 50-85 style range instead of being widened by cool defaults.
        heat = settings.get("heat", {})
        cool = settings.get("cool", {})
        mins: list[float] = []
        maxes: list[float] = []
        if heat:
            mins.append(heat.get("min", HEAT_MIN_DEFAULT))
            maxes.append(heat.get("max", HEAT_MAX_DEFAULT))
        if cool:
            mins.append(cool.get("min", COOL_MIN_DEFAULT))
            maxes.append(cool.get("max", COOL_MAX_DEFAULT))
        self._attr_min_temp = min(mins) if mins else HEAT_MIN_DEFAULT
        self._attr_max_temp = max(maxes) if maxes else HEAT_MAX_DEFAULT

        # Features
        features = (
            ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        )
        if HVACMode.HEAT_COOL in hvac_modes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if any(m in hvac_modes for m in (HVACMode.HEAT, HVACMode.COOL)):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if emergency_supported:
            features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = ["none", PRESET_EMERGENCY]
        self._attr_supported_features = features

    @callback
    def _handle_coordinator_update(self) -> None:
        # Re-derive capabilities on every poll in case the device's mode
        # changed and the settings shape now includes heat/cool blocks.
        self._set_capabilities()
        super()._handle_coordinator_update()

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        d = self._device()
        return DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id or self._addr)},
            name=(d.get("name") or self._addr).strip() or self._addr,
            manufacturer="Tekmar",
            model=d.get("product") or f"tekmarNet Thermostat {d.get('id', '')}",
            sw_version=d.get("version"),
            via_device=(DOMAIN, self.coordinator.building_id),
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        # The summary endpoint reports `error: true` for offline thermostats.
        return not self._device().get("error", False)

    # ------------------------------------------------------------------
    # State reads
    # ------------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        temp = self._device().get("temp")
        # 0 is a common "no reading" sentinel from tekmar - filter it out
        # when the device is also in error state.
        if temp in (None, 0):
            return None
        return temp

    @property
    def current_humidity(self) -> int | None:
        sensors = self._device().get("sensors", {}) or {}
        hum = sensors.get("hum")
        if hum in (None, 0):
            return None
        return int(hum)

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode_val = (
            self._device().get("settings", {})
            .get("mode", {})
            .get("val")
        )
        if mode_val is None:
            return None
        return _TEKMAR_NUM_TO_HVAC.get(int(mode_val))

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        demand = self._device().get("demand")
        if demand == "Heating":
            return HVACAction.HEATING
        if demand == "Cooling":
            return HVACAction.COOLING
        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        mode_val = (
            self._device().get("settings", {})
            .get("mode", {})
            .get("val")
        )
        if mode_val == 6:
            return PRESET_EMERGENCY
        return "none"

    @property
    def target_temperature(self) -> float | None:
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return None  # use target_temperature_high/low instead
        settings = self._device().get("settings", {})
        if self.hvac_mode == HVACMode.COOL:
            return settings.get("cool", {}).get("val")
        if self.hvac_mode in (HVACMode.HEAT, None):
            return settings.get("heat", {}).get("val")
        return None

    @property
    def target_temperature_low(self) -> float | None:
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return (
                self._device().get("settings", {}).get("heat", {}).get("val")
            )
        return None

    @property
    def target_temperature_high(self) -> float | None:
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return (
                self._device().get("settings", {}).get("cool", {}).get("val")
            )
        return None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self._attr_hvac_modes:
            raise ValueError(
                f"mode {hvac_mode} not supported by this thermostat"
            )
        tekmar_mode = _HVAC_TO_TEKMAR_STR[hvac_mode]
        await self.coordinator.client.set_mode(
            self.coordinator.building_id, self._addr, tekmar_mode
        )
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_EMERGENCY:
            await self.coordinator.client.set_mode(
                self.coordinator.building_id, self._addr, MODE_EMERGENCY
            )
        else:
            # Falling back to plain Heat when leaving emergency.
            await self.coordinator.client.set_mode(
                self.coordinator.building_id, self._addr, MODE_HEAT
            )
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the setpoint(s).

        HA may pass `temperature` (single) or both `target_temp_low`/
        `target_temp_high` (range) depending on hvac_mode.
        """
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        single = kwargs.get(ATTR_TEMPERATURE)

        building = self.coordinator.building_id
        addr = self._addr

        if low is not None:
            await self.coordinator.client.set_heat(building, addr, int(low))
        if high is not None:
            await self.coordinator.client.set_cool(building, addr, int(high))
        if single is not None:
            if self.hvac_mode == HVACMode.COOL:
                await self.coordinator.client.set_cool(
                    building, addr, int(single)
                )
            else:
                await self.coordinator.client.set_heat(
                    building, addr, int(single)
                )

        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        """Default 'on' = Heat for heat-only, Auto for full thermostats."""
        if HVACMode.HEAT_COOL in self._attr_hvac_modes:
            await self.async_set_hvac_mode(HVACMode.HEAT_COOL)
        else:
            await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
