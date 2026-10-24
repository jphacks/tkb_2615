"""Typed, source-attributed operator conditions compiled to existing scenario contracts."""

from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import Point2D
from sheltersim.domain.infrastructure import dependent_facilities
from sheltersim.experiments.scenario import (
    RESOURCE_SERVICE_POLICY_ID,
    InitialStockSpec,
    PlannedEventSpec,
    ResourceServiceConfig,
    ScenarioInfrastructureSpec,
)
from sheltersim.region.models import RegionModel

OPERATOR_CONDITION_SOURCE_ID = "SRC-OPERATOR-INITIAL-CONDITIONS"
RESOURCE_SERVICE_ASSUMPTION_ID = "ASM-REGION-RESOURCE-SERVICE-V1"
INFRASTRUCTURE_ASSUMPTION_ID = "ASM-REGION-INFRASTRUCTURE-V1"


def default_resource_service_config() -> ResourceServiceConfig:
    """Return the water and meal service coefficients generated Scenarios use."""
    return ResourceServiceConfig(
        schema_version=1,
        policy_version=RESOURCE_SERVICE_POLICY_ID,
        source_id=RESOURCE_SERVICE_ASSUMPTION_ID,
        source_type="assumption",
        service_minutes=5,
        water_ml_per_service=250,
        food_meal_units_per_service=1,
        hydration_growth_per_minute=0.001,
    )


class InitialResourceCondition(RegionModel):
    """Known inventory is distinct from an unknown inventory; partial quantities are rejected."""

    status: Literal["unknown", "incomplete", "entered"] = "unknown"
    water_ml: int | None = Field(default=None, ge=0)
    food_meal_units: int | None = Field(default=None, ge=0)
    warehouse_id: str = "main_warehouse"
    zone_id: str | None = None
    source_ids: tuple[str, ...] = ()
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"

    @model_validator(mode="after")
    def inventory_status_is_explicit(self) -> InitialResourceCondition:
        quantities = (self.water_ml, self.food_meal_units)
        if self.status == "unknown" and any(value is not None for value in quantities):
            raise ValueError("unknown initial inventory cannot contain quantities")
        if self.status == "entered" and any(value is None for value in quantities):
            raise ValueError("entered initial inventory requires both water and food quantities")
        if self.status == "incomplete" and all(value is None for value in quantities):
            raise ValueError("incomplete initial inventory must preserve at least one entered quantity")
        if self.status == "entered" and not self.source_ids:
            object.__setattr__(self, "source_ids", (OPERATOR_CONDITION_SOURCE_ID,))
        if self.status == "incomplete" and any(value is not None for value in quantities) and not self.source_ids:
            object.__setattr__(self, "source_ids", (OPERATOR_CONDITION_SOURCE_ID,))
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("initial inventory source IDs must be unique")
        return self


class FacilityEquipmentCondition(RegionModel):
    facility_id: str
    capacity: int = Field(ge=0)
    working_units: int | None = Field(default=None, ge=0)
    facility_kind: str | None = None
    zone_id: str | None = None
    add: bool = False
    position_m: Point2D | None = None
    footprint_m2: float | None = Field(default=None, gt=0, le=10_000, allow_inf_nan=False)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"

    @model_validator(mode="after")
    def working_units_fit(self) -> FacilityEquipmentCondition:
        if self.working_units is not None and self.working_units > self.capacity:
            raise ValueError("equipment working_units cannot exceed capacity")
        if self.add and (not self.facility_kind or not self.zone_id):
            raise ValueError("new equipment requires facility_kind and zone_id")
        if not self.add and (self.facility_kind is not None or self.zone_id is not None):
            raise ValueError("facility_kind and zone_id are only accepted when adding new equipment")
        return self


class FacilityOutageCondition(RegionModel):
    kind: Literal["facility_outage"]
    facility_id: str
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    units_out: int = Field(gt=0)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered_window(self) -> FacilityOutageCondition:
        if self.end_minute <= self.start_minute:
            raise ValueError("facility outage end_minute must follow start_minute")
        return self


