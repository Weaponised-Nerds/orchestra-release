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
            state = hass.states.get(str(entity_id))
            name = str(state.attributes.get("friendly_name") or entity_id) if state else str(entity_id)
            if name not in out.setdefault(asset, []):
                out[asset].append(name)
    return out


class EventNotifier:
    def __init__(self, hass: Any, coordinator: Any, engine: Any) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.engine = engine
        self.last_key: str | None = None
        self.sent: list[dict[str, Any]] = []
        self._unsubs: list[Any] = []

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
        self.hass.async_create_task(self.async_check())

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
                notice.message + "\n\nSkip it with button.orchestra_skip_event on the Orchestra dashboard.",
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

