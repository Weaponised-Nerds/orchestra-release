"""
Which of the home's equipment follows Conductor: the Overture blueprint automations.

The setup agent prefills these on first boot (`setup_planner.plan_asset_automations`).
This module lets the setup assistant read them back and assign equipment the agent
missed or got wrong, for example a heat pump or battery set up after the first day.
Assignments live in automations.yaml as ordinary blueprint automations, so the person can
still see and edit them under Settings → Automations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from pathlib import Path
from typing import Any

from .blueprint_installer import (
    BLUEPRINT_EV_CHARGE_RATE,
    BLUEPRINT_HEAT_PUMP_TARGET,
    BLUEPRINT_HEAT_SHIELD,
    BLUEPRINT_HOME_BATTERY_RESERVE,
    BLUEPRINT_HVAC_SETPOINT_OFFSET,
    BLUEPRINT_MORNING_OPEN_UP,
    BLUEPRINT_PEAK_QUIET,
    blueprint_path,
)


@dataclass(frozen=True)
class AssetKind:
    """One Overture blueprint, and the inputs the assistant may fill."""

    blueprint: str
    title: str
    main_input: str
    main_domains: tuple[str, ...]
    entity_inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    value_inputs: tuple[str, ...] = ()
    multiple: bool = False
    help: str = ""
    # Settings that take a list of entities (a rhythm's other groups of blinds)
    list_inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # One automation for the whole home, under this id
    fixed_id: str = ""
    # A daily rhythm, which runs with or without Conductor
    rhythm: bool = False


ASSET_KINDS: dict[str, AssetKind] = {
    "air_conditioning": AssetKind(
        BLUEPRINT_HVAC_SETPOINT_OFFSET,
        "Air conditioning",
        "climate_entity",
        ("climate",),
        value_inputs=("skip_when_off", "turn_on_for_pre", "invert_shed_when_heating"),
        multiple=True,
        help=(
            "One automation for the whole home: list every climate that should drift. On "
            "zoned systems (AirTouch and similar) list the zone climates, not the main unit."
        ),
    ),
    "heat_pump": AssetKind(
        BLUEPRINT_HEAT_PUMP_TARGET,
        "Hot water / heat pump",
        "water_heater",
        ("water_heater", "climate", "switch"),
        entity_inputs={"power_switch": ("switch", "input_boolean", "climate", "water_heater")},
        value_inputs=(
            "boost_temperature",
            "boost_operation_mode",
            "coast_temperature",
            "coast_operation_mode",
            "normal_temperature",
            "normal_operation_mode",
        ),
        help="One per heat pump or hot water system. power_switch is its separate on/off switch, if it has one.",
    ),
    "ev_charger": AssetKind(
        BLUEPRINT_EV_CHARGE_RATE,
        "EV charger",
        "charger_current",
        ("number",),
        entity_inputs={"charger_switch": ("switch",)},
        value_inputs=("normal_amps",),
        help="One per charger: its charging current (amps) number, and its charge on/off switch if it has one.",
    ),
    "home_battery": AssetKind(
        BLUEPRINT_HOME_BATTERY_RESERVE,
        "Home battery",
        "reserve_number",
        ("number",),
        entity_inputs={"grid_charge_switch": ("switch",)},
        value_inputs=("normal_reserve",),
        help="One per battery: its backup reserve (%) number, and its grid charging switch if it has one.",
    ),
    "heat_shield": AssetKind(
        BLUEPRINT_HEAT_SHIELD,
        "Heat shield",
        "unknown_covers",
        ("cover",),
        list_inputs={f"{side}_covers": ("cover",) for side in ("east", "north", "west", "south")},
        multiple=True,
        fixed_id="orchestra_rhythm_heat_shield",
        rhythm=True,
        help=(
            "Daily rhythm, one for the whole home. entities = blinds and curtains whose facing is "
            "unknown (they shut only for Conductor pre-cooling); settings east_covers, north_covers, "
            "west_covers and south_covers = the blinds on each side of the house, which shut while "
            "the sun is on that side on hot days. Ask the household which way each window faces."
        ),
    ),
    "morning_open_up": AssetKind(
        BLUEPRINT_MORNING_OPEN_UP,
        "Morning open-up",
        "living_covers",
        ("cover",),
        list_inputs={"bedroom_covers": ("cover",)},
        multiple=True,
        fixed_id="orchestra_rhythm_morning_open_up",
        rhythm=True,
        help=(
            "Daily rhythm, one for the whole home. entities = living area blinds and curtains (open at "
            "sunrise, not before 6 am); settings bedroom_covers = bedroom ones (open at the wake time)."
        ),
    ),
    "peak_quiet": AssetKind(
        BLUEPRINT_PEAK_QUIET,
        "Peak quiet",
        "loads",
        ("switch", "input_boolean"),
        multiple=True,
        fixed_id="orchestra_rhythm_peak_quiet",
        rhythm=True,
        help=(
            "Daily rhythm, one for the whole home. entities = switches for heavy loads to hold off "
            "through the evening peak (pool pump, spa heater). Leave EV chargers and A/Cs to their "
            "Conductor blueprints."
        ),
    ),
}
_KIND_BY_BLUEPRINT = {blueprint_path(k.blueprint): name for name, k in ASSET_KINDS.items()}


class AssignmentError(Exception):
    """Why an assignment was refused, in words the assistant can pass on."""


# Automations made before the Orchestra -> Overture rename carry the old name. Only the lines
# Overture wrote are touched (its aliases and description), as text, so the rest of the file
# keeps its formatting and comments.
_OLD_ALIAS = re.compile(r"^(\s*(?:-\s+)?alias:\s*['\"]?)Orchestra(?=:| rhythm:)", re.MULTILINE)
_OLD_DESCRIPTION = re.compile(
    r"^(\s*(?:-\s+)?description:\s*['\"]?Assigned by the )Orchestra( setup assistant)", re.MULTILINE
)


def rebrand_automation_names(text: str) -> str:
    """automations.yaml text with Overture's old 'Orchestra: ...' names renamed to 'Overture: ...'."""
    text = _OLD_ALIAS.sub(r"\1Overture", text)
    return _OLD_DESCRIPTION.sub(r"\1Overture\2", text)