class StaffUnavailableCondition(RegionModel):
    kind: Literal["staff_unavailable"]
    staff_id: str
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered_window(self) -> StaffUnavailableCondition:
        if self.end_minute <= self.start_minute:
            raise ValueError("staff unavailability end_minute must follow start_minute")
        return self


class TemperatureCondition(RegionModel):
    kind: Literal["extreme_heat", "extreme_cold"]
    zone_id: str
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered_window(self) -> TemperatureCondition:
        if self.end_minute <= self.start_minute:
            raise ValueError("temperature event end_minute must follow start_minute")
        return self


class WaterOutageCondition(RegionModel):
    kind: Literal["water_outage"]
    facility_id: str
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered_window(self) -> WaterOutageCondition:
        if self.end_minute <= self.start_minute:
            raise ValueError("water outage end_minute must follow start_minute")
        return self


class PowerOutageCondition(RegionModel):
    kind: Literal["power_outage"]
    component_id: str
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered_window(self) -> PowerOutageCondition:
        if self.end_minute <= self.start_minute:
            raise ValueError("power outage end_minute must follow start_minute")
        return self


class EquipmentFailureCondition(RegionModel):
    kind: Literal["equipment_failure"]
    facility_id: str
    units_out: int = Field(gt=0)
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered_window(self) -> EquipmentFailureCondition:
        if self.end_minute <= self.start_minute:
            raise ValueError("equipment failure end_minute must follow start_minute")
        return self


class ResourceQuantityCondition(RegionModel):
    kind: Literal["resource_delivery", "resource_loss"]
    resource_id: Literal["water_ml", "food_meal_units"]
    quantity: int = Field(gt=0)
    due_minute: int = Field(ge=0)
    warehouse_id: str = "main_warehouse"
    source_id: str = OPERATOR_CONDITION_SOURCE_ID
    source_kind: Literal["official", "municipal", "operator", "assumption", "inferred"] = "operator"
    repeat_every_minutes: int | None = Field(default=None, gt=0)


ScheduledCondition = Annotated[
    FacilityOutageCondition
    | StaffUnavailableCondition
    | TemperatureCondition
    | WaterOutageCondition
    | PowerOutageCondition
    | EquipmentFailureCondition
    | ResourceQuantityCondition,
    Field(discriminator="kind"),
]


