"""Skip today and Cooler please: the household's two quick answers to the day's plan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .rhythm_entity import RhythmEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    engine = hass.data[DOMAIN].get("rhythms")
    if engine is not None:
        async_add_entities([OrchestraSkipTodayButton(engine), OrchestraCoolerPleaseButton(engine)])


class OrchestraSkipTodayButton(RhythmEntity, ButtonEntity):
    """Pauses every rhythm and Conductor until midnight, and puts back what rhythms moved."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine, "skip_today", "button.orchestra_skip_today")
        self._attr_name = "Orchestra skip today"
        self._attr_icon = "mdi:calendar-remove"

    async def async_press(self) -> None:
        await self.engine.async_skip_today()


class OrchestraCoolerPleaseButton(RhythmEntity, ButtonEntity):
    """Ends Conductor easing the A/C for this event, and eases it less for a week."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine, "cooler_please", "button.orchestra_cooler_please")
        self._attr_name = "Orchestra cooler please"
        self._attr_icon = "mdi:snowflake-alert"

    async def async_press(self) -> None:
        await self.engine.async_cooler_please()
