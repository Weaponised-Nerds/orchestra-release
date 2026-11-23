"""
Add-on settings for the setup assistant, through Home Assistant's Supervisor.

The same things a person does on an add-on's Info and Configuration tabs: see what is
installed or in the store, read and change an add-on's options, its start-on-boot, watchdog
and auto-update switches, and install, update, start, stop or restart it. Uninstalling is
left to the person.

Passwords, tokens and keys never pass through the assistant: they are shown as "(set)" or
"(not set)" and can't be changed here, so the person types them on the add-on's
Configuration tab, as with Home Assistant logins.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from homeassistant.core import HomeAssistant

SECRET_SHOWN = "(set)"
SECRET_EMPTY = "(not set)"
SECRET_NAME_RE = re.compile(r"pass(word|wd|phrase)?$|secret|token|api_?key|private_?key|psk", re.IGNORECASE)
ACTIONS = ("install", "update", "start", "stop", "restart")
MAX_STORE_RESULTS = 25


class AddonError(Exception):
    """Why an add-on request failed, in words the assistant can pass on."""


def _client(hass: HomeAssistant) -> Any:
    if "hassio" not in hass.config.components:
        raise AddonError(
            "This Home Assistant has no add-ons: that needs Home Assistant OS, which the Overture image runs."
        )
    from homeassistant.components.hassio.handler import get_supervisor_client

    return get_supervisor_client(hass)


def config_url(slug: str) -> str:
    return f"/hassio/addon/{slug}/config"


async def async_list_addons(hass: HomeAssistant, search: str | None = None) -> dict[str, Any]:
    """Installed add-ons, and store add-ons matching `search`."""
    client = _client(hass)
    try:
        installed = await client.addons.list()
        out: dict[str, Any] = {
            "installed": [
                {
                    "addon": a.slug,
                    "name": a.name,
                    "state": str(_value(a.state)),
                    "version": a.version,
                    "update_available": bool(a.update_available),
                }
                for a in installed
            ]
        }
        if search:
            needle = search.lower()
            store = await client.store.addons_list()
            matches = [
                a for a in store
                if not a.installed and (needle in a.name.lower() or needle in (a.description or "").lower()
                                        or needle in a.slug.lower())
            ]
            out["store"] = [
                {"addon": a.slug, "name": a.name, "description": a.description, "available": bool(a.available)}
                for a in matches[:MAX_STORE_RESULTS]
            ]
    except Exception as err:
        raise _error(err) from err
    return out


async def async_get_settings(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    client = _client(hass)
    try:
        info = await client.addons.addon_info(slug)
    except Exception as err:
        raise _error(err, slug) from err
    secrets = _secret_names(info.schema)
    return {
        "addon": slug,
        "name": info.name,
        "description": info.description,
        "state": str(_value(info.state)),
        "version": info.version,
        "latest_version": info.version_latest,
        "start_on_boot": str(_value(info.boot)) == "auto",
        "watchdog": info.watchdog,
        "auto_update": info.auto_update,
        "options": _mask(info.options, secrets),
        "fields": _fields(info.schema, secrets),
        "configuration_page": config_url(slug),
    }


async def async_set_settings(
    hass: HomeAssistant,
    slug: str,
    options: dict[str, Any] | None = None,
    start_on_boot: bool | None = None,
    watchdog: bool | None = None,
    auto_update: bool | None = None,
) -> dict[str, Any]:
    from aiohasupervisor.models import AddonBoot, AddonsOptions

    client = _client(hass)
    try:
        info = await client.addons.addon_info(slug)
    except Exception as err:
        raise _error(err, slug) from err
    secrets = _secret_names(info.schema)
    names = {field.get("name") for field in info.schema or []}
    config: dict[str, Any] | None = None
    if options:
        refused = sorted(k for k in options if k in secrets or SECRET_NAME_RE.search(k))
        if refused:
            raise AddonError(
                f"{', '.join(refused)} is a password or key, so the person enters it on the add-on's "
                f"Configuration tab ({config_url(slug)}). Nothing was changed."
            )
        unknown = sorted(k for k in options if names and k not in names)
        if unknown:
            raise AddonError(
                f"{info.name} has no setting {', '.join(unknown)}. It takes: {', '.join(sorted(n for n in names if n))}."
            )
        config = copy.deepcopy(info.options)
        for key, value in options.items():
            # A nested group keeps the passwords inside it
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                if any(k in secrets or SECRET_NAME_RE.search(k) for k in value):
                    raise AddonError(f"Passwords and keys go on the add-on's Configuration tab ({config_url(slug)}).")
                config[key] = {**config[key], **value}
            else:
                config[key] = value
    changes: dict[str, Any] = {}
    if config is not None:
        changes["config"] = config
    if start_on_boot is not None:
        changes["boot"] = AddonBoot.AUTO if start_on_boot else AddonBoot.MANUAL
    if watchdog is not None:
        changes["watchdog"] = watchdog
    if auto_update is not None:
        changes["auto_update"] = auto_update
    if not changes:
        raise AddonError("Nothing to change: give options, start_on_boot, watchdog or auto_update.")
    try:
        await client.addons.set_addon_options(slug, AddonsOptions(**changes))
    except Exception as err:
        raise _error(err, slug) from err
    running = str(_value(info.state)) == "started"
    return {
        "addon": slug,
        "saved": True,
        "restart_needed": config is not None and running,
        "settings": await async_get_settings(hass, slug),
    }


async def async_action(hass: HomeAssistant, slug: str, action: str) -> dict[str, Any]:
    if action not in ACTIONS:
        raise AddonError(f"Use one of: {', '.join(ACTIONS)}.")
    client = _client(hass)
    call = {
        "install": client.store.install_addon,
        "update": client.store.update_addon,
        "start": client.addons.start_addon,
        "stop": client.addons.stop_addon,
        "restart": client.addons.restart_addon,
    }[action]
    try:
        await call(slug)
    except Exception as err:
        raise _error(err, slug) from err
    return {"addon": slug, "done": action}


def _secret_names(schema: list[dict[str, Any]] | None) -> set[str]:
    """Field names the add-on marks as passwords, at any depth."""
    out: set[str] = set()
    for field in schema or []:
        if field.get("format") == "password" or field.get("type") == "password":
            out.add(str(field.get("name")))
        nested = field.get("schema")
        if isinstance(nested, list):
            out |= _secret_names(nested)
    return out


def _is_secret(key: str, secrets: set[str]) -> bool:
    return key in secrets or bool(SECRET_NAME_RE.search(key))


def _mask(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            k: (SECRET_SHOWN if v not in (None, "") else SECRET_EMPTY) if _is_secret(k, secrets) else _mask(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask(v, secrets) for v in value]
    return value


def _fields(schema: list[dict[str, Any]] | None, secrets: set[str]) -> list[dict[str, Any]]:
    out = []
    for field in schema or []:
        name = str(field.get("name"))
        item = {k: v for k, v in field.items() if k in ("name", "type", "required", "optional", "options", "multiple")}
        if _is_secret(name, secrets):
            item["password"] = True
        if isinstance(field.get("schema"), list):
            item["fields"] = _fields(field["schema"], secrets)
        out.append(item)
    return out


def _value(enum_or_str: Any) -> Any:
    return getattr(enum_or_str, "value", enum_or_str)


def _error(err: Exception, slug: str | None = None) -> AddonError:
    if isinstance(err, AddonError):
        return err
    text = str(err) or type(err).__name__
    if slug and ("not" in text.lower() and ("found" in text.lower() or "exist" in text.lower())):
        return AddonError(f"There is no add-on '{slug}'. Use orchestra_list_addons to find its id.")
    return AddonError(f"The Supervisor refused that: {text}")
