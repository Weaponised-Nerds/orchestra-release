"""
Builds the Orchestra dashboard in Home Assistant, from `dashboard_planner.py`'s plan.

The setup agent calls this after filling the blueprints, on every pass, so equipment found
late still makes it onto the dashboard. It stops rewriting the dashboard as soon as a
person edits it: the hash of what Orchestra last saved is kept (`.storage/orchestra.dashboard`), and a dashboard that no
longer matches it is theirs. `force` (the setup assistant's tool, when the person asks for
a fresh one) rebuilds it anyway.

When the Energy dashboard has no settings yet, it fills them first, so Home Assistant's
own energy flow card has something to draw.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .dashboard_planner import (
    DASHBOARD_ICON,
    DASHBOARD_TITLE,
    DASHBOARD_URL_PATH,
    Equipment,
    build_dashboard,
    orchestra_automations,
    plan_energy_prefs,
)
from .setup_planner import EntityInfo

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "orchestra.dashboard"
STORE_VERSION = 1


class DashboardError(Exception):
    """The dashboard couldn't be made."""


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def entity_infos(hass: HomeAssistant) -> list[EntityInfo]:
    """Every entity outside Orchestra, as the planners see them."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    infos: list[EntityInfo] = []
    for state in hass.states.async_all():
        if state.domain in ("automation", "todo"):
            continue
        entry = ent_reg.async_get(state.entity_id)
        if entry and (entry.disabled_by or entry.platform == DOMAIN):
            continue
        device = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
        infos.append(
            EntityInfo(
                entity_id=state.entity_id,
                name=str(state.attributes.get("friendly_name") or ""),
                device_id=device.id if device else None,
                device_name=(device.name_by_user or device.name or "") if device else "",
                via_device_id=device.via_device_id if device else None,
                config_entry_id=entry.config_entry_id if entry else None,
                platform=entry.platform if entry else "",
                unit=state.attributes.get("unit_of_measurement"),
                attributes=dict(state.attributes),
            )
        )
    return infos


def _registry_map(hass: HomeAssistant, platform: str) -> dict[str, str]:
    """unique id -> entity id for one platform's enabled entities."""
    return {
        str(e.unique_id): e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == platform and not e.disabled_by
    }


# ---------------------------------------------------------------------- energy settings


def _legacy_grid(source: dict[str, Any]) -> dict[str, Any]:
    """The grid source as Home Assistant before late 2026 wanted it (flow_from / flow_to lists)."""
    out: dict[str, Any] = {
        "type": "grid",
        "flow_from": [
            {
                "stat_energy_from": source["stat_energy_from"],
                "stat_cost": None,
                "entity_energy_price": None,
                "number_energy_price": None,
            }
        ],
        "flow_to": [],
        "cost_adjustment_day": 0,
    }
    if source.get("stat_energy_to"):
        out["flow_to"].append(
            {
                "stat_energy_to": source["stat_energy_to"],
                "stat_compensation": None,
                "entity_energy_price": None,
                "number_energy_price": None,
            }
        )
    return out


def _fit_schema(sources: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The sources in whichever shape this Home Assistant's energy settings accept."""
    from homeassistant.components.energy.data import ENERGY_SOURCE_SCHEMA

    flat = sources
    legacy = [_legacy_grid(s) if s["type"] == "grid" else s for s in sources]
    for candidate in (flat, legacy):
        try:
            return ENERGY_SOURCE_SCHEMA(candidate)
        except Exception:  # vol.Invalid, or a shape this version doesn't know
            continue
    # Some versions reject the power readings: keep the energy meters
    bare = [{k: v for k, v in s.items() if k != "stat_rate"} for s in legacy]
    try:
        return ENERGY_SOURCE_SCHEMA(bare)
    except Exception:
        return None


async def async_fill_energy_prefs(hass: HomeAssistant, infos: list[EntityInfo], eq: Equipment) -> dict[str, Any] | None:
    """The Energy dashboard settings, filled in first when nobody has set them."""
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return None
    try:
        manager = await async_get_manager(hass)
    except Exception as err:  # the energy integration isn't loaded
        _LOGGER.debug("Orchestra could not read the energy settings: %s", err)
        return None
    current = manager.data or {}
    if current.get("energy_sources"):
        return current
    plan = plan_energy_prefs(infos, eq)
    if not plan:
        return current or None
    sources = _fit_schema(plan["energy_sources"])
    if not sources:
        return current or None
    await manager.async_update({"energy_sources": sources})
    _LOGGER.info("Orchestra filled the Energy dashboard settings: %s", [s.get("type") for s in sources])
    return manager.data


# ---------------------------------------------------------------------- the dashboard


def _dashboards_collection(hass: HomeAssistant) -> Any:
    """Lovelace keeps its dashboards collection to itself; its list command holds it."""
    handlers = hass.data.get("websocket_api") or {}
    handler = (handlers.get("lovelace/dashboards/list") or (None,))[0]
    handler = inspect.unwrap(handler) if handler else None
    owner = getattr(handler, "__self__", None)
    return getattr(owner, "storage_collection", None)


def _lovelace_dashboards(hass: HomeAssistant) -> dict[str | None, Any]:
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
    except ImportError:
        LOVELACE_DATA = "lovelace"  # type: ignore[assignment]
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return {}
    return getattr(data, "dashboards", None) or (data.get("dashboards") if isinstance(data, dict) else {}) or {}


async def async_build_dashboard(
    hass: HomeAssistant,
    automations: list[Any],
    force: bool = False,
) -> dict[str, Any]:
    """
    Make (or refresh) the Orchestra dashboard. Returns {"url": ..., "hash": ...}, or
    {"skipped": reason} when the person has made it their own.
    """
    store: Store = Store(hass, STORE_VERSION, STORE_KEY)
    last_hash = ((await store.async_load()) or {}).get("hash")
    infos = entity_infos(hass)
    autos = orchestra_automations(automations)
    eq = Equipment.from_automations(autos)
    prefs = await async_fill_energy_prefs(hass, infos, eq)
    config = build_dashboard(
        infos,
        _registry_map(hass, DOMAIN),
        autos,
        _registry_map(hass, "automation"),
        prefs,
    )

    dashboards = _lovelace_dashboards(hass)
    dashboard = dashboards.get(DASHBOARD_URL_PATH)
    if dashboard is None:
        collection = _dashboards_collection(hass)
        if collection is None:
            raise DashboardError("Home Assistant's dashboards aren't ready yet.")
        await collection.async_create_item(
            {
                "url_path": DASHBOARD_URL_PATH,
                "title": DASHBOARD_TITLE,
                "icon": DASHBOARD_ICON,
                "show_in_sidebar": True,
                "require_admin": False,
            }
        )
        dashboard = _lovelace_dashboards(hass).get(DASHBOARD_URL_PATH)
        if dashboard is None:
            raise DashboardError("The Orchestra dashboard couldn't be registered.")
    elif not force:
        try:
            saved = await dashboard.async_load(False)
        except Exception:  # never saved: no config yet
            saved = None
        if saved and config_hash(saved) != last_hash:
            return {"skipped": "The Orchestra dashboard has been edited, so Orchestra leaves it alone.", "url": f"/{DASHBOARD_URL_PATH}"}
    await dashboard.async_save(config)
    await store.async_save({"hash": config_hash(config)})
    return {
        "url": f"/{DASHBOARD_URL_PATH}",
        "views": [v["title"] for v in config["views"]],
        "energy_settings": bool((prefs or {}).get("energy_sources")),
    }
