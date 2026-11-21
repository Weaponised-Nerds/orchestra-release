"""
The Orchestra dashboard the setup agent builds once the blueprints are filled.

Kept free of Home Assistant imports, like `setup_planner.py`, so the rules can be tested on
their own. `dashboard_builder.py` feeds it from the live registries and saves the result.

The dashboard only uses cards that ship with Home Assistant, so it works on a fresh box
with no custom cards installed. Every card is built from what this home actually has:
- Home: what Conductor is asking for and the household's choices (opt-outs, rhythm
  switches, Skip today), today's rhythms, the equipment Orchestra drives, the Orchestra
  automations, what Orchestra did lately, and the market signal history.
- Energy: Home Assistant's own energy flow, then solar, home battery, home use and car,
  each with its own graph.

`plan_energy_prefs` fills the Energy dashboard settings (which the energy flow cards read)
when nobody has set them yet, from the home's grid, solar and battery sensors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from .blueprint_installer import (
        BLUEPRINT_EV_CHARGE_RATE,
        BLUEPRINT_HEAT_PUMP_TARGET,
        BLUEPRINT_HOME_BATTERY_RESERVE,
        BLUEPRINT_HVAC_SETPOINT_OFFSET,
        BLUEPRINT_NAMESPACE,
    )
    from .setup_planner import EntityInfo
except ImportError:
    from custom_components.orchestra.blueprint_installer import (
        BLUEPRINT_EV_CHARGE_RATE,
        BLUEPRINT_HEAT_PUMP_TARGET,
        BLUEPRINT_HOME_BATTERY_RESERVE,
        BLUEPRINT_HVAC_SETPOINT_OFFSET,
        BLUEPRINT_NAMESPACE,
    )
    from custom_components.orchestra.setup_planner import EntityInfo

DASHBOARD_URL_PATH = "orchestra-home"
DASHBOARD_TITLE = "Orchestra"
DASHBOARD_ICON = "mdi:music-clef-treble"

# Orchestra's own entities, by unique id. Entity ids differ per box (the device name is in
# them), so the dashboard always looks them up through the entity registry.
UID_CONTROL_SIGNAL = "orchestra_conductor_control_signal"
UID_DISPATCH_MODE = "orchestra_conductor_dispatch_mode"
UID_OPT_OUT = "orchestra_conductor_opt_out"
UID_PRICE = "orchestra_wem_wholesale_price"
UID_DEMAND = "orchestra_wem_operational_demand"
UID_RENEWABLES = "orchestra_wem_renewable_availability"
UID_SHED = "orchestra_counterfactual_shed_power"
UID_DIVIDEND = "orchestra_accumulated_dividend_aud"
UID_CALENDAR = "orchestra_rhythm_today_calendar"
UID_SKIP_TODAY = "orchestra_rhythm_skip_today"
UID_COOLER = "orchestra_rhythm_cooler_please"
UID_SKIP_EVENT = "orchestra_rhythm_skip_event"
RHYTHM_SWITCH_PREFIX = "orchestra_rhythm_switch_"
RHYTHM_COUNTER_PREFIX = "orchestra_rhythm_counter_"
UID_TODAY = "orchestra_rhythm_today"
# Older aliases (sensor.conductor_dr_*) repeat other entities, so they stay off the dashboard
ALIAS_PREFIX = "conductor_dr_"

# What each kind of equipment adds to "What Conductor is asking for"
ASSET_UIDS: dict[str, tuple[str, ...]] = {
    "hvac": ("orchestra_ac_dispatch_mode", "orchestra_ac_precool", "orchestra_ac_preheat", "orchestra_hvac_setpoint_offset"),
    "ev": ("orchestra_ev_charge_now", "orchestra_ev_target_amps"),
    "heat_pump": ("orchestra_heat_pump_run_now", "orchestra_heat_pump_target_temp"),
    "battery": ("orchestra_home_battery_storm_watch", "orchestra_home_battery_min_reserve"),
}

POWER_UNITS = ("W", "kW", "MW")
ENERGY_UNITS = ("Wh", "kWh", "MWh")
SKIP_PLATFORMS = ("mobile_app",)
MAX_THERMOSTATS = 4


@dataclass
class Equipment:
    """The equipment the blueprint automations drive, taken from their inputs."""

    climates: list[str] = field(default_factory=list)
    ev_currents: list[str] = field(default_factory=list)
    ev_switches: list[str] = field(default_factory=list)
    heaters: list[str] = field(default_factory=list)
    heater_switches: list[str] = field(default_factory=list)
    battery_reserves: list[str] = field(default_factory=list)

    @classmethod
    def from_automations(cls, automations: Iterable[dict[str, Any]]) -> Equipment:
        eq = cls()
        for auto in automations:
            bp = str((auto.get("use_blueprint") or {}).get("path", "")).rsplit("/", 1)[-1]
            inputs = (auto.get("use_blueprint") or {}).get("input") or {}
            if bp == BLUEPRINT_HVAC_SETPOINT_OFFSET:
                eq.climates += _as_list(inputs.get("climate_entity"))
            elif bp == BLUEPRINT_EV_CHARGE_RATE:
                eq.ev_currents += _as_list(inputs.get("charger_current"))
                eq.ev_switches += _as_list(inputs.get("charger_switch"))
            elif bp == BLUEPRINT_HEAT_PUMP_TARGET:
                eq.heaters += _as_list(inputs.get("water_heater"))
                eq.heater_switches += _as_list(inputs.get("power_switch"))
            elif bp == BLUEPRINT_HOME_BATTERY_RESERVE:
                eq.battery_reserves += _as_list(inputs.get("reserve_number"))
        return eq


@dataclass
class EnergyRoles:
    """The live power readings (and state of charge) the Energy view shows."""

    solar_power: str | None = None
    battery_power: str | None = None
    battery_level: str | None = None
    home_power: str | None = None
    grid_power: str | None = None


def orchestra_automations(automations: Iterable[Any]) -> list[dict[str, Any]]:
    """The automations built on Orchestra's blueprints, whoever made them."""
    out = []
    for auto in automations:
        if not isinstance(auto, dict):
            continue
        path = str((auto.get("use_blueprint") or {}).get("path", ""))
        if path.startswith(f"{BLUEPRINT_NAMESPACE}/") and auto.get("id"):
            out.append(auto)
    return out


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _dc(info: EntityInfo) -> str | None:
    return info.attributes.get("device_class")


