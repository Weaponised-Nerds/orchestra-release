"""
Daily rhythms: the Home Assistant half (planning lives in `rhythm_plan.py`).

The rhythm engine
- plans each day's rhythm events at midnight and every hour (the forecast moves), and
  publishes them on `calendar.orchestra_today`;
- carries out each step when a rhythm blueprint calls `orchestra.rhythm_step`, remembering
  what it moved so the end of the step puts back only what is still where it left it;
- sends the morning heads-up, with Skip today and Cooler please buttons on the companion app;
- keeps Skip today, the "cooler please" taps and the daily counters in
  `.storage/orchestra.rhythms`.

Rhythms run whether or not the home follows Conductor. Skip today also pauses Conductor
for the rest of the day, through the same opt-out switch the household already has.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable

try:
    from .const import DOMAIN, SIGNAL_ENERGY_SOAK
    from .rhythm_plan import (
        DEFAULT_SETTINGS,
        RHYTHM_HEAT_SHIELD,
        RHYTHM_MORNING_OPEN_UP,
        RHYTHM_PEAK_QUIET,
        RHYTHMS,
        DayPlan,
        comfort_offset,
        headsup_text,
        in_quiet_hours,
        parse_hhmm,
        plan_day,
        plan_hold,
        plan_release,
    )
except ImportError:
    from custom_components.orchestra.const import DOMAIN, SIGNAL_ENERGY_SOAK
    from custom_components.orchestra.rhythm_plan import (
        DEFAULT_SETTINGS,
        RHYTHM_HEAT_SHIELD,
        RHYTHM_MORNING_OPEN_UP,
        RHYTHM_PEAK_QUIET,
        RHYTHMS,
        DayPlan,
        comfort_offset,
        headsup_text,
        in_quiet_hours,
        parse_hhmm,
        plan_day,
        plan_hold,
        plan_release,
    )

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.rhythms"
STORAGE_VERSION = 1
SERVICE_RHYTHM_STEP = "rhythm_step"
SERVICE_SKIP_TODAY = "skip_today"
SERVICE_COOLER_PLEASE = "cooler_please"
SERVICE_SET_RHYTHM_SETTINGS = "set_rhythm_settings"
SERVICE_SEND_HEADSUP = "send_rhythm_headsup"

ACTION_SKIP_TODAY = "ORCHESTRA_SKIP_TODAY"
ACTION_COOLER_PLEASE = "ORCHESTRA_COOLER_PLEASE"
HEADSUP_TAG = "orchestra-headsup"


def _empty_counters(day: date) -> dict[str, Any]:
    return {"date": day.isoformat(), "actions": 0, "skips": 0, "cooler": 0}


class RhythmEngine:
    """Plans, runs and remembers the home's daily rhythms."""

    def __init__(self, hass: Any) -> None:
        from homeassistant.helpers.storage import Store

        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.settings: dict[str, Any] = {**DEFAULT_SETTINGS, "enabled": dict(DEFAULT_SETTINGS["enabled"])}
        self.holds: dict[str, dict[str, dict[str, Any]]] = {}
        self.skip_date: str | None = None
        self.skip_set_opt_out = False
        self.cooler_dispatch_id: str | None = None
        self.taps: list[str] = []
        self.counters: dict[str, Any] = _empty_counters(self._today())
        self.plan: DayPlan | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._headsup_unsub: Callable[[], None] | None = None

    # ---------------------------------------------------------------- state
    def _now(self) -> datetime:
        from homeassistant.util import dt as dt_util

        return dt_util.now()

    def _today(self) -> date:
        return self._now().date()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        saved = data.get("settings") or {}
        self.settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS and k != "enabled"})
        self.settings["enabled"].update(
            {k: bool(v) for k, v in (saved.get("enabled") or {}).items() if k in RHYTHMS}
        )
        self.holds = data.get("holds") or {}
        self.skip_date = data.get("skip_date")
        self.skip_set_opt_out = bool(data.get("skip_set_opt_out"))
        self.cooler_dispatch_id = data.get("cooler_dispatch_id")
        self.taps = list(data.get("taps") or [])
        counters = data.get("counters") or {}
        if counters.get("date") == self._today().isoformat():
            self.counters = {**_empty_counters(self._today()), **counters}

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "settings": self.settings,
                "holds": self.holds,
                "skip_date": self.skip_date,
                "skip_set_opt_out": self.skip_set_opt_out,
                "cooler_dispatch_id": self.cooler_dispatch_id,
                "taps": self.taps,
                "counters": self.counters,
            },
            1,
        )

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _changed(self) -> None:
        self._save()
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as err:  # one broken entity mustn't stop the rest
                _LOGGER.error("Overture rhythm listener failed: %s", err)

    def _count(self, key: str, amount: int = 1) -> None:
        if self.counters.get("date") != self._today().isoformat():
            self.counters = _empty_counters(self._today())
        self.counters[key] = int(self.counters.get(key, 0)) + amount

    @property
    def skipped_today(self) -> bool:
        return self.skip_date == self._today().isoformat()

    def enabled(self, rhythm: str) -> bool:
        return bool(self.settings["enabled"].get(rhythm, True))

    @property
    def coordinator(self) -> Any:
        return self.hass.data.get(DOMAIN, {}).get("coordinator")

    # ---------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        from homeassistant.helpers.event import async_track_time_change

        self._unsubs.append(async_track_time_change(self.hass, self._async_midnight, hour=0, minute=1, second=0))
        self._unsubs.append(async_track_time_change(self.hass, self._async_hourly, minute=5, second=0))
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._async_notification_action)
        )
        self._schedule_headsup()
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.comfort_hook = self.comfort
            self._unsubs.append(coordinator.async_add_listener(self._on_dispatch))
        await self.async_replan()
        if not getattr(self.hass, "is_running", True):
            # The weather integration may not be up yet: plan again once everything has started
            from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._async_hourly)

    def async_stop(self) -> None:
        coordinator = self.coordinator
        if coordinator is not None and getattr(coordinator, "comfort_hook", None) == self.comfort:
            coordinator.comfort_hook = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._headsup_unsub:
            self._headsup_unsub()
            self._headsup_unsub = None

    def _schedule_headsup(self) -> None:
        from homeassistant.helpers.event import async_track_time_change

        if self._headsup_unsub:
            self._headsup_unsub()
        at = parse_hhmm(self.settings.get("headsup_time", ""), DEFAULT_SETTINGS["headsup_time"])
        quiet_start = parse_hhmm(self.settings.get("quiet_start", ""), DEFAULT_SETTINGS["quiet_start"])
        quiet_end = parse_hhmm(self.settings.get("quiet_end", ""), DEFAULT_SETTINGS["quiet_end"])
        if in_quiet_hours(at, quiet_start, quiet_end):
            at = quiet_end
        self._headsup_unsub = async_track_time_change(
            self.hass, self._async_headsup_time, hour=at.hour, minute=at.minute, second=0
        )

    async def _async_midnight(self, _now: Any = None) -> None:
        if self.skip_date and not self.skipped_today:
            self.skip_date = None
            coordinator = self.coordinator
            if self.skip_set_opt_out and coordinator is not None and coordinator.data.dispatch.opted_out:
                coordinator.set_opt_out(False)
            self.skip_set_opt_out = False
        self.counters = _empty_counters(self._today())
        await self.async_replan()

    async def _async_hourly(self, _now: Any = None) -> None:
        await self.async_replan()

    async def _async_headsup_time(self, _now: Any = None) -> None:
        await self.async_replan()
        await self.async_send_headsup()

    # ---------------------------------------------------------------- planning
    async def _async_forecast_max(self) -> float | None:
        entity_id = self.settings.get("weather_entity") or next(
            iter(sorted(self.hass.states.async_entity_ids("weather"))), None
        )
        if not entity_id:
            return None
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "daily"},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.debug("No daily forecast from %s: %s", entity_id, err)
            return None
        forecast = ((response or {}).get(entity_id) or {}).get("forecast") or []
        today = self._today()
        from homeassistant.util import dt as dt_util

        for entry in forecast:
            when = dt_util.parse_datetime(str(entry.get("datetime", "")))
            if when is not None and dt_util.as_local(when).date() == today:
                return _as_float(entry.get("temperature"))
        return _as_float(forecast[0].get("temperature")) if forecast else None

    async def async_replan(self) -> None:
        from homeassistant.util import dt as dt_util

        forecast_max = await self._async_forecast_max()
        self.plan = plan_day(
            self._today(),
            tz=dt_util.get_default_time_zone(),
            lat=float(self.hass.config.latitude),
            lon=float(self.hass.config.longitude),
            forecast_max_c=forecast_max,
            settings=self.settings,
        )
        self._changed()

    # ---------------------------------------------------------------- steps
    async def async_rhythm_step(self, call: Any) -> dict[str, Any]:
        rhythm = call.data["rhythm"]
        phase = call.data.get("phase", "start")
        group = call.data.get("group", "")
        entity_ids: list[str] = list(call.data.get("entity_id") or [])
        key = f"{rhythm}:{group}"

        if phase == "end":
            moved = await self._async_release(key, call)
            self._changed()
            return {"released": moved}

        if not self.enabled(rhythm):
            return {"skipped": f"{RHYTHMS.get(rhythm, rhythm)} is switched off"}
        if self.skipped_today:
            return {"skipped": "skip today is on"}

        if rhythm == RHYTHM_HEAT_SHIELD:
            moved = await self._async_hold(key, entity_ids, "closed", call)
        elif rhythm == RHYTHM_MORNING_OPEN_UP:
            moved = await self._async_open_up(entity_ids, call)
        elif rhythm == RHYTHM_PEAK_QUIET:
            coordinator = self.coordinator
            if coordinator is not None and _soaking(coordinator):
                return {"skipped": "Conductor is soaking up spare power right now"}
            moved = await self._async_hold(key, entity_ids, "off", call)
        else:
            return {"skipped": f"unknown rhythm {rhythm}"}
        self._count("actions", len(moved))
        self._changed()
        return {"moved": moved}

    async def _async_hold(self, key: str, entity_ids: list[str], want: str, call: Any) -> list[str]:
        held = self.holds.setdefault(key, {})
        moved: list[str] = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if plan_hold(state.state if state else None, want=want) != "act":
                continue
            before: dict[str, Any] = {"state": state.state}
            if want == "closed":
                position = state.attributes.get("current_position")
                if position is not None:
                    before["position"] = position
                await self._async_call("cover", "close_cover", entity_id, call)
            else:
                await self._async_call(entity_id.split(".", 1)[0], "turn_off", entity_id, call)
            held[entity_id] = {"set_to": want, "before": before}
            moved.append(entity_id)
        if not held:
            self.holds.pop(key, None)
        return moved

    async def _async_release(self, key: str, call: Any) -> list[str]:
        held = self.holds.pop(key, {})
        moved: list[str] = []
        for entity_id, record in held.items():
            if self._held_elsewhere(entity_id):
                continue
            state = self.hass.states.get(entity_id)
            if plan_release(state.state if state else None, set_to=record["set_to"]) != "restore":
                continue
            before = record.get("before") or {}
            if record["set_to"] == "closed":
                position = before.get("position")
                if position is not None and 0 < int(position) < 100:
                    await self._async_call("cover", "set_cover_position", entity_id, call, {"position": int(position)})
                else:
                    await self._async_call("cover", "open_cover", entity_id, call)
            else:
                await self._async_call(entity_id.split(".", 1)[0], "turn_on", entity_id, call)
            moved.append(entity_id)
        return moved

    def _held_elsewhere(self, entity_id: str) -> bool:
        return any(entity_id in held for held in self.holds.values())

    async def _async_open_up(self, entity_ids: list[str], call: Any) -> list[str]:
        moved: list[str] = []
        for entity_id in entity_ids:
            if self._held_elsewhere(entity_id):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state not in ("closed", "closing"):
                continue
            await self._async_call("cover", "open_cover", entity_id, call)
            moved.append(entity_id)
        return moved

    async def _async_call(
        self, domain: str, service: str, entity_id: str, call: Any, extra: dict[str, Any] | None = None
    ) -> None:
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {"entity_id": entity_id, **(extra or {})},
                blocking=True,
                context=getattr(call, "context", None),
            )
        except Exception as err:  # one stuck blind mustn't stop the others
            _LOGGER.warning("Overture rhythm could not %s %s: %s", service, entity_id, err)

    # ---------------------------------------------------------------- household controls
    async def async_skip_today(self, _call: Any = None) -> None:
        if self.skipped_today:
            return
        self.skip_date = self._today().isoformat()
        coordinator = self.coordinator
        if coordinator is not None and not coordinator.data.dispatch.opted_out:
            coordinator.set_opt_out(True)
            self.skip_set_opt_out = True
        for key in list(self.holds):
            await self._async_release(key, _call)
        self._count("skips")
        self._changed()

    async def async_cooler_please(self, _call: Any = None) -> None:
        now = self._now()
        self.taps = [t for t in self.taps if _recent(t, now)] + [now.isoformat()]
        coordinator = self.coordinator
        if coordinator is not None:
            self.cooler_dispatch_id = coordinator.data.dispatch.dispatch_id
        self._count("cooler")
        self._changed()
        if coordinator is not None:
            coordinator.async_update_listeners()

    def comfort(self, offset: float, dispatch: Any) -> float:
        """The coordinator's A/C offset after the household's comfort taps."""
        from homeassistant.util import dt as dt_util

        coordinator = self.coordinator
        max_offset = float(getattr(getattr(coordinator, "safety_guard", None), "hvac_max_setpoint_offset_c", 2.0))
        taps = [t for t in (dt_util.parse_datetime(x) for x in self.taps) if t is not None]
        return comfort_offset(
            offset,
            max_offset=max_offset,
            cooler_now=self.cooler_dispatch_id is not None and self.cooler_dispatch_id == dispatch.dispatch_id,
            taps=taps,
            now=self._now(),
        )

    def _on_dispatch(self) -> None:
        dispatch = self.coordinator.data.dispatch
        if self.cooler_dispatch_id and (
            dispatch.dispatch_id != self.cooler_dispatch_id or dispatch.ac_setpoint_offset_c <= 0
        ):
            self.cooler_dispatch_id = None
            self._save()

    def set_enabled(self, rhythm: str, on: bool) -> None:
        self.settings["enabled"][rhythm] = on
        self.hass.async_create_task(self.async_replan())

    async def async_set_settings(self, call: Any) -> None:
        for key, value in call.data.items():
            if key == "enabled" and isinstance(value, dict):
                self.settings["enabled"].update({k: bool(v) for k, v in value.items() if k in RHYTHMS})
            elif key in DEFAULT_SETTINGS:
                self.settings[key] = value
        self._schedule_headsup()
        await self.async_replan()

    # ---------------------------------------------------------------- heads-up
    async def async_send_headsup(self, call: Any = None) -> str | None:
        force = bool(getattr(call, "data", {}).get("force")) if call is not None else False
        if self.plan is None or (self.skipped_today and not force):
            return None
        text = headsup_text(self.plan)
        if text is None and force:
            text = "A normal day: no heat shield needed."
        if text is None:
            return None
        targets = self._notify_targets()
        data = {
            "tag": HEADSUP_TAG,
            "actions": [
                {"action": ACTION_SKIP_TODAY, "title": "Skip today"},
                {"action": ACTION_COOLER_PLEASE, "title": "Cooler please"},
            ],
        }
        for service in targets:
            try:
                await self.hass.services.async_call(
                    "notify", service, {"title": "Overture today", "message": text, "data": data}, blocking=True
                )
            except Exception as err:
                _LOGGER.warning("Overture heads-up not sent to notify.%s: %s", service, err)
        if not targets:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                text + " Skip today and Cooler please are on the Overture dashboard.",
                title="Overture today",
                notification_id=HEADSUP_TAG,
            )
        return text

    def _notify_targets(self) -> list[str]:
        chosen = [str(t).removeprefix("notify.") for t in self.settings.get("notify_targets") or []]
        if chosen:
            return chosen
        notify = self.hass.services.async_services().get("notify", {})
        return sorted(name for name in notify if name.startswith("mobile_app_"))

    async def _async_notification_action(self, event: Any) -> None:
        action = event.data.get("action")
        if action == ACTION_SKIP_TODAY:
            await self.async_skip_today()
        elif action == ACTION_COOLER_PLEASE:
            await self.async_cooler_please()


