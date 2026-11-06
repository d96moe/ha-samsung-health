"""Problem sensor: the phone has stopped syncing Samsung Health to Samsung Cloud."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import STALE_AFTER
from .coordinator import SamsungHealthConfigEntry, SamsungHealthCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamsungHealthConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CloudSyncStaleSensor(entry.runtime_data)])


class CloudSyncStaleSensor(CoordinatorEntity[SamsungHealthCoordinator], BinarySensorEntity):
    """On when the newest record in Samsung Cloud is older than STALE_AFTER."""

    _attr_has_entity_name = True
    _attr_translation_key = "cloud_sync_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SamsungHealthCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_cloud_sync_stale"
        self._attr_device_info = device_info(coordinator.config_entry)

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or data.last_cloud_update is None:
            return None
        return dt_util.utcnow() - data.last_cloud_update > STALE_AFTER
