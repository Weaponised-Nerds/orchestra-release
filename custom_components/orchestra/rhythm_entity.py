"""Base for the rhythm engine's entities (they follow the engine, not the Conductor coordinator)."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class RhythmEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, engine: Any, key: str, entity_id: str) -> None:
        self.engine = engine
        self._attr_unique_id = f"{DOMAIN}_rhythm_{key}"
        self.entity_id = entity_id

    @property
    def device_info(self) -> Any:
        return {"identifiers": {(DOMAIN, "orchestra_gateway")}, "name": "Orchestra Gateway"}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.engine.async_add_listener(self.async_write_ha_state))
