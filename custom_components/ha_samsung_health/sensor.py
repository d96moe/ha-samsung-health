"""Sensors.

Measurements (body, sleep, recovery) have state_class measurement: their full
history is imported onto the entity's own statistic, so the History panel shows it.
Daily totals (steps, calories, active time, distance) reset at midnight and have no
state_class: their history is in external statistics (see statistics.py)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import parsers
from .const import DOMAIN
from .coordinator import SamsungHealthConfigEntry, SamsungHealthCoordinator, SamsungHealthData
from .statistics import ENTITY_KEYS

KCAL = "kcal"


def day_fraction(now: datetime) -> float:
    """Share of today's local day that has passed (a DST day has 23 or 25 hours).

    In UTC: subtracting datetimes that share a tzinfo is wall-clock arithmetic,
    which would make every day 24 hours long.
    """
    local_day = dt_util.as_local(now).date()
    start = dt_util.as_utc(dt_util.start_of_local_day(local_day))
    end = dt_util.as_utc(dt_util.start_of_local_day(local_day + timedelta(days=1)))
    return (dt_util.as_utc(now) - start) / (end - start)


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
        value_fn=lambda d, n: (a.exercise_kcal if (a := _today(d, n)) else None), attrs_fn=_today_attr,
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
        native_unit_of_measurement=UnitOfTime.HOURS, suggested_display_precision=1,
        value_fn=lambda d, n: (s.duration_min / 60 if (s := _last_sleep(d)) and s.duration_min is not None else None),
        attrs_fn=_sleep_attr,
    ),
    SamsungSensorDescription(
        key="time_in_bed", translation_key="time_in_bed", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS, suggested_display_precision=1,
        value_fn=lambda d, n: (s.time_in_bed_min / 60 if (s := _last_sleep(d)) and s.time_in_bed_min is not None else None),
        attrs_fn=_sleep_attr,
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


class SamsungHealthSensor(CoordinatorEntity[SamsungHealthCoordinator], RestoreSensor):
    """Until the first download after a restart finishes (it runs in the background,
    see __init__.py), the sensor shows its value from before the restart."""

    _attr_has_entity_name = True
    entity_description: SamsungSensorDescription

    def __init__(self, coordinator: SamsungHealthCoordinator, description: SamsungSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        if description.key in ENTITY_KEYS:
            # History is imported onto this entity's statistic (statistics.py).
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif description.attrs_fn is _today_attr:
            # A daily total that starts over at midnight, like a "rain today" sensor:
            # the recorder keeps its statistics from now on. Nothing is imported onto
            # it; the full history lives in the ha_samsung_health:<key> statistic.
            self._attr_state_class = SensorStateClass.TOTAL
        self._daily_total = description.key not in ENTITY_KEYS and description.attrs_fn is _today_attr
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(coordinator.config_entry)
        self._restored_value: Any = None
        self._restored_attrs: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_description.attrs_fn is _today_attr:
            # "Today" values start over at local midnight, not at the next poll.
            self.async_on_remove(
                async_track_time_change(self.hass, self._async_new_day, hour=0, minute=0, second=0)
            )
        if self.coordinator.data is not None:
            return
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is None or last_data is None:
            return
        attrs = {k: v for k, v in last_state.attributes.items() if k in ("date", "measured_at")}
        if self.entity_description.attrs_fn is _today_attr and attrs.get("date") != dt_util.now().date().isoformat():
            return  # "today" values from an earlier day would be wrong
        self._restored_value = last_data.native_value
        self._restored_attrs = attrs or None

    @property
    def last_reset(self) -> datetime | None:
        if self._daily_total:
            return dt_util.start_of_local_day()
        return None

    @callback
    def _async_new_day(self, _now: datetime) -> None:
        self._restored_value = self._restored_attrs = None  # yesterday's, if restored
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.data, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return self._restored_attrs
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, dt_util.utcnow())
