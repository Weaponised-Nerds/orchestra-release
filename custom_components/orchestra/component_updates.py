"""
Keeps Overture up to date when nothing else does.

From Home Assistant 2026.11 the built-in Marketplace (which replaced HACS) updates
community integrations. An image or starter backup puts Overture in custom_components
directly, so the Marketplace doesn't know about it until someone adds it there. Until
then Overture fetches its own public release zip from GitHub, checks it, and swaps it
into custom_components. Once the Marketplace tracks Overture, Overture stands down so
the two never fight over the same folder. New files only take effect on a restart, which
the setup assistant asks the person about first.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
import shutil
from typing import Any
import zipfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = aiohttp.ClientTimeout(total=120)
MAX_ZIP_BYTES = 30 * 1024 * 1024

# Where each component's release lives, and where its files sit inside the release zip
COMPONENTS: dict[str, dict[str, str]] = {
    "orchestra": {
        # The source repo is private; releases are published to this public one
        # (haos_image/publish_release.py), which the Marketplace can also read
        "repo": "Weaponised-Nerds/overture",
        "zip_url": "https://github.com/Weaponised-Nerds/overture/archive/refs/tags/{tag}.zip",
        "zip_dir": "custom_components/orchestra/",
    },
}


# The Marketplace keys its update entity on the GitHub repository id: that of the public
# Weaponised-Nerds/overture. Entities for the private source repo (1422859678, left over
# from HACS) and for the retired Weaponised-Nerds/orchestra-release (1458342129, used up to
# 0.7.x) never see a new release, so they don't count.
ORCHESTRA_REPOSITORY_ID = "1468630872"
MARKETPLACE_DOMAIN = "marketplace"


class UpdateError(Exception):
    """Why an update couldn't be checked or installed, in plain words."""


def installed_version(hass: HomeAssistant, name: str) -> str | None:
    manifest = Path(hass.config.path("custom_components", name, "manifest.json"))
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version"))
    except (OSError, ValueError):
        return None


def is_running(hass: HomeAssistant, name: str) -> bool:
    """True when the component is loaded, so new files only take effect after a restart."""
    return True


def marketplace_entity_id(hass: HomeAssistant, name: str) -> str | None:
    """The Marketplace update entity for this component, when the Marketplace tracks it."""
    if name != "orchestra":
        return None
    registry = er.async_get(hass)
    return registry.async_get_entity_id("update", MARKETPLACE_DOMAIN, ORCHESTRA_REPOSITORY_ID)


async def async_check(hass: HomeAssistant, name: str) -> dict[str, Any]:
    if name not in COMPONENTS:
        raise UpdateError(f"Overture can only update {', '.join(COMPONENTS)}.")
    if entity_id := marketplace_entity_id(hass, name):
        installed = await hass.async_add_executor_job(installed_version, hass, name)
        return {
            "component": name,
            "installed": installed,
            "managed_by": "marketplace",
            "update_entity": entity_id,
            "message": "The Marketplace keeps this up to date: Settings > Updates.",
        }
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            GITHUB_API.format(repo=COMPONENTS[name]["repo"]),
            headers={"Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise UpdateError(f"GitHub didn't answer ({resp.status}). Try again later.")
            release = await resp.json()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise UpdateError("Couldn't reach GitHub. Check the internet connection.") from err
    latest = str(release.get("tag_name") or "")
    installed = await hass.async_add_executor_job(installed_version, hass, name)
    return {
        "component": name,
        "installed": installed,
        "latest": latest,
        "update_available": bool(latest) and _norm(latest) != _norm(installed or ""),
        "managed_by": "orchestra",
    }


async def async_install(hass: HomeAssistant, name: str, tag: str | None = None) -> dict[str, Any]:
    """Download and install a release. Returns whether a restart is needed."""
    if name not in COMPONENTS:
        raise UpdateError(f"Overture can only update {', '.join(COMPONENTS)}.")
    if marketplace_entity_id(hass, name):
        # Swapping files under the Marketplace would leave it with the wrong version on record
        raise UpdateError("The Marketplace keeps Overture up to date. Update it from Settings > Updates.")
    if tag is None:
        tag = (await async_check(hass, name))["latest"]
    spec = COMPONENTS[name]
    session = async_get_clientsession(hass)
    try:
        async with session.get(spec["zip_url"].format(tag=tag), timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise UpdateError(f"Couldn't download {name} {tag} ({resp.status}).")
            data = await resp.content.read(MAX_ZIP_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise UpdateError("Couldn't reach GitHub. Check the internet connection.") from err
    if len(data) > MAX_ZIP_BYTES:
        raise UpdateError("That download is too big to be a real release.")
    target = Path(hass.config.path("custom_components", name))
    version = await hass.async_add_executor_job(_swap_in, data, spec["zip_dir"], name, target)
    _LOGGER.info("Overture installed %s %s", name, version)
    return {"component": name, "installed": version, "restart_needed": is_running(hass, name)}


def _swap_in(data: bytes, zip_dir: str, name: str, target: Path) -> str:
    """Unpack next to the old copy, check it, then swap it in. The old copy is kept until then."""
    staging = target.with_name(f".{name}.new")
    backup = target.with_name(f".{name}.old")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.infolist():
                path = member.filename
                if zip_dir:
                    # GitHub source zips put everything under "<repo>-<tag>/"
                    _, _, path = path.partition("/")
                    if not path.startswith(zip_dir):
                        continue
                    path = path[len(zip_dir):]
                if not path or member.is_dir():
                    continue
                dest = (staging / path).resolve()
                if staging.resolve() not in dest.parents:
                    raise UpdateError(f"Unsafe path in the {name} download.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(member))
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    except (zipfile.BadZipFile, OSError, ValueError) as err:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"The {name} download wasn't a valid release.") from err
    if manifest.get("domain") != name:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"The download isn't {name}.")
    shutil.rmtree(backup, ignore_errors=True)
    if target.exists():
        target.rename(backup)
    staging.rename(target)
    shutil.rmtree(backup, ignore_errors=True)
    return str(manifest.get("version"))


def _norm(version: str) -> str:
    return version.strip().lstrip("vV")