def _is_power(info: EntityInfo) -> bool:
    return info.domain == "sensor" and (_dc(info) == "power" or (info.unit or "") in POWER_UNITS)


def _is_energy(info: EntityInfo) -> bool:
    return (
        info.domain == "sensor"
        and (_dc(info) == "energy" or (info.unit or "") in ENERGY_UNITS)
        and info.attributes.get("state_class") in ("total", "total_increasing")
    )


def _has(info: EntityInfo, *words: str) -> bool:
    return any(w in info.text for w in words)


def _ev_devices(entities: list[EntityInfo], eq: Equipment) -> set[str]:
    ids = set(eq.ev_currents) | set(eq.ev_switches)
    return {e.device_id for e in entities if e.entity_id in ids and e.device_id}


def _site_entries(entities: list[EntityInfo], ev_devices: set[str]) -> list[str | None]:
    """
    Config entries that measure the whole site: they report grid power or grid energy.

    Their solar reading comes first, because a gateway that also sees the grid and the
    battery (a Powerwall, a hybrid inverter) measures solar where it reaches the house,
    which is right even when another solar integration counts differently.
    """
    entries: list[str | None] = []
    for e in entities:
        if e.device_id in ev_devices or e.platform in SKIP_PLATFORMS:
            continue
        if (_is_power(e) or _is_energy(e)) and _has(e, "grid", "site_import", "site_export"):
            if e.config_entry_id not in entries:
                entries.append(e.config_entry_id)
    return entries


def _pick(candidates: list[EntityInfo], site_entries: list[str | None]) -> str | None:
    if not candidates:
        return None

    def rank(e: EntityInfo) -> int:
        return site_entries.index(e.config_entry_id) if e.config_entry_id in site_entries else len(site_entries)

    return sorted(candidates, key=rank)[0].entity_id