def rebrand_automations_file(path: Path) -> bool:
    """Rename Overture's automations in place. True when the file changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    renamed = rebrand_automation_names(text)
    if renamed == text:
        return False
    path.write_text(renamed, encoding="utf-8")
    return True


def automations_path(hass: Any) -> Path:
    return Path(hass.config.path("automations.yaml"))


def describe_assignments(automations: list[Any]) -> list[dict[str, Any]]:
    """The Overture blueprint automations in automations.yaml."""
    out = []
    for automation in automations:
        if not isinstance(automation, dict):
            continue
        use = automation.get("use_blueprint") or {}
        kind = _KIND_BY_BLUEPRINT.get(str(use.get("path", "")))
        if kind:
            out.append(
                {
                    "kind": kind,
                    "automation_id": automation.get("id"),
                    "name": automation.get("alias"),
                    "inputs": dict(use.get("input") or {}),
                }
            )
    return out


def apply_assignment(
    automations: list[Any],
    kind_name: str,
    entities: list[str],
    settings: dict[str, Any],
    domain_of: Any,
) -> tuple[list[Any], dict[str, Any]]:
    """
    Add or update the automation for one piece of equipment.

    `domain_of(entity_id)` returns the entity's domain, or None when it doesn't exist.
    Returns the new automation list and the assignment as described to the assistant.
    """
    kind = ASSET_KINDS.get(kind_name)
    if kind is None:
        raise AssignmentError(f"Unknown kind '{kind_name}'. Use one of: {', '.join(ASSET_KINDS)}.")
    if (not entities and not (kind.rhythm and settings)) or (len(entities) > 1 and not kind.multiple):
        raise AssignmentError(
            f"{kind.title} takes exactly one {kind.main_input} entity." if not kind.multiple
            else "List at least one climate entity."
        )
    for entity_id in entities:
        _check_entity(entity_id, kind.main_domains, domain_of)

    inputs: dict[str, Any] = {}
    for key, value in settings.items():
        if key in kind.list_inputs:
            values = [str(v) for v in (value if isinstance(value, list) else [value]) if v]
            for entity_id in values:
                _check_entity(entity_id, kind.list_inputs[key], domain_of)
            inputs[key] = sorted(set(values))
        elif key in kind.entity_inputs:
            if value:
                _check_entity(str(value), kind.entity_inputs[key], domain_of)
            inputs[key] = str(value or "")
        elif key in kind.value_inputs:
            inputs[key] = value
        else:
            allowed = list(kind.list_inputs) + list(kind.entity_inputs) + list(kind.value_inputs)
            raise AssignmentError(
                f"{kind.title} has no setting '{key}'." + (f" It takes: {', '.join(allowed)}." if allowed else "")
            )

    if kind.multiple:
        automation_id = kind.fixed_id or "orchestra_hvac_offset"
        if entities or not kind.rhythm:
            inputs[kind.main_input] = sorted(set(entities))
    else:
        automation_id = f"orchestra_{_ID_PREFIX[kind_name]}_{entities[0].split('.', 1)[1]}"
        inputs[kind.main_input] = entities[0]

    path = blueprint_path(kind.blueprint)
    updated = list(automations)
    for index, automation in enumerate(updated):
        if not isinstance(automation, dict):
            continue
        use = automation.get("use_blueprint") or {}
        if automation.get("id") == automation_id or (
            use.get("path") == path
            and (kind.fixed_id or (use.get("input") or {}).get(kind.main_input) == inputs[kind.main_input])
        ):
            merged = {**(use.get("input") or {}), **inputs}
            if kind.rhythm and kind.main_input not in inputs:
                # A blind given a side leaves the "facing unknown" (or other main) list
                placed = {e for key in kind.list_inputs if key in inputs for e in inputs[key]}
                merged[kind.main_input] = [e for e in merged.get(kind.main_input) or [] if e not in placed]
            updated[index] = {**automation, "use_blueprint": {"path": path, "input": merged}}
            return updated, {"kind": kind_name, "automation_id": automation.get("id"), "inputs": merged, "updated": True}

    names = ", ".join(entities)
    updated.append(
        {
            "id": automation_id,
            "alias": f"Overture: {kind.title.lower()} follows Conductor ({names})"
            if not kind.multiple
            else (f"Overture rhythm: {kind.title.lower()}" if kind.rhythm else "Overture: air conditioning follows Conductor"),
            "description": "Assigned by the Overture setup assistant.",
            "use_blueprint": {"path": path, "input": inputs},
        }
    )
    return updated, {"kind": kind_name, "automation_id": automation_id, "inputs": inputs, "updated": False}


def remove_assignment(automations: list[Any], automation_id: str) -> list[Any]:
    kept = [
        a
        for a in automations
        if not (isinstance(a, dict) and a.get("id") == automation_id and describe_assignments([a]))
    ]
    if len(kept) == len(automations):
        raise AssignmentError("There is no Overture blueprint automation with that id.")
    return kept


_ID_PREFIX = {"heat_pump": "heat_pump", "ev_charger": "ev_rate", "home_battery": "battery_reserve"}


def _check_entity(entity_id: str, domains: tuple[str, ...], domain_of: Any) -> None:
    domain = domain_of(entity_id)
    if domain is None:
        raise AssignmentError(f"There is no entity {entity_id}. Use orchestra_list_entities to find it.")
    if domain not in domains:
        raise AssignmentError(f"{entity_id} is a {domain}; this needs a {' or '.join(domains)} entity.")