class StaffShiftInput(RegionModel):
    """Operator-declared positive weekly availability, distinct from absence events."""
    staff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    start_minute: int = Field(strict=True, ge=0, lt=10080)
    end_minute: int = Field(strict=True, gt=0, le=10080)
    source_id: str = Field(default=OPERATOR_CONDITION_SOURCE_ID, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    source_kind: Literal["operator"] = "operator"

    @model_validator(mode="after")
    def ordered(self) -> StaffShiftInput:
        if self.start_minute >= self.end_minute:
            raise ValueError("invalid_staff_shift_interval")
        return self


class OperationalConditions(RegionModel):
    initial_resources: InitialResourceCondition = Field(default_factory=InitialResourceCondition)
    equipment: tuple[FacilityEquipmentCondition, ...] = ()
    scheduled: tuple[ScheduledCondition, ...] = ()
    staff_shifts: tuple[StaffShiftInput, ...] = Field(default=(), max_length=2100)
    infrastructure: ScenarioInfrastructureSpec | None = None
    source_geographic_scope: str | None = None
    source_period: str | None = None

    @model_validator(mode="after")
    def unique_targets(self) -> OperationalConditions:
        equipment_ids = [item.facility_id for item in self.equipment]
        if len(equipment_ids) != len(set(equipment_ids)):
            raise ValueError("equipment overrides must have unique facility IDs")
        if len(self.scheduled) > 128:
            raise ValueError("at most 128 scheduled region conditions are supported")
        source_kinds: dict[str, str] = {}
        entries = [
            *((source_id, self.initial_resources.source_kind) for source_id in self.initial_resources.source_ids),
            *((item.source_id, item.source_kind) for item in self.equipment),
            *((item.source_id, item.source_kind) for item in self.scheduled),
            *((item.source_id, item.source_kind) for item in self.staff_shifts),
        ]
        if self.infrastructure is not None:
            entries.append((self.infrastructure.source_id, "assumption"))
        for source_id, source_kind in entries:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", source_id):
                raise ValueError("condition source IDs must be stable identifiers")
            previous = source_kinds.setdefault(source_id, source_kind)
            if previous != source_kind:
                raise ValueError(f"condition source {source_id!r} has conflicting source kinds")
        return self

    def referenced_source_ids(self) -> tuple[str, ...]:
        values = set(self.initial_resources.source_ids)
        values.update(item.source_id for item in self.equipment)
        values.update(item.source_id for item in self.scheduled)
        if self.infrastructure is not None:
            values.add(self.infrastructure.source_id)
        return tuple(sorted(values))

    def compile(
        self,
        *,
        duration_minutes: int,
        zone_ids: set[str],
        facilities: tuple[object, ...],
        staff: tuple[object, ...],
    ) -> tuple[InitialStockSpec | None, tuple[PlannedEventSpec, ...], dict[str, dict[str, object]], tuple[str, ...]]:
        """Compile supported conditions only; unsupported or unknown values stay visible as blockers."""
        facility_by_id = {item.id: item for item in facilities}
        staff_ids = {item.id for item in staff}
        blockers: list[str] = []
        for item in self.equipment:
            if item.facility_id not in facility_by_id:
                blockers.append(f"設備条件が未知の施設ID {item.facility_id!r} を参照しています。")
        stock = self.initial_resources
        if stock.status == "incomplete":
            blockers.append("初期在庫の水・食料の両方を明示してください。不明を0には変換していません。")
        invalid_stock_zone = (
            stock.status == "entered"
            and stock.zone_id is not None
            and stock.zone_id not in zone_ids
        )
        if invalid_stock_zone:
            blockers.append("初期在庫の保管ゾーンが施設Geometryにありません。")
        initial_stock = None
        if stock.status == "entered" and not invalid_stock_zone:
            initial_stock = InitialStockSpec(
                schema_version=1,
                warehouse_id=stock.warehouse_id,
                zone_id=stock.zone_id,
                water_ml=stock.water_ml or 0,
                food_meal_units=stock.food_meal_units or 0,
                source_ids=stock.source_ids,
            )
        events: list[PlannedEventSpec] = []
        needs_resource_service = stock.status == "entered"
        needs_infrastructure = False
        infrastructure_config = self.infrastructure
        facility_outage_windows: dict[str, list[tuple[int, int, int]]] = {}
        staff_unavailable_windows: dict[str, list[tuple[int, int]]] = {}
        environmental_stressor_windows: dict[str, list[tuple[int, int]]] = {}

        def add_event(
            event_type: str,
            due_minute: int,
            payload: dict[str, object],
            source_id: str,
            source_kind: str,
            sequence: int,
            input_index: int,
            causation_id: str | None = None,
        ) -> None:
            if due_minute < 0 or due_minute >= duration_minutes:
                blockers.append(f"運営条件 {event_type} の開始時刻はシミュレーション期間内にしてください。")
                return
            events.append(
                PlannedEventSpec(
                    id=f"region-condition-{sequence:03d}-{event_type}",
                    due_minute=due_minute,
                    sequence=sequence,
                    event_type=event_type,
                    payload=payload,
                    source_id=source_id,
                    causation_id=causation_id,
                    provenance={
                        "input_path": f"operational_conditions.scheduled[{input_index}]",
                        "source_type": source_kind,
                        "units": "simulation_minutes",
                        "geographic_scope": self.source_geographic_scope,
                        "observation_period": self.source_period,
                    },
                )
            )

        sequence = 0
        for index, condition in enumerate(self.scheduled):
            configured_start = getattr(condition, "start_minute", getattr(condition, "due_minute", 0))
            if configured_start >= duration_minutes:
                blockers.append("運営条件の開始時刻はシミュレーション期間内にしてください。")
                continue
            if condition.kind == "facility_outage":
                facility = facility_by_id.get(condition.facility_id)
                working_capacity = facility.capacity if facility is not None and facility.working_units is None else (facility.working_units or 0 if facility is not None else 0)
                if facility is None or condition.units_out > working_capacity:
                    blockers.append(f"設備停止条件が未知の設備または過剰な停止数を参照しています: {condition.facility_id}.")
                    continue
                for occurrence, (start_minute, end_minute) in enumerate(_repeating_windows(condition, duration_minutes)):
                    if end_minute >= duration_minutes:
                        blockers.append("設備停止条件の復旧時刻はシミュレーション終了前にしてください。")
                        continue
                    concurrent_units = sum(
                        units
                        for existing_start, existing_end, units in facility_outage_windows.get(condition.facility_id, ())
                        if start_minute < existing_end and end_minute > existing_start
                    )
                    if concurrent_units + condition.units_out > working_capacity:
                        blockers.append(
                            f"設備停止条件が重なり、施設 {condition.facility_id!r} の稼働可能数を超えます。"
                        )
                        continue
                    facility_outage_windows.setdefault(condition.facility_id, []).append(
                        (start_minute, end_minute, condition.units_out)
                    )
                    sequence += 1
                    add_event("facility_outage", start_minute, {"facility_id": condition.facility_id, "units_out": condition.units_out}, condition.source_id, condition.source_kind, sequence, index)
                    sequence += 1
                    add_event("facility_recovery", end_minute, {"facility_id": condition.facility_id, "units_restored": condition.units_out}, condition.source_id, condition.source_kind, sequence, index)
            elif condition.kind == "staff_unavailable":
                if condition.staff_id not in staff_ids:
                    blockers.append(f"運営条件が未知の職員IDを参照しています: {condition.staff_id}.")
                    continue
                for _, (start_minute, end_minute) in enumerate(_repeating_windows(condition, duration_minutes)):
                    if end_minute >= duration_minutes:
                        blockers.append("職員不在条件の復帰時刻はシミュレーション終了前にしてください。")
                        continue
                    if any(
                        start_minute < existing_end and end_minute > existing_start
                        for existing_start, existing_end in staff_unavailable_windows.get(condition.staff_id, ())
                    ):
                        blockers.append(
                            f"職員不在条件が重なり、復帰時刻を正しく定義できません: {condition.staff_id}."
                        )
                        continue
                    staff_unavailable_windows.setdefault(condition.staff_id, []).append(
                        (start_minute, end_minute)
                    )
                    sequence += 1
                    add_event("staff_unavailable", start_minute, {"staff_id": condition.staff_id, "until_minute": end_minute}, condition.source_id, condition.source_kind, sequence, index)
            elif condition.kind in {"extreme_heat", "extreme_cold"}:
                if condition.zone_id not in zone_ids:
                    blockers.append("気温影響条件が未知のゾーンを参照しています。")
                    continue
                for occurrence, (start_minute, end_minute) in enumerate(_repeating_windows(condition, duration_minutes)):
                    if end_minute >= duration_minutes:
                        blockers.append("気温影響条件はシミュレーション終了前に終了する時間帯を指定してください。")
                        continue
                    active_count = sum(
                        1
                        for existing_start, existing_end in environmental_stressor_windows.get(condition.zone_id, ())
                        if existing_start <= start_minute < existing_end
                    )
                    if active_count >= 16:
                        blockers.append("同一ゾーンの同時気温影響条件は16件以下にしてください。")
                        continue
                    environmental_stressor_windows.setdefault(condition.zone_id, []).append(
                        (start_minute, end_minute)
                    )
                    needs_infrastructure = True
                    sequence += 1
                    add_event(condition.kind, start_minute, {"stressor_id": f"region-stressor-{index:03d}-{occurrence:03d}", "zone_id": condition.zone_id, "until_minute": end_minute}, condition.source_id, condition.source_kind, sequence, index)
            elif condition.kind == "water_outage":
                facility = facility_by_id.get(condition.facility_id)
                if facility is None or facility.kind != "water_station":
                    blockers.append("断水条件は既知の給水設備を指定してください。")
                    continue
                if initial_stock is None:
                    blockers.append("断水条件をシミュレーションへ反映するには、水・食料の入力済み在庫が必要です。")
                    continue
                if not any(item.kind == "meal_distribution" for item in facilities):
                    blockers.append("断水条件をシミュレーションへ反映するには食事配布設備も必要です。")
                    continue
                needs_resource_service = True
                for occurrence, (start_minute, end_minute) in enumerate(_repeating_windows(condition, duration_minutes)):
                    if end_minute >= duration_minutes:
                        blockers.append("断水条件の復旧時刻はシミュレーション終了前にしてください。")
                        continue
                    sequence += 1
                    outage_id = f"region-water-outage-{index:03d}-{occurrence:03d}"
                    outage_event_id = f"region-condition-{sequence:03d}-water_outage"
                    add_event("water_outage", start_minute, {"outage_id": outage_id, "facility_id": condition.facility_id}, condition.source_id, condition.source_kind, sequence, index)
                    sequence += 1
                    # ScenarioSpec validation requires a restoration to name its outage.
                    add_event("water_restored", end_minute, {"outage_id": outage_id}, condition.source_id, condition.source_kind, sequence, index, causation_id=outage_event_id)
            elif condition.kind == "power_outage":
                if infrastructure_config is None:
                    blockers.append("停電条件には依存関係を含むinfrastructure設定が必要です。施設への影響を推測していません。")
                    continue
                affected = dependent_facilities(
                    infrastructure_config.model_dump(mode="json"),
                    condition.component_id,
                    set(facility_by_id),
                )
                if not affected:
                    blockers.append(f"停電条件の設備系統 {condition.component_id!r} は有効な施設依存関係を持ちません。")
                    continue
                for occurrence, (start_minute, end_minute) in enumerate(_repeating_windows(condition, duration_minutes)):
                    if end_minute >= duration_minutes:
                        blockers.append("停電条件はシミュレーション終了前に復旧する時刻を指定してください。")
                        continue
                    sequence += 1
                    add_event(
                        "power_outage",
                        start_minute,
                        {"outage_id": f"region-power-outage-{index:03d}-{occurrence:03d}", "component_id": condition.component_id, "until_minute": end_minute},
                        condition.source_id,
                        condition.source_kind,
                        sequence,
                        index,
                    )
                    needs_infrastructure = True
            elif condition.kind == "equipment_failure":
                facility = facility_by_id.get(condition.facility_id)
                working_capacity = (
                    facility.capacity if facility is not None and facility.working_units is None
                    else facility.working_units if facility is not None and facility.working_units is not None
                    else 0
                )
                if facility is None or condition.units_out > working_capacity:
                    blockers.append(f"故障条件が未知の設備または過剰な停止数を参照しています: {condition.facility_id}.")
                    continue
                for occurrence, (start_minute, end_minute) in enumerate(_repeating_windows(condition, duration_minutes)):
                    if end_minute >= duration_minutes:
                        blockers.append("設備故障条件はシミュレーション終了前に復旧する時刻を指定してください。")
                        continue
                    sequence += 1
                    add_event(
                        "equipment_failure",
                        start_minute,
                        {"outage_id": f"region-equipment-failure-{index:03d}-{occurrence:03d}", "facility_id": condition.facility_id, "units_out": condition.units_out, "until_minute": end_minute},
                        condition.source_id,
                        condition.source_kind,
                        sequence,
                        index,
                    )
                    needs_infrastructure = True
            elif condition.kind in {"resource_delivery", "resource_loss"}:
                if initial_stock is None or condition.warehouse_id != initial_stock.warehouse_id:
                    blockers.append("資源の納入・損失イベントには同じ倉庫の入力済み初期在庫が必要です。")
                    continue
                for due_minute in _repeating_minutes(condition.due_minute, condition.repeat_every_minutes, duration_minutes):
                    sequence += 1
                    payload = {
                        "warehouse_id": condition.warehouse_id,
                        "resource_id": condition.resource_id,
                        "quantity": condition.quantity,
                    }
                    if condition.kind == "resource_delivery":
                        # A shipment ID opts the delivery into the operational-event state;
                        # without it the first state validation stopped the Run.
                        payload["shipment_id"] = f"region-shipment-{sequence:03d}"
                    add_event(
                        condition.kind, due_minute, payload, condition.source_id,
                        condition.source_kind, sequence, index,
                    )

        model_configs: dict[str, dict[str, object]] = {}
        if needs_resource_service:
            has_water = any(item.kind == "water_station" for item in facilities)
            has_food = any(item.kind == "meal_distribution" for item in facilities)
            if initial_stock is None:
                blockers.append("資源サービス条件には入力済み初期在庫が必要です。")
            elif not has_water or not has_food:
                blockers.append("資源をシミュレーションで配布するには給水・食事配布設備の両方が必要です。")
            else:
                model_configs["resource_service"] = default_resource_service_config().model_dump(
                    mode="json"
                )
        if needs_infrastructure:
            model_configs["infrastructure"] = (
                infrastructure_config
                or ScenarioInfrastructureSpec(
                    schema_version=1,
                    policy_version="infrastructure-events-v1",
                    source_type="assumption",
                    source_id=INFRASTRUCTURE_ASSUMPTION_ID,
                )
            ).model_dump(mode="json")
        if len(events) > 128:
            blockers.append("繰り返しを含むスケジュールは128イベント以下にしてください。")
            events = []
        return initial_stock, tuple(events), model_configs, tuple(blockers)

    def provenance_sources(self) -> tuple[dict[str, object], ...]:
        kinds = {source_id: self.initial_resources.source_kind for source_id in self.initial_resources.source_ids}
        kinds.update({item.source_id: item.source_kind for item in self.equipment})
        kinds.update({item.source_id: item.source_kind for item in self.scheduled})
        kinds.update({item.source_id: item.source_kind for item in self.staff_shifts})
        if self.infrastructure is not None:
            kinds[self.infrastructure.source_id] = "assumption"
        return tuple(
            {
                "id": source_id,
                "title": "Operator-entered initial shelter conditions",
                "source_kind": kinds[source_id],
                "description": (
                    "Scenario operator input; not an independently verified official observation."
                    if kinds[source_id] == "operator"
                    else "Scenario assumption or inference explicitly supplied as an input."
                ),
                "geographic_scope": self.source_geographic_scope,
                "observation_period": self.source_period,
            }
            for source_id in sorted(kinds)
            if kinds[source_id] in {"operator", "assumption", "inferred"}
        )


def _repeating_windows(condition: object, duration_minutes: int) -> tuple[tuple[int, int], ...]:
    start = condition.start_minute
    end = condition.end_minute
    interval = condition.repeat_every_minutes
    windows = []
    offset = 0
    while start + offset < duration_minutes and len(windows) < 129:
        windows.append((start + offset, end + offset))
        if interval is None:
            break
        offset += interval
    return tuple(windows)


def _repeating_minutes(
    start_minute: int, repeat_every_minutes: int | None, duration_minutes: int
) -> tuple[int, ...]:
    minutes = []
    current = start_minute
    while current < duration_minutes and len(minutes) < 129:
        minutes.append(current)
        if repeat_every_minutes is None:
            break
        current += repeat_every_minutes
    return tuple(minutes)


STANDARD_PROVISIONING_SOURCE_ID = "ASM-STANDARD-PROVISIONING-V1"
DRINKING_WATER_ML_PER_PERSON_DAY = 3_000
MEAL_UNITS_PER_PERSON_DAY = 3
STOCK_DAYS = 3
# Default deliveries may use at most half of the 128 compiled scheduled events.
_MAX_DEFAULT_DELIVERY_EVENTS = 64


def with_standard_provisioning(
    conditions: OperationalConditions,
    *,
    population: int,
    duration_minutes: int,
    supply_start_day: int,
) -> tuple[OperationalConditions, dict[str, object] | None]:
    """Fill an unknown inventory with the standard stock and a recurring resupply.

    Entered or partial inventories are kept as entered. The defaults follow national
    guidance: drinking water of 3 L per person per day, three days of household and
    municipal stock, and outside supply from day 4. Three meal units per person per day
    is a software assumption standing in for the 2,000 kcal reference intake.
    """
    if conditions.initial_resources.status != "unknown":
        return conditions, None
    water_per_day = DRINKING_WATER_ML_PER_PERSON_DAY * population
    meals_per_day = MEAL_UNITS_PER_PERSON_DAY * population
    stock = InitialResourceCondition(
        status="entered",
        water_ml=water_per_day * STOCK_DAYS,
        food_meal_units=meals_per_day * STOCK_DAYS,
        source_ids=(STANDARD_PROVISIONING_SOURCE_ID,),
        source_kind="assumption",
    )
    start_minute = (supply_start_day - 1) * 1440
    interval_days = 1
    deliveries: list[ResourceQuantityCondition] = []
    if start_minute < duration_minutes:
        span_days = math.ceil((duration_minutes - start_minute) / 1440)
        # Long Runs deliver every few days with the matching quantity instead of daily,
        # so the expanded schedule stays within the scheduled-event limit.
        while 2 * math.ceil(span_days / interval_days) > _MAX_DEFAULT_DELIVERY_EVENTS:
            interval_days += 1
        deliveries = [
            ResourceQuantityCondition(
                kind="resource_delivery",
                resource_id=resource_id,
                quantity=per_day * interval_days,
                due_minute=start_minute,
                warehouse_id=stock.warehouse_id,
                source_id=STANDARD_PROVISIONING_SOURCE_ID,
                source_kind="assumption",
                repeat_every_minutes=interval_days * 1440,
            )
            for resource_id, per_day in (
                ("water_ml", water_per_day), ("food_meal_units", meals_per_day)
            )
        ]
    provisioned = OperationalConditions.model_validate({
        **conditions.model_dump(),
        "initial_resources": stock.model_dump(),
        "scheduled": [
            *(item.model_dump() for item in conditions.scheduled),
            *(item.model_dump() for item in deliveries),
        ],
    })
    record = {
        "id": STANDARD_PROVISIONING_SOURCE_ID,
        "subject": "standard_provisioning",
        "value": {
            "drinking_water_ml_per_person_day": DRINKING_WATER_ML_PER_PERSON_DAY,
            "meal_units_per_person_day": MEAL_UNITS_PER_PERSON_DAY,
            "stock_days": STOCK_DAYS,
            "initial_water_ml": stock.water_ml,
            "initial_food_meal_units": stock.food_meal_units,
            "supply_start_day": supply_start_day,
            "supply_interval_days": interval_days if deliveries else None,
            "supply_scheduled": bool(deliveries),
        },
        "reason": (
            "No starting inventory was entered, so the national guidance amounts were "
            "assumed: 3 days of stock and outside supply from the chosen day. Not a "
            "measured shelter inventory or delivery plan."
        ),
        "source_id": STANDARD_PROVISIONING_SOURCE_ID,
    }
    return provisioned, record