def find_energy_roles(entities: list[EntityInfo], eq: Equipment) -> EnergyRoles:
    """Solar, battery, home and grid power readings, skipping the car and phones."""
    ev = _ev_devices(entities, eq)
    usable = [e for e in entities if e.device_id not in ev and e.platform not in SKIP_PLATFORMS]
    sites = _site_entries(entities, ev)
    power = [e for e in usable if _is_power(e)]
    not_forecast = [e for e in power if not _has(e, "forecast", "estimate", "peak")]
    roles = EnergyRoles()
    roles.solar_power = _pick(
        [e for e in not_forecast if _has(e, "solar", "_pv", "pv_", "photovoltaic") and not _has(e, "export")], sites
    ) or _pick([e for e in not_forecast if _has(e, "solar", "_pv", "pv_")], sites)
    roles.battery_power = _pick([e for e in not_forecast if _has(e, "battery", "powerwall")], sites)
    roles.home_power = _pick(
        [e for e in not_forecast if _has(e, "load_power", "home_power", "house_power", "home_usage", "load", "consumption")
         and not _has(e, "solar", "battery", "grid")],
        sites,
    )
    roles.grid_power = _pick(
        [e for e in not_forecast if _has(e, "grid", "site_power") and not _has(e, "status", "solar", "battery")], sites
    )
    battery_entries = {e.config_entry_id for e in power if e.entity_id == roles.battery_power}
    roles.battery_level = _pick(
        [
            e
            for e in usable
            if e.domain == "sensor"
            and _dc(e) == "battery"
            and (e.config_entry_id in battery_entries or _has(e, "powerwall", "home_battery", "storage"))
        ],
        sites,
    ) or _pick(
        [e for e in usable if e.domain == "sensor" and (e.unit == "%") and _has(e, "powerwall", "home_battery", "storage_charge")],
        sites,
    )
    return roles


def plan_energy_prefs(entities: list[EntityInfo], eq: Equipment) -> dict[str, Any] | None:
    """
    Energy dashboard settings for a home whose settings are still empty.

    Grid needs both an import and an export meter, and every source must come from a
    lifetime (total) energy sensor, so the Energy dashboard's numbers are right. Returns
    None when there is no grid meter to build on: then the person sets it up themselves.
    """
    ev = _ev_devices(entities, eq)
    energy = [e for e in entities if _is_energy(e) and e.device_id not in ev and e.platform not in SKIP_PLATFORMS]
    sites = _site_entries(entities, ev)
    roles = find_energy_roles(entities, eq)

    def pick(cands: list[EntityInfo]) -> str | None:
        return _pick([c for c in cands if not _has(c, "today", "daily", "monthly", "yearly")], sites)

    grid = [e for e in energy if _has(e, "grid", "site_")]
    grid_in = pick([e for e in grid if _has(e, "import", "from_grid", "consumed", "purchased")])
    grid_out = pick([e for e in grid if _has(e, "export", "to_grid", "returned", "fed_in", "sold")])
    if not grid_in:
        return None
    sources: list[dict[str, Any]] = [
        {
            "type": "grid",
            "stat_energy_from": grid_in,
            "stat_energy_to": grid_out,
            "stat_cost": None,
            "entity_energy_price": None,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0,
        }
    ]
    # Tesla counts what the panels produce as "solar exported" (out of the solar), so a
    # solar meter on the site gateway is production whatever it's called
    solar_cands = [e for e in energy if _has(e, "solar", "_pv", "pv_", "production", "generat") and not _has(e, "grid", "battery")]
    solar = pick([e for e in solar_cands if not _has(e, "import")])
    if solar:
        src: dict[str, Any] = {"type": "solar", "stat_energy_from": solar, "config_entry_solar_forecast": None}
        if roles.solar_power:
            src["stat_rate"] = roles.solar_power
        sources.append(src)
    bat = [e for e in energy if _has(e, "battery", "powerwall") and not _has(e, "grid", "solar")]
    # A battery's "export" or "discharge" is energy out of it, into the house
    bat_out = pick([e for e in bat if _has(e, "export", "discharg", "from_battery")])
    bat_in = pick([e for e in bat if _has(e, "import", "charged", "to_battery") and not _has(e, "discharg")])
    if bat_out and bat_in:
        src = {"type": "battery", "stat_energy_from": bat_out, "stat_energy_to": bat_in}
        if roles.battery_power:
            src["stat_rate"] = roles.battery_power
        sources.append(src)
    return {"energy_sources": sources, "device_consumption": [], "device_consumption_water": []}


