"""
Daily rhythms: the pure planning half (no Home Assistant imports, so it is unit tested).

A rhythm is a small daily routine that is worth having with or without Conductor:
blinds that keep the sun out on hot days, blinds that open in the morning, heavy loads
held off during the evening peak. Each morning Overture plans the day's rhythm events
from the sun's path and the local forecast, and publishes them on `calendar.orchestra_today`.
The rhythm blueprints trigger on those events and call `orchestra.rhythm_step`.

Everything here runs from data Home Assistant already has: the home's latitude and
longitude, its time zone and its weather entity. Nothing is sent anywhere.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

RHYTHM_HEAT_SHIELD = "heat_shield"
RHYTHM_MORNING_OPEN_UP = "morning_open_up"
RHYTHM_PEAK_QUIET = "peak_quiet"

RHYTHMS: dict[str, str] = {
    RHYTHM_HEAT_SHIELD: "Heat shield",
    RHYTHM_MORNING_OPEN_UP: "Morning open-up",
    RHYTHM_PEAK_QUIET: "Peak quiet",
}

# Which way a window faces, as the compass bearing of the direction it looks out to
FACINGS: dict[str, float] = {"north": 0.0, "east": 90.0, "south": 180.0, "west": 270.0}

DEFAULT_SETTINGS: dict[str, Any] = {
    "headsup_time": "07:00",
    "quiet_start": "21:00",
    "quiet_end": "07:00",
    "wake_time": "07:00",
    # Living area blinds open at sunrise, but not before this
    "open_up_earliest": "06:00",
    "peak_start": "16:00",
    "peak_end": "21:00",
    # Days with a forecast maximum at or above this get the heat shield
    "hot_day_c": 28.0,
    # Leave empty to send to every Home Assistant companion app
    "notify_targets": [],
    # Leave empty to use the first weather entity
    "weather_entity": "",
    "enabled": {key: True for key in RHYTHMS},
    # Tell the household when a Conductor event starts (event_notify.py)
    "event_notifications": True,
    # A retailer program that pays for taking part, shown in event notices when set
    "incentive_program": "",
    "incentive_aud_per_kwh": 0.0,
}

# A window gets direct sun while the sun is above this elevation and within this many
# degrees either side of the way the window faces
SUN_MIN_ELEVATION = 10.0
SUN_HALF_ANGLE = 70.0
SAMPLE_MINUTES = 5
# Shorter sunny spells than this aren't worth moving blinds for
MIN_SHIELD_MINUTES = 45
# Each "cooler please" lowers the most the A/C may be warmed by this much, for a week
COMFORT_STEP_C = 0.5
COMFORT_DAYS = 7

DEG = math.pi / 180


def solar_position(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Sun elevation and azimuth in degrees (azimuth clockwise from north), NOAA equations."""
    unix = when.astimezone(timezone.utc).timestamp()
    jd = unix / 86400 + 2440587.5
    t = (jd - 2451545) / 36525
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (
        math.sin(m * DEG) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m * DEG) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m * DEG) * 0.000289
    )
    omega = 125.04 - 1934.136 * t
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(omega * DEG)
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(omega * DEG)
    decl = math.asin(math.sin(eps * DEG) * math.sin(lam * DEG))
    y = math.tan(eps * DEG / 2) ** 2
    eq_time = 4 / DEG * (
        y * math.sin(2 * l0 * DEG)
        - 2 * e * math.sin(m * DEG)
        + 4 * e * y * math.sin(m * DEG) * math.cos(2 * l0 * DEG)
        - 0.5 * y * y * math.sin(4 * l0 * DEG)
        - 1.25 * e * e * math.sin(2 * m * DEG)
    )
    utc_minutes = (unix % 86400) / 60
    true_solar = (utc_minutes + eq_time + 4 * lon) % 1440
    hour_angle = (true_solar / 4 - 180) * DEG
    phi = lat * DEG
    cos_z = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    zenith = math.acos(max(-1.0, min(1.0, cos_z)))
    elevation = 90 - zenith / DEG
    sin_z = math.sin(zenith)
    if abs(sin_z) < 1e-9:
        return elevation, 0.0
    cos_az = (math.sin(phi) * math.cos(zenith) - math.sin(decl)) / (math.cos(phi) * sin_z)
    az = math.acos(max(-1.0, min(1.0, cos_az))) / DEG
    azimuth = (az + 180) % 360 if hour_angle > 0 else (540 - az) % 360
    return elevation, azimuth


