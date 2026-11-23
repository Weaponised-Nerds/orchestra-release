"""Skip today and Cooler please: the household's two quick answers to the day's plan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .rhythm_entity import RhythmEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    engine = hass.data[DOMAIN].get("rhythms")
    if engine is not None:
        buttons = [OrchestraSkipTodayButton(engine), OrchestraCoolerPleaseButton(engine)]
        notifier = hass.data[DOMAIN].get("event_notifier")
        if notifier is not None:
            buttons.append(OrchestraSkipEventButton(engine, notifier))
        async_add_entities(buttons)


class OrchestraSkipTodayButton(RhythmEntity, ButtonEntity):
    """Pauses every rhythm and Conductor until midnight, and puts back what rhythms moved."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine, "skip_today", "button.orchestra_skip_today")
        self._attr_name = "Overture skip today"
        self._attr_icon = "mdi:calendar-remove"

    async def async_press(self) -> None:
        await self.engine.async_skip_today()


class OrchestraCoolerPleaseButton(RhythmEntity, ButtonEntity):
    """Ends Conductor easing the A/C for this event, and eases it less for a week."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine, "cooler_please", "button.orchestra_cooler_please")
        self._attr_name = "Overture cooler please"
        self._attr_icon = "mdi:snowflake-alert"

    async def async_press(self) -> None:
        await self.engine.async_cooler_please()


class OrchestraSkipEventButton(RhythmEntity, ButtonEntity):
    """Runs the current Conductor event on the normal schedule; the next event applies as usual."""

    def __init__(self, engine: Any, notifier: Any) -> None:
        super().__init__(engine, "skip_event", "button.orchestra_skip_event")
        self._notifier = notifier
        self._attr_name = "Overture skip this event"
        self._attr_icon = "mdi:debug-step-over"

    async def async_press(self) -> None:
        await self._notifier.async_skip_event()