def _soaking(coordinator: Any) -> bool:
    dispatch = coordinator.data.dispatch
    return not dispatch.opted_out and dispatch.control_signal == SIGNAL_ENERGY_SOAK


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recent(iso: str, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(iso) <= timedelta(days=7)
    except (TypeError, ValueError):
        return False


async def async_setup_rhythms(hass: Any) -> RhythmEngine | None:
    """Start the rhythm engine and register its actions, once per HA instance."""
    existing = hass.data.get(DOMAIN, {}).get("rhythms")
    if existing is not None:
        return existing
    services = getattr(hass, "services", None)
    if services is None or not hasattr(services, "async_register"):
        return None
    try:
        from .schema import vol
        from homeassistant.core import SupportsResponse
        from homeassistant.helpers import config_validation as cv
    except ImportError:
        return None

    engine = RhythmEngine(hass)
    await engine.async_load()
    hass.data.setdefault(DOMAIN, {})["rhythms"] = engine

    services.async_register(
        DOMAIN,
        SERVICE_RHYTHM_STEP,
        engine.async_rhythm_step,
        schema=vol.Schema(
            {
                vol.Required("rhythm"): vol.In(list(RHYTHMS)),
                vol.Optional("phase", default="start"): vol.In(["start", "end"]),
                vol.Optional("group", default=""): cv.string,
                vol.Optional("entity_id", default=[]): cv.entity_ids,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    services.async_register(DOMAIN, SERVICE_SKIP_TODAY, engine.async_skip_today)
    services.async_register(DOMAIN, SERVICE_COOLER_PLEASE, engine.async_cooler_please)
    services.async_register(
        DOMAIN,
        SERVICE_SEND_HEADSUP,
        engine.async_send_headsup,
        schema=vol.Schema({vol.Optional("force", default=False): cv.boolean}),
    )
    services.async_register(
        DOMAIN,
        SERVICE_SET_RHYTHM_SETTINGS,
        engine.async_set_settings,
        schema=vol.Schema(
            {
                vol.Optional("headsup_time"): cv.string,
                vol.Optional("quiet_start"): cv.string,
                vol.Optional("quiet_end"): cv.string,
                vol.Optional("wake_time"): cv.string,
                vol.Optional("open_up_earliest"): cv.string,
                vol.Optional("peak_start"): cv.string,
                vol.Optional("peak_end"): cv.string,
                vol.Optional("hot_day_c"): vol.Coerce(float),
                vol.Optional("notify_targets"): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional("weather_entity"): cv.string,
                vol.Optional("enabled"): dict,
                vol.Optional("event_notifications"): cv.boolean,
                vol.Optional("incentive_program"): cv.string,
                vol.Optional("incentive_aud_per_kwh"): vol.Coerce(float),
            }
        ),
    )
    await engine.async_start()
    return engine