# ---------------------------------------------------------------------- the dashboard


def _tile(entity_id: str, name: str | None = None, **extra: Any) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "tile", "entity": entity_id}
    if name:
        card["name"] = name
    card.update(extra)
    return card


def _heading(text: str, icon: str | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "heading", "heading": text, "heading_style": "title"}
    if icon:
        card["icon"] = icon
    return card


def _same_device(entities: list[EntityInfo], entity_id: str) -> list[EntityInfo]:
    owner = next((e for e in entities if e.entity_id == entity_id), None)
    if not owner or not owner.device_id:
        return []
    return [e for e in entities if e.device_id == owner.device_id]


def _thermostats(entities: list[EntityInfo], climates: list[str]) -> list[str]:
    """
    The climates to show as thermostats. Zoned systems drift their zones, but people run
    the house from the main units, so show those: the zones' parent devices, or failing
    that the climates of the same integration that aren't zones.
    """
    by_id = {e.entity_id: e for e in entities}
    zones = [by_id[c] for c in climates if c in by_id and (_dc(by_id[c]) == "zone" or by_id[c].via_device_id)]
    if not zones:
        return list(climates)
    mains: list[str] = []
    for zone in zones:
        for e in entities:
            if e.domain != "climate" or e.entity_id in climates or e.entity_id in mains or _dc(e) == "zone":
                continue
            if (zone.via_device_id and e.device_id == zone.via_device_id) or (
                not zone.via_device_id and e.config_entry_id == zone.config_entry_id
            ):
                mains.append(e.entity_id)
    return sorted(mains) or list(climates)


def _equipment_sections(
    entities: list[EntityInfo], eq: Equipment, orch: dict[str, str], roles: EnergyRoles
) -> list[list[dict[str, Any]]]:
    """One section for each kind of equipment the blueprints drive."""
    sections: list[list[dict[str, Any]]] = []
    if eq.climates:
        cards: list[dict[str, Any]] = [_heading("Heating & cooling", "mdi:air-conditioner")]
        shown = _thermostats(entities, eq.climates)
        if len(shown) <= MAX_THERMOSTATS:
            for cid in shown:
                cards.append(
                    {
                        "type": "thermostat",
                        "entity": cid,
                        "show_current_as_primary": True,
                        "features": [{"type": "climate-hvac-modes", "style": "icons"}],
                        "grid_options": {"columns": 6, "rows": 4},
                    }
                )
        else:
            cards += [_tile(cid, features=[{"type": "target-temperature"}]) for cid in shown]
        sections.append(cards)
    for current in eq.ev_currents:
        device = _same_device(entities, current)
        cards = [_heading(next((e.device_name for e in device if e.device_name), "Car charging"), "mdi:car-electric")]
        level = next((e.entity_id for e in device if e.domain == "sensor" and _dc(e) == "battery"), None)
        plug = next(
            (e.entity_id for e in device if e.domain == "binary_sensor" and (_dc(e) == "plug" or _has(e, "cable", "plug"))),
            None,
        )
        charging = next(
            (e.entity_id for e in device if e.domain in ("sensor", "binary_sensor") and _has(e, "charging") and _dc(e) != "plug"),
            None,
        )
        switch = next((s for s in eq.ev_switches if any(e.entity_id == s for e in device)), None)
        for eid, name in ((level, "Battery"), (plug, "Cable"), (charging, "Charging"), (switch, "Charge")):
            if eid:
                cards.append(_tile(eid, name, grid_options={"columns": 6}))
        cards.append(_tile(current, "Charge rate", grid_options={"columns": 6}))
        sections.append(cards)
    for heater in eq.heaters:
        device = _same_device(entities, heater)
        cards = [_heading(next((e.device_name for e in device if e.device_name), "Heat pump"), "mdi:heat-pump")]
        switch = next((s for s in eq.heater_switches if any(e.entity_id == s for e in device)), None)
        if switch is None and len(eq.heaters) == 1 and eq.heater_switches:
            switch = eq.heater_switches[0]
        if switch:
            cards.append(_tile(switch, "Power", grid_options={"columns": 6}))
        cards.append(_tile(heater, grid_options={"columns": 6} if switch else {"columns": 12}))
        if orch.get("orchestra_heat_pump_target_temp"):
            cards.append(_tile(orch["orchestra_heat_pump_target_temp"], "Conductor target", grid_options={"columns": 6}))
        temps = [e.entity_id for e in device if e.domain == "sensor" and _dc(e) == "temperature"][:3]
        cards += [_tile(t, grid_options={"columns": 6}) for t in temps]
        sections.append(cards)
    energy = [
        (roles.solar_power, "Solar"),
        (roles.battery_level, "Home battery"),
        (roles.battery_power, "Battery power"),
        (roles.home_power, "Home use"),
        (roles.grid_power, "Grid"),
    ]
    if any(eid for eid, _ in energy) or eq.battery_reserves:
        cards = [_heading("Solar & battery", "mdi:solar-power-variant")]
        cards += [_tile(eid, name, grid_options={"columns": 6}) for eid, name in energy if eid]
        cards += [_tile(r, "Backup reserve", grid_options={"columns": 6}) for r in eq.battery_reserves]
        sections.append(cards)
    return sections


