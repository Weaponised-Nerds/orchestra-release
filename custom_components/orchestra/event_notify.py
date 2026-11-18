"""
Tells the household when a Conductor event starts: what responds, why, and how to skip it.

The notifier watches the coordinator. When a Load Shed or Energy Soak event starts and
some of the home's own equipment (the Orchestra blueprint automations) responds, it sends
one notification to the companion apps with:
- a line per responding piece of equipment, by its own name;
- the benefit, from the live market and any known payment (`event_messages.py`);
- buttons: Skip this event, Skip today, and Cooler please when the A/C is easing off
  (otherwise Turn off Conductor).

One notification per event: the same event re-sent every half hour stays quiet. During
quiet hours the notice goes to the Home Assistant notification list instead of a phone.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .const import DOMAIN
from .event_messages import KIND_FOR_ASSET, compose_event_notice
from .rhythm_plan import DEFAULT_SETTINGS, in_quiet_hours, parse_hhmm

_LOGGER = logging.getLogger(__name__)

ACTION_SKIP_EVENT = "ORCHESTRA_SKIP_EVENT"
ACTION_SKIP_TODAY = "ORCHESTRA_SKIP_TODAY"
ACTION_COOLER_PLEASE = "ORCHESTRA_COOLER_PLEASE"
ACTION_OPT_OUT = "ORCHESTRA_OPT_OUT"
EVENT_TAG = "orchestra-event"
SERVICE_SKIP_EVENT = "skip_event"


def equipment_from_automations(automations: list[Any], hass: Any) -> dict[str, list[str]]:
    """The home's equipment that follows Conductor, by asset class, as friendly names."""
    from .asset_assignments import ASSET_KINDS, describe_assignments

    kind_to_asset = {kind: asset for asset, kind in KIND_FOR_ASSET.items()}
    switched_off = {
        state.attributes.get("id")
        for state in hass.states.async_all("automation")
        if state.state == "off"
    }
    out: dict[str, list[str]] = {}
    for assignment in describe_assignments(automations):
        asset = kind_to_asset.get(assignment["kind"])
        if asset is None or assignment.get("automation_id") in switched_off:
            continue
        main = ASSET_KINDS[assignment["kind"]].main_input
        value = assignment["inputs"].get(main)
        for entity_id in value if isinstance(value, list) else [value]:
            if not entity_id:
                continue
            name = friendly_name(hass, str(entity_id))
            if name not in out.setdefault(asset, []):
                out[asset].append(name)
    return out


def _readable_id(entity_id: str) -> str:
    """climate.toy_room_2 -> Toy room: the last resort, never the raw entity id."""
    obj = entity_id.split(".", 1)[-1]
    obj = re.sub(r"_\d+$", "", obj).replace("_", " ").strip()
    return obj[:1].upper() + obj[1:] if obj else entity_id


def friendly_name(hass: Any, entity_id: str) -> str:
    """The name the household knows: its state's name, else the entity registry's, else a tidy id.

    Right after Home Assistant starts, an integration may not have added its entities yet,
    so the state can be missing even though the device is known."""
    state = hass.states.get(entity_id)
    if state is not None and state.attributes.get("friendly_name"):
        return str(state.attributes["friendly_name"])
    try:
        from homeassistant.helpers import device_registry as dr, entity_registry as er

        entry = er.async_get(hass).async_get(entity_id)
        if entry is not None:
            name = entry.name or entry.original_name
            device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
            device_name = device and (device.name_by_user or device.name)
            if entry.has_entity_name and device_name:
                return f"{device_name} {name}" if name else str(device_name)
            if name or device_name:
                return str(name or device_name)
    except Exception:  # outside Home Assistant, or a registry we can't read
        pass
    return _readable_id(entity_id)


def _ha_started(hass: Any) -> bool:
    """True once Home Assistant has finished starting (every integration has loaded)."""
    try:
        from homeassistant.core import CoreState
    except ImportError:
        return True
    return getattr(hass, "state", CoreState.running) is CoreState.running


