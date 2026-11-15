"""`calendar.orchestra_today`: today's rhythm events, which the rhythm blueprints trigger on."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent

from .const import DOMAIN
from .rhythm_entity import RhythmEntity


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    engine = hass.data[DOMAIN].get("rhythms")
    if engine is not None:
        async_add_entities([OrchestraTodayCalendar(engine)])


class OrchestraTodayCalendar(RhythmEntity, CalendarEntity):
    """Today's plan: heat shield spells, morning open-up and peak quiet."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine, "today_calendar", "calendar.orchestra_today")
        self._attr_name = "Orchestra today"
        self._attr_icon = "mdi:calendar-clock"

    def _events(self) -> list[CalendarEvent]:
        plan = self.engine.plan
        if plan is None:
            return []
        return [
            CalendarEvent(
                start=ev.start,
                end=ev.end,
                summary=ev.summary + (" (skipped today)" if self.engine.skipped_today else ""),
                description=ev.description,
                uid=ev.uid,
            )
            for ev in plan.events
            if self.engine.enabled(ev.rhythm)
        ]

    @property
    def event(self) -> CalendarEvent | None:
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        upcoming = [ev for ev in self._events() if ev.end > now]
        return min(upcoming, key=lambda ev: ev.start) if upcoming else None

    async def async_get_events(self, hass: Any, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        return [ev for ev in self._events() if ev.end > start_date and ev.start < end_date]
