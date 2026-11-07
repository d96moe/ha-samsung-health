"""Sync now: download from Samsung Cloud right away instead of waiting for the next poll."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SamsungHealthConfigEntry, SamsungHealthCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamsungHealthConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([SyncNowButton(entry.runtime_data)])


class SyncNowButton(CoordinatorEntity[SamsungHealthCoordinator], ButtonEntity):
    """Starts a download in the background; pressing it again meanwhile does nothing.

    Data is only as fresh as the phone's own sync to Samsung Cloud: to get the very
    latest, sync in the Samsung Health app first (Settings > Sync with Samsung
    Cloud), then press this.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "sync_now"

    def __init__(self, coordinator: SamsungHealthCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_sync_now"
        self._attr_device_info = device_info(coordinator.config_entry)

    @property
    def available(self) -> bool:
        # Pressable even after a failed poll: that is when you want to retry.
        return True

    async def async_press(self) -> None:
        self.coordinator.async_sync_now()
