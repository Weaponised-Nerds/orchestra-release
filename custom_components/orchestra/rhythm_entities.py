"""The rhythm engine's sensors and switches (added by sensor.py and switch.py)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity

from .rhythm_entity import RhythmEntity
from .rhythm_plan import RHYTHM_HEAT_SHIELD, RHYTHMS


class OrchestraTodaySensor(RhythmEntity, SensorEntity):
    """One line on today's plan for the dashboard tile; the events are in the attributes."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine, "today", "sensor.orchestra_today")
        self._attr_name = "Orchestra today"
        self._attr_icon = "mdi:weather-sunny-alert"

    @property
    def native_value(self) -> str:
        plan = self.engine.plan
        if self.engine.skipped_today:
            return "Skipped today"
        if plan is None:
            return "Planning"
        if plan.hot_day and any(ev.rhythm == RHYTHM_HEAT_SHIELD for ev in plan.events):
            return f"Hot day, {plan.forecast_max_c:g} °C: heat shield on"
        return "Normal day"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.engine.plan
        return {
            "forecast_max_c": plan.forecast_max_c if plan else None,
            "hot_day": plan.hot_day if plan else None,
            "sunrise": plan.sunrise.isoformat() if plan and plan.sunrise else None,
            "sunset": plan.sunset.isoformat() if plan and plan.sunset else None,
            "skipped_today": self.engine.skipped_today,
            "events": [
                {"summary": ev.summary, "start": ev.start.isoformat(), "end": ev.end.isoformat(), "rhythm": ev.rhythm}
                for ev in (plan.events if plan else [])
                if self.engine.enabled(ev.rhythm)
            ],
        }


class OrchestraRhythmCounter(RhythmEntity, SensorEntity):
    """A daily count, kept in the home only. Numbers, so the trial's monitor can read them."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, engine: Any, counter: str, entity_id: str, name: str, icon: str) -> None:
        super().__init__(engine, f"counter_{counter}", entity_id)
        self._counter = counter
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> int:
        counters = self.engine.counters
        if counters.get("date") != self.engine._today().isoformat():
            return 0
        return int(counters.get(self._counter, 0))


def rhythm_sensors(engine: Any) -> list[Any]:
    return [
        OrchestraTodaySensor(engine),
        OrchestraRhythmCounter(
            engine, "actions", "sensor.orchestra_rhythm_actions_today", "Orchestra rhythm actions today", "mdi:counter"
        ),
        OrchestraRhythmCounter(
            engine, "skips", "sensor.orchestra_rhythm_skips_today", "Orchestra skips today", "mdi:calendar-remove"
        ),
        OrchestraRhythmCounter(
            engine, "cooler", "sensor.orchestra_cooler_please_today", "Orchestra cooler please today",
            "mdi:snowflake-alert",
        ),
    ]


class OrchestraRhythmSwitch(RhythmEntity, SwitchEntity):
    """Turns one rhythm on or off until switched back."""

    def __init__(self, engine: Any, rhythm: str) -> None:
        super().__init__(engine, f"switch_{rhythm}", f"switch.orchestra_rhythm_{rhythm}")
        self._rhythm = rhythm
        self._attr_name = f"Orchestra rhythm: {RHYTHMS[rhythm].lower()}"
        self._attr_icon = "mdi:sun-clock"

    @property
    def is_on(self) -> bool:
        return self.engine.enabled(self._rhythm)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.engine.set_enabled(self._rhythm, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.engine.set_enabled(self._rhythm, False)


def rhythm_switches(engine: Any) -> list[Any]:
    return [OrchestraRhythmSwitch(engine, rhythm) for rhythm in RHYTHMS]