def _orchestra_rows(orch: dict[str, str], eq: Equipment) -> list[dict[str, Any]]:
    """Every Orchestra entity worth seeing, signals first, then what each asset is asked to do."""
    rows: list[str] = []
    for uid in (UID_CONTROL_SIGNAL, UID_DISPATCH_MODE, UID_PRICE, UID_DEMAND, UID_RENEWABLES, UID_SHED, UID_DIVIDEND):
        if orch.get(uid):
            rows.append(orch[uid])
    have = {
        "hvac": bool(eq.climates),
        "ev": bool(eq.ev_currents),
        "heat_pump": bool(eq.heaters),
        "battery": bool(eq.battery_reserves),
    }
    for asset, uids in ASSET_UIDS.items():
        if have[asset]:
            rows += [orch[u] for u in uids if orch.get(u)]
    rows += [orch[u] for u in sorted(orch) if u.startswith(RHYTHM_COUNTER_PREFIX)]
    if orch.get(UID_TODAY):
        rows.append(orch[UID_TODAY])
    return [{"entity": eid} for eid in rows]


def build_dashboard(
    entities: list[EntityInfo],
    orchestra_entities: dict[str, str],
    automations: list[dict[str, Any]],
    automation_entities: dict[str, str],
    energy_prefs: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    The Lovelace config for the Orchestra dashboard.

    `orchestra_entities` maps Orchestra's unique ids to this box's entity ids,
    `automations` are the agent's blueprint automations (as in automations.yaml) and
    `automation_entities` maps their ids to automation entity ids.
    """
    orch = {u: e for u, e in orchestra_entities.items() if not u.startswith(ALIAS_PREFIX)}
    eq = Equipment.from_automations(automations)
    roles = find_energy_roles(entities, eq)
    sources = (energy_prefs or {}).get("energy_sources") or []

    # Home: Today (asks and choices), equipment, what Orchestra is doing, market
    today: list[dict[str, Any]] = [_heading("Today", "mdi:calendar-today")]
    if orch.get(UID_CONTROL_SIGNAL):
        today.append(_tile(orch[UID_CONTROL_SIGNAL], "Conductor is asking for", grid_options={"columns": 12}))
    if orch.get(UID_OPT_OUT):
        today.append(_tile(orch[UID_OPT_OUT], "Take a break from Conductor", grid_options={"columns": 12}))
    for uid, name in ((UID_SKIP_TODAY, "Skip today"), (UID_COOLER, "Cooler please"), (UID_SKIP_EVENT, "Skip this event")):
        if orch.get(uid):
            today.append(_tile(orch[uid], name, grid_options={"columns": 4}, vertical=True, hide_state=True))
    if orch.get(UID_CALENDAR):
        today.append({"type": "calendar", "title": "Daily rhythms", "initial_view": "dayGridDay", "entities": [orch[UID_CALENDAR]]})
    rhythm_switches = [orch[u] for u in sorted(orch) if u.startswith(RHYTHM_SWITCH_PREFIX)]
    today += [_tile(s, grid_options={"columns": 6}) for s in rhythm_switches]

    equipment = _equipment_sections(entities, eq, orch, roles)

    autos = [automation_entities[a["id"]] for a in automations if automation_entities.get(a.get("id"))]
    doing: list[dict[str, Any]] = [_heading("What Orchestra is doing", "mdi:music-clef-treble")]
    if autos:
        doing.append({"type": "entities", "title": "Orchestra automations", "show_header_toggle": False, "entities": autos})
        doing.append({"type": "logbook", "title": "What Orchestra did", "hours_to_show": 24, "target": {"entity_id": autos}})
    rows = _orchestra_rows(orch, eq)
    if rows:
        doing.append({"type": "entities", "title": "What Conductor is asking for", "entities": rows})

    market: list[dict[str, Any]] = [_heading("Market signal", "mdi:transmission-tower")]
    if orch.get(UID_CONTROL_SIGNAL):
        market.append({"type": "history-graph", "title": "Conductor signal", "hours_to_show": 48, "entities": [{"entity": orch[UID_CONTROL_SIGNAL], "name": "Signal"}]})
    numeric = [(orch.get(UID_PRICE), "Wholesale price"), (orch.get(UID_DEMAND), "Demand"), (orch.get(UID_RENEWABLES), "Renewables")]
    numeric = [(e, n) for e, n in numeric if e]
    if numeric:
        market.append({"type": "history-graph", "title": "Wholesale market", "hours_to_show": 48, "entities": [{"entity": e, "name": n} for e, n in numeric]})

    sections = [s for s in (today, *equipment, doing, market) if len(s) > 1]
    home_view: dict[str, Any] = {
        "title": "Home",
        "path": "home",
        "icon": DASHBOARD_ICON,
        "type": "sections",
        "max_columns": 4,
        "badges": _badges(entities, orch),
        "sections": [{"type": "grid", "cards": cards} for cards in sections],
    }

    # Energy: the flow, then a graph for each of solar, battery, home and car
    flow: list[dict[str, Any]] = [_heading("Energy flow", "mdi:home-lightning-bolt")]
    if sources:
        flow.append({"type": "energy-distribution", "link_dashboard": True})
        if any(s.get("stat_rate") for s in sources):
            flow.append({"type": "power-sources-graph", "title": "Power right now"})
    graphs: list[tuple[str | None, str, str, str]] = [
        (roles.solar_power, "Solar", "amber", "mdi:solar-power"),
        (roles.battery_level or roles.battery_power, "Battery", "green", "mdi:home-battery"),
        (roles.home_power, "Home", "blue", "mdi:home"),
    ]
    for current in eq.ev_currents:
        level = next((e.entity_id for e in _same_device(entities, current) if e.domain == "sensor" and _dc(e) == "battery"), None)
        graphs.append((level, "Car", "red", "mdi:car-electric"))
    for eid, name, color, icon in graphs:
        if eid:
            flow.append(
                {"type": "sensor", "entity": eid, "name": name, "graph": "line", "detail": 1, "color": color, "icon": icon, "grid_options": {"columns": 3, "rows": 2}}
            )
    views = [home_view]
    if len(flow) > 1:
        views.append(
            {"title": "Energy", "path": "energy", "icon": "mdi:home-battery", "type": "sections", "max_columns": 2, "sections": [{"type": "grid", "cards": flow}]}
        )
    return {"title": DASHBOARD_TITLE, "views": views}


def _badges(entities: list[EntityInfo], orch: dict[str, str]) -> list[dict[str, Any]]:
    badges: list[dict[str, Any]] = []
    for uid in (UID_CONTROL_SIGNAL, UID_PRICE, UID_DEMAND):
        if orch.get(uid):
            badges.append({"type": "entity", "entity": orch[uid], "show_name": False, "show_state": True, "show_icon": True})
    weather = next((e.entity_id for e in entities if e.domain == "weather"), None)
    if weather:
        badges.append({"type": "entity", "entity": weather, "show_name": False, "show_state": True, "show_icon": True})
    for eid, name, color in (("sensor.sun_next_dawn", "Sunrise", "amber"), ("sensor.sun_next_dusk", "Sunset", "deep-orange")):
        if any(e.entity_id == eid for e in entities):
            badges.append({"type": "entity", "entity": eid, "name": name, "color": color, "show_name": True, "show_state": True})
    return badges