def _angle_between(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _samples(day: date, tz: tzinfo) -> list[datetime]:
    start = datetime.combine(day, time(0, 0), tz)
    return [start + timedelta(minutes=i) for i in range(0, 24 * 60, SAMPLE_MINUTES)]


def sun_times(day: date, tz: tzinfo, lat: float, lon: float) -> tuple[datetime | None, datetime | None]:
    """Sunrise and sunset to the nearest few minutes (None in polar day or night)."""
    up = [s for s in _samples(day, tz) if solar_position(s, lat, lon)[0] > -0.833]
    if not up:
        return None, None
    return up[0], up[-1] + timedelta(minutes=SAMPLE_MINUTES)


def sunny_windows(day: date, tz: tzinfo, lat: float, lon: float, facing: float) -> list[tuple[datetime, datetime]]:
    """The spells when the sun shines onto a window facing `facing` degrees."""
    spells: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    for sample in _samples(day, tz):
        elevation, azimuth = solar_position(sample, lat, lon)
        lit = elevation >= SUN_MIN_ELEVATION and _angle_between(azimuth, facing) <= SUN_HALF_ANGLE
        if lit and start is None:
            start = sample
        elif not lit and start is not None:
            spells.append((start, sample))
            start = None
    if start is not None:
        spells.append((start, datetime.combine(day + timedelta(days=1), time(0, 0), tz)))
    return [(a, b) for a, b in spells if (b - a) >= timedelta(minutes=MIN_SHIELD_MINUTES)]


def parse_hhmm(value: str, fallback: str = "00:00") -> time:
    for text in (value, fallback):
        try:
            hours, minutes = str(text).split(":")[:2]
            return time(int(hours), int(minutes))
        except (ValueError, TypeError):
            continue
    return time(0, 0)


def in_quiet_hours(moment: time, start: time, end: time) -> bool:
    """True inside [start, end), which may wrap past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def event_tag(rhythm: str, group: str) -> str:
    """The machine-readable tag in an event's description that the blueprints match on."""
    return f"[orchestra:{rhythm}:{group}]"


@dataclass
class RhythmEvent:
    """One step of one rhythm on today's calendar."""

    uid: str
    rhythm: str
    group: str
    start: datetime
    end: datetime
    summary: str
    description: str

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["start"] = self.start.isoformat()
        out["end"] = self.end.isoformat()
        return out


@dataclass
class DayPlan:
    day: date
    forecast_max_c: float | None
    hot_day: bool
    sunrise: datetime | None
    sunset: datetime | None
    events: list[RhythmEvent] = field(default_factory=list)


def _clock(moment: datetime) -> str:
    text = moment.strftime("%I:%M %p").lstrip("0").lower()
    return text.replace(":00 ", " ")


def plan_day(
    day: date,
    *,
    tz: tzinfo,
    lat: float,
    lon: float,
    forecast_max_c: float | None,
    settings: dict[str, Any],
) -> DayPlan:
    """Today's rhythm events, from the sun's path, the forecast and the household's settings."""
    enabled = {**DEFAULT_SETTINGS["enabled"], **(settings.get("enabled") or {})}
    hot_day_c = float(settings.get("hot_day_c", DEFAULT_SETTINGS["hot_day_c"]))
    sunrise, sunset = sun_times(day, tz, lat, lon)
    hot = forecast_max_c is not None and forecast_max_c >= hot_day_c
    plan = DayPlan(day, forecast_max_c, hot, sunrise, sunset)
    stamp = day.isoformat()

    def add(rhythm: str, group: str, start: datetime, end: datetime, summary: str, words: str) -> None:
        plan.events.append(
            RhythmEvent(
                uid=f"{stamp}-{rhythm}-{group}",
                rhythm=rhythm,
                group=group,
                start=start,
                end=end,
                summary=summary,
                description=f"{words}\n{event_tag(rhythm, group)}",
            )
        )

    if enabled.get(RHYTHM_MORNING_OPEN_UP) and sunrise is not None:
        wake = datetime.combine(day, parse_hhmm(settings.get("wake_time", ""), DEFAULT_SETTINGS["wake_time"]), tz)
        earliest = datetime.combine(
            day, parse_hhmm(settings.get("open_up_earliest", ""), DEFAULT_SETTINGS["open_up_earliest"]), tz
        )
        living = max(sunrise, earliest)
        add(
            RHYTHM_MORNING_OPEN_UP, "living", living, living + timedelta(minutes=5),
            "Morning open-up",
            "Opens the living area blinds and curtains at sunrise (not before the earliest time set), except any the heat shield is keeping shut.",
        )
        bedrooms = max(wake, sunrise)
        add(
            RHYTHM_MORNING_OPEN_UP, "bedrooms", bedrooms, bedrooms + timedelta(minutes=5),
            "Morning open-up: bedrooms",
            "Opens the bedroom blinds and curtains at the household's wake time.",
        )

    if enabled.get(RHYTHM_HEAT_SHIELD) and hot:
        for name, bearing in FACINGS.items():
            for index, (start, end) in enumerate(sunny_windows(day, tz, lat, lon, bearing)):
                group = name if index == 0 else f"{name}{index + 1}"
                add(
                    RHYTHM_HEAT_SHIELD, group, start, end,
                    f"Heat shield: {name} blinds",
                    f"Forecast {forecast_max_c:g} °C. Shuts the {name}-facing blinds from {_clock(start)} "
                    f"to {_clock(end)} while the sun is on them, then opens the ones still shut.",
                )

    if enabled.get(RHYTHM_PEAK_QUIET):
        start = datetime.combine(day, parse_hhmm(settings.get("peak_start", ""), DEFAULT_SETTINGS["peak_start"]), tz)
        end = datetime.combine(day, parse_hhmm(settings.get("peak_end", ""), DEFAULT_SETTINGS["peak_end"]), tz)
        if end <= start:
            end += timedelta(days=1)
        add(
            RHYTHM_PEAK_QUIET, "evening", start, end,
            "Peak quiet",
            f"Holds the chosen heavy loads off from {_clock(start)} to {_clock(end)}, "
            "then switches back on the ones it switched off.",
        )

    plan.events.sort(key=lambda ev: (ev.start, ev.rhythm, ev.group))
    return plan


def headsup_text(plan: DayPlan) -> str | None:
    """The morning heads-up, or None on a day with nothing out of the ordinary."""
    shields = [ev for ev in plan.events if ev.rhythm == RHYTHM_HEAT_SHIELD]
    if not shields:
        return None
    parts = [f"Hot day, {plan.forecast_max_c:g} °C."]
    spans = [f"{ev.group.rstrip('0123456789')} {_clock(ev.start)} to {_clock(ev.end)}" for ev in shields]
    parts.append("Blinds shut on the sunny side: " + ", ".join(spans) + ".")
    peak = next((ev for ev in plan.events if ev.rhythm == RHYTHM_PEAK_QUIET), None)
    if peak:
        parts.append(f"Heavy loads wait until after {_clock(peak.end)}.")
    parts.append("If Conductor pre-cools today, all blinds shut first to keep the cool in.")
    return " ".join(parts)


def comfort_offset(
    offset: float,
    *,
    max_offset: float,
    cooler_now: bool,
    taps: list[datetime],
    now: datetime,
) -> float:
    """
    The A/C offset after the household's "cooler please" taps.

    Only warming offsets (Conductor easing the A/C during a peak) are softened. A tap ends
    the current warming event, and each tap in the last week lowers the warming limit.
    """
    if offset <= 0:
        return offset
    if cooler_now:
        return 0.0
    recent = [t for t in taps if now - t <= timedelta(days=COMFORT_DAYS)]
    limit = max(0.0, max_offset - COMFORT_STEP_C * len(recent))
    return min(offset, limit)


# What each rhythm step does to a device, and how it is put back
COVER_SHUT = ("closed", "closing")
COVER_OPEN = ("open", "opening")


def plan_hold(state: str | None, *, want: str) -> str:
    """
    For a step that moves a device to `want` ("closed" or "off"): "act" to move it
    and remember it, "leave" when it is already there or unavailable.
    """
    if state is None or state in ("unavailable", "unknown"):
        return "leave"
    if want == "closed":
        return "leave" if state in COVER_SHUT else "act"
    return "leave" if state == want else "act"


def plan_release(state: str | None, *, set_to: str) -> str:
    """
    At the end of a step: "restore" a device still where Overture put it, "drop" one
    the household has changed since (their change wins).
    """
    if state is None or state in ("unavailable", "unknown"):
        return "drop"
    if set_to == "closed":
        return "restore" if state in COVER_SHUT else "drop"
    return "restore" if state == set_to else "drop"