class EventNotifier:
    def __init__(self, hass: Any, coordinator: Any, engine: Any) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.engine = engine
        self.last_key: str | None = None
        self.sent: list[dict[str, Any]] = []
        self._unsubs: list[Any] = []
        self._waiting_for_start = False

    def async_start(self) -> None:
        self._unsubs.append(self.coordinator.async_add_listener(self._on_update))
        self._unsubs.append(self.hass.bus.async_listen("mobile_app_notification_action", self._async_action))

    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def _active(self) -> bool:
        disp = self.coordinator.data.dispatch
        return (
            disp.control_signal in ("Energy Soak", "Load Shed")
            and not disp.opted_out
            and not disp.event_skipped
        )

    def _on_update(self) -> None:
        if not self._active():
            self.last_key = None
            return
        if not self.engine.settings.get("event_notifications", True) or self.engine.skipped_today:
            return
        if not _ha_started(self.hass):
            # Conductor replays its latest frame the moment Orchestra connects, often before
            # the A/C, EV and phone (companion app) integrations have loaded. Wait for Home
            # Assistant to finish starting so the notice names the equipment and reaches phones.
            if not self._waiting_for_start:
                from homeassistant.helpers.start import async_at_started

                self._waiting_for_start = True
                self._unsubs.append(async_at_started(self.hass, self._async_started))
            return
        self.hass.async_create_task(self.async_check())

    async def _async_started(self, _hass: Any = None) -> None:
        self._waiting_for_start = False
        self._on_update()

    async def async_check(self) -> dict[str, Any] | None:
        """Send the notice for the current event if it hasn't been sent."""
        if not self._active():
            return None
        from .asset_assignments import automations_path
        from .setup_agent import _read_automations

        automations = await self.hass.async_add_executor_job(_read_automations, automations_path(self.hass))
        equipment = equipment_from_automations(automations, self.hass)
        disp = self.coordinator.data.dispatch
        settings = self.engine.settings
        incentive = dict(disp.incentive or {})
        if not incentive.get("aud_per_kwh") and settings.get("incentive_aud_per_kwh"):
            incentive = {
                "aud_per_kwh": settings.get("incentive_aud_per_kwh"),
                "program": settings.get("incentive_program", ""),
            }
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        notice = compose_event_notice(
            disp, self.coordinator.data.market, equipment, incentive, now.timetuple().tm_yday
        )
        if notice is None or notice.key == self.last_key:
            return None
        self.last_key = notice.key
        await self._async_send(notice, now)
        record = {"title": notice.title, "message": notice.message, "key": notice.key}
        self.sent.append(record)
        return record

    async def _async_send(self, notice: Any, now: Any) -> None:
        actions = [
            {"action": ACTION_SKIP_EVENT, "title": "Skip this event"},
            {"action": ACTION_SKIP_TODAY, "title": "Skip today"},
            {"action": ACTION_COOLER_PLEASE, "title": "Cooler please"}
            if notice.cooler_offered
            else {"action": ACTION_OPT_OUT, "title": "Turn off Conductor"},
        ]
        settings = self.engine.settings
        quiet = in_quiet_hours(
            now.time(),
            parse_hhmm(settings.get("quiet_start", ""), DEFAULT_SETTINGS["quiet_start"]),
            parse_hhmm(settings.get("quiet_end", ""), DEFAULT_SETTINGS["quiet_end"]),
        )
        targets = [] if quiet else self.engine._notify_targets()
        for service in targets:
            try:
                await self.hass.services.async_call(
                    "notify",
                    service,
                    {
                        "title": notice.title,
                        "message": notice.message,
                        "data": {"tag": EVENT_TAG, "actions": actions},
                    },
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.warning("Orchestra event notice not sent to notify.%s: %s", service, err)
        if not targets:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                notice.message + "\n\nTo skip it, press Skip this event on the Orchestra dashboard.",
                title=notice.title,
                notification_id=EVENT_TAG,
            )

    async def async_skip_event(self, _call: Any = None) -> None:
        self.coordinator.skip_event()
        self.engine._count("skips")
        self.engine._changed()

    async def _async_action(self, event: Any) -> None:
        action = event.data.get("action")
        if action == ACTION_SKIP_EVENT:
            await self.async_skip_event()
        elif action == ACTION_OPT_OUT:
            self.coordinator.set_opt_out(True)
        # Skip today and Cooler please are handled by the rhythm engine, which listens too


async def async_setup_event_notifier(hass: Any) -> EventNotifier | None:
    data = hass.data.get(DOMAIN, {})
    if "event_notifier" in data:
        return data["event_notifier"]
    coordinator, engine = data.get("coordinator"), data.get("rhythms")
    if coordinator is None or engine is None:
        return None
    notifier = EventNotifier(hass, coordinator, engine)
    notifier.async_start()
    data["event_notifier"] = notifier
    hass.services.async_register(DOMAIN, SERVICE_SKIP_EVENT, notifier.async_skip_event)
    return notifier

