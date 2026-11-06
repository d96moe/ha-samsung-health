"""Sensors. No state_class on purpose: long-term history lives in external
statistics (see statistics.py), which also covers the years before setup."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import parsers
from .const import DOMAIN
from .coordinator import SamsungHealthConfigEntry, SamsungHealthCoordinator, SamsungHealthData

KCAL = "kcal"


def day_fraction(now: datetime) -> float:
    local = dt_util.as_local(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (local - midnight).total_seconds() / 86400


@dataclass(frozen=True, kw_only=True)
class SamsungSensorDescription(SensorEntityDescription):
    value_fn: Callable[[SamsungHealthData, datetime], Any]
    attrs_fn: Callable[[SamsungHealthData, datetime], dict[str, Any]] | None = None


def _today(data: SamsungHealthData, now: datetime) -> parsers.DailyActivity | None:
    return data.days.get(dt_util.as_local(now).date())


def _today_attr(data: SamsungHealthData, now: datetime) -> dict[str, Any]:
    return {"date": dt_util.as_local(now).date().isoformat()}


def _latest_body(data: SamsungHealthData) -> parsers.BodyMeasurement | None:
    return data.body[-1] if data.body else None


def _body(get: Callable[[parsers.BodyMeasurement], float | None]):
    def value(data: SamsungHealthData, now: datetime):
        m = _latest_body(data)
        return get(m) if m else None
    return value


def _body_attr(data: SamsungHealthData, now: datetime) -> dict[str, Any]:
    m = _latest_body(data)
    return {"measured_at": m.measured_at.isoformat()} if m else {}


def _last_sleep(data: SamsungHealthData) -> parsers.SleepSession | None:
    return data.sleep[-1] if data.sleep else None


def _latest_recovery(data: SamsungHealthData) -> parsers.RecoveryDay | None:
    return data.recovery[max(data.recovery)] if data.recovery else None


def _recovery(get: Callable[[parsers.RecoveryDay], float | None]):
    def value(data: SamsungHealthData, now: datetime):
        r = _latest_recovery(data)
        return get(r) if r else None
    return value


def _recovery_attr(data: SamsungHealthData, now: datetime) -> dict[str, Any]:
    r = _latest_recovery(data)
    return {"date": r.day.isoformat()} if r else {}


def _sleep_attr(data: SamsungHealthData, now: datetime) -> dict[str, Any]:
    s = _last_sleep(data)
    return {"date": s.day.isoformat()} if s else {}


def _total_today(data: SamsungHealthData, now: datetime) -> float | None:
    a = _today(data, now)
    today: date = dt_util.as_local(now).date()
    return parsers.estimate_total_kcal(
        a.active_kcal if a else 0.0, parsers.bmr_for_day(data.body, today), day_fraction(now)
    )


SENSORS: tuple[SamsungSensorDescription, ...] = (
    # Activity, today
    SamsungSensorDescription(
        key="steps", translation_key="steps", native_unit_of_measurement="steps",
        icon="mdi:shoe-print",
        value_fn=lambda d, n: (a.steps if (a := _today(d, n)) else None), attrs_fn=_today_attr,
    ),
    SamsungSensorDescription(
        key="active_calories", translation_key="active_calories", native_unit_of_measurement=KCAL,
        icon="mdi:fire", suggested_display_precision=0,
        value_fn=lambda d, n: (a.active_kcal if (a := _today(d, n)) else None), attrs_fn=_today_attr,
    ),
    SamsungSensorDescription(
        key="total_calories", translation_key="total_calories", native_unit_of_measurement=KCAL,
        icon="mdi:fire-circle", suggested_display_precision=0,
        value_fn=_total_today, attrs_fn=_today_attr,
    ),
    SamsungSensorDescription(
        key="exercise_calories", translation_key="exercise_calories", native_unit_of_measurement=KCAL,
        icon="mdi:dumbbell", suggested_display_precision=0,
        value_fn=lambda d, n: (a.exercise_kcal if (a := _today(d, n)) else 0.0), attrs_fn=_today_attr,
    ),
    SamsungSensorDescription(
        key="active_time", translation_key="active_time", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES, suggested_display_precision=0,
        value_fn=lambda d, n: (a.active_time_min if (a := _today(d, n)) else None), attrs_fn=_today_attr,
    ),
    SamsungSensorDescription(
        key="distance", translation_key="distance", device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS, suggested_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2,
        value_fn=lambda d, n: (a.distance_m if (a := _today(d, n)) else None), attrs_fn=_today_attr,
    ),
    # Body, latest weighing
    SamsungSensorDescription(
        key="weight", translation_key="weight", device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS, suggested_display_precision=1,
        value_fn=_body(lambda m: m.weight), attrs_fn=_body_attr,
    ),
    SamsungSensorDescription(
        key="body_fat", translation_key="body_fat", native_unit_of_measurement=PERCENTAGE,
        icon="mdi:percent", suggested_display_precision=1,
        value_fn=_body(lambda m: m.body_fat), attrs_fn=_body_attr,
    ),
    SamsungSensorDescription(
        key="body_fat_mass", translation_key="body_fat_mass", device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS, suggested_display_precision=1,
        value_fn=_body(lambda m: m.body_fat_mass), attrs_fn=_body_attr,
    ),
    SamsungSensorDescription(
        key="skeletal_muscle_mass", translation_key="skeletal_muscle_mass",
        device_class=SensorDeviceClass.WEIGHT, native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value_fn=_body(lambda m: m.skeletal_muscle_mass), attrs_fn=_body_attr,
    ),
    SamsungSensorDescription(
        key="body_water", translation_key="body_water", device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS, suggested_display_precision=1,
        value_fn=_body(lambda m: m.total_body_water), attrs_fn=_body_attr,
    ),
    SamsungSensorDescription(
        key="bmr", translation_key="bmr", native_unit_of_measurement=KCAL, icon="mdi:fire-alert",
        suggested_display_precision=0,
        value_fn=_body(lambda m: m.bmr), attrs_fn=_body_attr,
    ),
    # Sleep, last night
    SamsungSensorDescription(
        key="sleep_duration", translation_key="sleep_duration", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES, suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda d, n: (s.duration_min if (s := _last_sleep(d)) else None), attrs_fn=_sleep_attr,
    ),
    SamsungSensorDescription(
        key="sleep_score", translation_key="sleep_score", icon="mdi:sleep",
        value_fn=lambda d, n: (s.score if (s := _last_sleep(d)) else None), attrs_fn=_sleep_attr,
    ),
    SamsungSensorDescription(
        key="bedtime", translation_key="bedtime", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d, n: (s.start if (s := _last_sleep(d)) else None), attrs_fn=_sleep_attr,
    ),
    SamsungSensorDescription(
        key="wake_time", translation_key="wake_time", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d, n: (s.end if (s := _last_sleep(d)) else None), attrs_fn=_sleep_attr,
    ),
    # Recovery, latest day
    SamsungSensorDescription(
        key="sleeping_heart_rate", translation_key="sleeping_heart_rate", native_unit_of_measurement="bpm",
        icon="mdi:heart-pulse", suggested_display_precision=0,
        value_fn=_recovery(lambda r: r.sleeping_hr), attrs_fn=_recovery_attr,
    ),
    SamsungSensorDescription(
        key="sleeping_hrv", translation_key="sleeping_hrv", native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        icon="mdi:heart-flash", suggested_display_precision=0,
        value_fn=_recovery(lambda r: r.sleeping_hrv), attrs_fn=_recovery_attr,
    ),
    SamsungSensorDescription(
        key="energy_score", translation_key="energy_score", icon="mdi:lightning-bolt",
        suggested_display_precision=0,
        value_fn=_recovery(lambda r: r.energy_score), attrs_fn=_recovery_attr,
    ),
    # Status
    SamsungSensorDescription(
        key="last_cloud_sync", translation_key="last_cloud_sync", device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, n: d.last_cloud_update,
    ),
)


def device_info(entry: SamsungHealthConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Samsung Health",
        manufacturer="Samsung",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamsungHealthConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(SamsungHealthSensor(coordinator, description) for description in SENSORS)


class SamsungHealthSensor(CoordinatorEntity[SamsungHealthCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: SamsungSensorDescription

    def __init__(self, coordinator: SamsungHealthCoordinator, description: SamsungSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(coordinator.config_entry)

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, dt_util.utcnow())
