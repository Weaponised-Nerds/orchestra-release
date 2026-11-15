"""
What the household is told when a Conductor event starts (no Home Assistant imports).

Each notification says three things in plain words: what is happening, which of their
own equipment responds and how, and why it is worth it. The "why" is the point: a
household that understands the benefit keeps Orchestra switched on.

Amounts of money appear only when they are known: a rate in the Conductor frame
(`incentive`), or one the household or installer set for their retailer's program.
Nothing is estimated or invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SIGNAL_ENERGY_SOAK = "Energy Soak"
SIGNAL_LOAD_SHED = "Load Shed"

# The assignment kinds (asset_assignments.ASSET_KINDS) that follow each asset class
KIND_FOR_ASSET = {
    "hvac": "air_conditioning",
    "heat_pump": "heat_pump",
    "ev": "ev_charger",
    "home_battery": "home_battery",
}

# A short line of background, picked by the day of the year so it varies but repeats rarely
TIPS = (
    "Perth's rooftops make more solar at midday than the grid can use, then demand peaks after sunset.",
    "Moving the same energy use a few hours earlier costs nothing in comfort and takes strain off the grid.",
    "Every home that eases off at the peak means a gas peaking plant can stay switched off.",
    "A house cooled at midday stays comfortable for hours, like a battery made of bricks and air.",
    "Hot water is stored energy: heat it while solar is plentiful and it lasts into the evening.",
)


@dataclass
class EventNotice:
    title: str
    message: str
    key: str
    lines: list[str] = field(default_factory=list)
    benefit: str = ""
    cooler_offered: bool = False


def _names(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _c_per_kwh(aud_mwh: float) -> str:
    cents = aud_mwh / 10
    return f"{cents:.1f} c/kWh" if abs(cents) < 10 else f"{cents:.0f} c/kWh"


def asset_lines(dispatch: Any, equipment: dict[str, list[str]]) -> list[str]:
    """One line per kind of the home's equipment that responds, in the household's words."""
    excluded = set(getattr(dispatch, "excluded_assets", []) or [])
    lines: list[str] = []

    names = equipment.get("hvac") or []
    if names and "hvac" not in excluded:
        offset = abs(float(dispatch.ac_setpoint_offset_c))
        mode = dispatch.ac_dispatch_mode
        if mode == "precool" and offset:
            lines.append(f"{_names(names)}: cools {offset:g} °C below your setting now, so the house stays cool later.")
        elif mode == "preheat" and offset:
            lines.append(f"{_names(names)}: warms {offset:g} °C above your setting now, so the house stays warm later.")
        elif mode == "shed" and offset:
            lines.append(f"{_names(names)}: eases {offset:g} °C off your setting until the peak passes.")

    names = equipment.get("heat_pump") or []
    if names and "heat_pump" not in excluded:
        action = str(dispatch.heat_pump_action or "").upper()
        if dispatch.heat_pump_run_now:
            lines.append(f"{_names(names)}: heats now, up to {dispatch.heat_pump_target_temp_c:g} °C.")
        elif "COAST" in action:
            lines.append(f"{_names(names)}: rests on the hot water already stored until the peak passes.")

    names = equipment.get("ev") or []
    if names and "ev" not in excluded:
        if dispatch.ev_charge_now:
            lines.append(f"{_names(names)}: charges now, at up to {int(dispatch.ev_target_amps)} A.")
        elif int(dispatch.ev_target_amps) == 0:
            lines.append(f"{_names(names)}: pauses charging until the peak passes, then carries on.")

    names = equipment.get("home_battery") or []
    if names and "home_battery" not in excluded:
        action = str(dispatch.battery_action or "").upper()
        if dispatch.battery_charge_from_grid or dispatch.home_battery_storm_watch:
            lines.append(f"{_names(names)}: tops up to {dispatch.battery_min_reserve_soc_pct:g}% from the grid.")
        elif "DISCHARGE" in action:
            lines.append(f"{_names(names)}: powers your home through the peak, and sends any spare to the grid.")
    return lines


def benefit_text(signal: str, market: Any, incentive: dict[str, Any], battery_exports: bool) -> str:
    """Why this event is worth it, from the live market and any known payment."""
    price = float(getattr(market, "wholesale_price_aud_mwh", 0.0))
    renewable = float(getattr(market, "renewable_availability_pct", 0.0))
    if signal == SIGNAL_ENERGY_SOAK:
        if price < 0:
            text = (
                f"There's more solar on the grid than it can use (wholesale price {_c_per_kwh(price)}, below zero). "
                "Using it now is clean and cheap, and saves running the same equipment in the evening peak."
            )
        else:
            text = (
                f"Power is cheap and {renewable:.0f}% renewable right now ({_c_per_kwh(price)} wholesale). "
                "Using it now saves running the same equipment in the evening peak."
            )
    else:
        text = (
            f"This is the evening peak, when the grid is most strained and power costs most "
            f"({_c_per_kwh(price)} wholesale now). Easing off for a few hours keeps gas peaking plants switched off."
        )
    rate = _as_float(incentive.get("aud_per_kwh"))
    program = str(incentive.get("program") or "").strip()
    if rate:
        who = program or "Your retailer's program"
        if signal == SIGNAL_LOAD_SHED:
            what = "you send to the grid or shift out of the peak" if battery_exports else "you shift out of the peak"
        else:
            what = "you soak up"
        text += f" {who} pays ${rate:.2f} for each kWh {what}."
    return text


def compose_event_notice(
    dispatch: Any,
    market: Any,
    equipment: dict[str, list[str]],
    incentive: dict[str, Any],
    day_of_year: int,
) -> EventNotice | None:
    """The notification for an event, or None when none of the home's equipment responds."""
    signal = dispatch.control_signal
    if signal not in (SIGNAL_ENERGY_SOAK, SIGNAL_LOAD_SHED) or dispatch.opted_out:
        return None
    lines = asset_lines(dispatch, equipment)
    if not lines:
        return None
    battery_exports = "DISCHARGE" in str(dispatch.battery_action or "").upper() and bool(equipment.get("home_battery"))
    benefit = benefit_text(signal, market, incentive, battery_exports)
    title = "Orchestra: soaking up spare solar" if signal == SIGNAL_ENERGY_SOAK else "Orchestra: easing off for the peak"
    tip = TIPS[day_of_year % len(TIPS)]
    message = "\n".join([*lines, "", benefit, "", tip])
    cooler = dispatch.ac_dispatch_mode == "shed" and bool(equipment.get("hvac")) and "hvac" not in (
        getattr(dispatch, "excluded_assets", []) or []
    )
    responding = sorted(k for k in KIND_FOR_ASSET if equipment.get(k))
    return EventNotice(
        title=title,
        message=message,
        key=f"{signal}|{','.join(responding)}",
        lines=lines,
        benefit=benefit,
        cooler_offered=cooler,
    )


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
