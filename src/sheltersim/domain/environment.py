"""Opt-in, assumption-tagged zone environment modifiers (H19)."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sheltersim.events.queue import EventsById

if TYPE_CHECKING:
    from sheltersim.core.world import WorldState


ZONE_ENVIRONMENT_STATE_KEY = "zone_environment_v1"
ZONE_ENVIRONMENT_POLICY = "zone_environment_v1"
ZONE_ENVIRONMENT_SOURCE_ID = "ASM-H19-ZONE-ENVIRONMENT-V2"
_LEGACY_ZONE_ENVIRONMENT_SOURCE_ID = "ASM-H19-ZONE-ENVIRONMENT-V1"
MAX_ZONE_ENVIRONMENT_MODIFIERS = 16

_StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

ZONE_DEMAND_ACTIONS = frozenset(
    {"request_toilet", "rest", "sleep", "seek_help", "fetch_water", "collect_meal"}
)


class _EnvironmentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ZoneEnvironmentBaseline(_EnvironmentContract):
    """Initial experimental relative conditions for one zone."""

    ventilation_effectiveness: float = Field(
        default=0.7, strict=True, ge=0.0, le=1.0, allow_inf_nan=False
    )
    lighting_level: float = Field(default=0.5, strict=True, ge=0.0, le=1.0, allow_inf_nan=False)
    privacy_level: float = Field(default=0.5, strict=True, ge=0.0, le=1.0, allow_inf_nan=False)
    temperature_c: float = Field(default=20.0, strict=True, ge=-40.0, le=60.0, allow_inf_nan=False)


class ScenarioZoneEnvironmentConfig(_EnvironmentContract):
    """Bounded model assumptions for zone noise and action-demand modifiers."""

    schema_version: Literal[1] = 1
    policy_version: Literal["zone-environment-v1", "zone-environment-v2"] = "zone-environment-v2"
    source_type: Literal["assumption"] = "assumption"
    source_id: _StableId = ZONE_ENVIRONMENT_SOURCE_ID
    noise_rest_efficiency_loss: float = Field(
        default=0.5, strict=True, ge=0.0, le=0.9, allow_inf_nan=False
    )
    min_probability_multiplier: float = Field(
        default=0.5, strict=True, ge=0.1, le=1.0, allow_inf_nan=False
    )
    max_probability_multiplier: float = Field(
        default=2.0, strict=True, ge=1.0, le=2.0, allow_inf_nan=False
    )
    min_rest_decision_minutes: int = Field(default=1, strict=True, ge=1, le=60)
    max_rest_decision_minutes: int = Field(default=60, strict=True, ge=1, le=240)
    max_active_modifiers_per_zone: int = Field(
        default=MAX_ZONE_ENVIRONMENT_MODIFIERS,
        strict=True,
        ge=1,
        le=MAX_ZONE_ENVIRONMENT_MODIFIERS,
    )
    lighting_sleep_efficiency_loss: float = Field(
        default=0.2, strict=True, ge=0.0, le=0.9, allow_inf_nan=False
    )
    ventilation_hygiene_growth_per_minute: float = Field(
        default=0.0001, strict=True, ge=0.0, le=0.01, allow_inf_nan=False
    )
    privacy_discontent_per_minute: float = Field(
        default=0.0001, strict=True, ge=0.0, le=0.01, allow_inf_nan=False
    )
    temperature_stress_per_degree_minute: float = Field(
        default=0.0001, strict=True, ge=0.0, le=0.01, allow_inf_nan=False
    )
    comfortable_min_temperature_c: float = Field(
        default=18.0, strict=True, ge=-10.0, le=40.0, allow_inf_nan=False
    )
    comfortable_max_temperature_c: float = Field(
        default=26.0, strict=True, ge=-10.0, le=50.0, allow_inf_nan=False
    )
    baseline_by_zone: dict[_StableId, ZoneEnvironmentBaseline] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def keep_legacy_environment_assumptions_neutral(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        legacy = raw.get("policy_version") == "zone-environment-v1" or (
            "policy_version" not in raw
            and not {
                "lighting_sleep_efficiency_loss",
                "ventilation_hygiene_growth_per_minute",
                "privacy_discontent_per_minute",
                "temperature_stress_per_degree_minute",
                "baseline_by_zone",
            }.intersection(raw)
        )
        if not legacy:
            return raw
        result = dict(raw)
        result.setdefault("policy_version", "zone-environment-v1")
        result.setdefault("source_id", _LEGACY_ZONE_ENVIRONMENT_SOURCE_ID)
        for name in (
            "lighting_sleep_efficiency_loss",
            "ventilation_hygiene_growth_per_minute",
            "privacy_discontent_per_minute",
            "temperature_stress_per_degree_minute",
        ):
            if name not in result:
                result[name] = 0.0
        result.setdefault("baseline_by_zone", {})
        return result

    @model_validator(mode="after")
    def multipliers_are_ordered(self) -> ScenarioZoneEnvironmentConfig:
        if self.policy_version == "zone-environment-v1" and (
            self.lighting_sleep_efficiency_loss
            or self.ventilation_hygiene_growth_per_minute
            or self.privacy_discontent_per_minute
            or self.temperature_stress_per_degree_minute
            or self.baseline_by_zone
        ):
            raise ValueError("new environment feedback requires zone-environment-v2")
        if self.min_probability_multiplier > self.max_probability_multiplier:
            raise ValueError("minimum probability multiplier exceeds its maximum")
        if self.min_rest_decision_minutes > self.max_rest_decision_minutes:
            raise ValueError("minimum rest decision interval exceeds its maximum")
        if self.comfortable_min_temperature_c >= self.comfortable_max_temperature_c:
            raise ValueError("comfortable temperature interval must be non-empty")
        return self


class AmbientNoiseParameters(_EnvironmentContract):
    """Normalized zone noise level; this is not a physical sound measurement."""

    level: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)


class VentilationParameters(_EnvironmentContract):
    """Relative ventilation effectiveness, not measured air-change rate."""

    effectiveness: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)


class LightingParameters(_EnvironmentContract):
    """Normalized lighting intensity used only by the experimental rest model."""

    level: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)


class PrivacyParameters(_EnvironmentContract):
    """Normalized privacy availability for a zone."""

    level: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)


class TemperatureParameters(_EnvironmentContract):
    """Zone temperature in degrees Celsius; values are scenario assumptions."""

    temperature_c: float = Field(strict=True, ge=-40.0, le=60.0, allow_inf_nan=False)


class DemandRateParameters(_EnvironmentContract):
    """Bound one action's relative selection odds and optional rest duration."""

    action: Literal["request_toilet", "rest", "sleep", "seek_help", "fetch_water", "collect_meal"]
    probability_multiplier: float = Field(
        default=1.0, strict=True, ge=0.5, le=2.0, allow_inf_nan=False
    )
    duration_multiplier: float = Field(
        default=1.0, strict=True, ge=0.5, le=2.0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def duration_only_applies_to_rest(self) -> DemandRateParameters:
        if self.duration_multiplier != 1.0 and self.action not in {"rest", "sleep"}:
            raise ValueError("duration_multiplier is supported only for rest and sleep actions")
        if self.probability_multiplier == self.duration_multiplier == 1.0:
            raise ValueError("a demand modifier must change probability or duration")
        return self


@dataclass(frozen=True)
class ZoneDemandView:
    action: str
    probability_multiplier: float
    duration_multiplier: float
    modifier_event_ids: tuple[str, ...]
    assumption_ids: tuple[str, ...]


@dataclass(frozen=True)
class ZoneEnvironmentView:
    ambient_noise_level: float = 0.0
    rest_efficiency_multiplier: float = 1.0
    noise_modifier_event_ids: tuple[str, ...] = ()
    noise_assumption_ids: tuple[str, ...] = ()
    demand_modifiers: tuple[ZoneDemandView, ...] = ()
    min_rest_decision_minutes: int = 1
    max_rest_decision_minutes: int = 60
    ventilation_effectiveness: float = 0.7
    lighting_level: float = 0.5
    privacy_level: float = 0.5
    temperature_c: float = 20.0
    ventilation_hygiene_growth_per_minute: float = 0.0001
    privacy_discontent_per_minute: float = 0.0001
    temperature_stress_per_minute: float = 0.0
    condition_modifier_event_ids: tuple[str, ...] = ()
    condition_assumption_ids: tuple[str, ...] = ()


def create_zone_environment_runtime_state() -> dict[str, object]:
    """Return optional checkpoint state for a configured environment policy."""
    return {"schema_version": 1, "modifiers": {}}


def zone_environment_view(world: WorldState, zone_id: str) -> ZoneEnvironmentView:
    """Build stable, read-only environment inputs for one zone."""
    config_data = world.model_configs.get("zone_environment")
    state = world.model_state.get(ZONE_ENVIRONMENT_STATE_KEY)
    if not isinstance(config_data, dict) or not isinstance(state, dict):
        return ZoneEnvironmentView()
    config = ScenarioZoneEnvironmentConfig.model_validate(config_data)
    entries = [
        (modifier_id, record)
        for modifier_id, record in sorted(state["modifiers"].items())
        if record["zone_id"] == zone_id
    ]
    entries.sort(key=lambda item: (item[1]["start_minute"], item[0]))
    by_op = {
        op: [item for item in entries if item[1]["op"] == op]
        for op in ("set_ventilation", "set_lighting", "set_privacy", "set_temperature")
    }
    baseline = ZoneEnvironmentBaseline.model_validate(
        config_data.get("baseline_by_zone", {}).get(zone_id, {})
    )

    def last(op: str) -> dict[str, Any] | None:
        return by_op[op][-1][1] if by_op[op] else None

    ventilation_record = last("set_ventilation")
    lighting_record = last("set_lighting")
    privacy_record = last("set_privacy")
    temperature_record = last("set_temperature")
    ventilation = (
        ventilation_record["parameters"]["effectiveness"]
        if ventilation_record
        else baseline.ventilation_effectiveness
    )
    lighting = (
        lighting_record["parameters"]["level"] if lighting_record else baseline.lighting_level
    )
    privacy = privacy_record["parameters"]["level"] if privacy_record else baseline.privacy_level
    temperature = (
        temperature_record["parameters"]["temperature_c"]
        if temperature_record
        else baseline.temperature_c
    )
    condition_records = [record for op in by_op.values() for _, record in op]
    condition_event_ids = tuple(sorted(record["start_event_id"] for record in condition_records))
    condition_source_ids = {
        config.source_id,
        *(record["source_id"] for record in condition_records),
    }
    from sheltersim.policies.registry import (
        create_zone_environment_formula,
        create_zone_hygiene_formula,
        create_zone_privacy_formula,
        create_zone_rest_formula,
    )

    formula_config = config.model_dump(mode="python")
    temperature_formula = create_zone_environment_formula(world.model_configs)
    hygiene_formula = create_zone_hygiene_formula(world.model_configs)
    privacy_formula = create_zone_privacy_formula(world.model_configs)
    rest_formula = create_zone_rest_formula(world.model_configs)
    policy_selection = world.model_configs.get("policy_registry", {})
    for domain, formula in (
        ("zone_environment", temperature_formula),
        ("zone_hygiene", hygiene_formula),
        ("zone_privacy", privacy_formula),
        ("zone_rest", rest_formula),
    ):
        if domain in policy_selection:
            condition_source_ids.add(formula.source_id)
    condition_sources = tuple(sorted(condition_source_ids))
    degree_deviation = temperature_formula.temperature_deviation(temperature, formula_config)
    noise_entries = [item for item in entries if item[1]["op"] == "set_ambient_noise"]
    noise_level = max((record["parameters"]["level"] for _, record in noise_entries), default=0.0)
    rest_efficiency = rest_formula.rest_efficiency(noise_level, lighting, formula_config)

    by_action: dict[str, list[tuple[str, dict[str, object]]]] = defaultdict(list)
    for item in entries:
        if item[1]["op"] == "modify_demand_rate":
            by_action[item[1]["parameters"]["action"]].append(item)
    demand: list[ZoneDemandView] = []
    for action, modifiers in sorted(by_action.items()):
        probability = math.prod(
            record["parameters"]["probability_multiplier"] for _, record in modifiers
        )
        duration = math.prod(record["parameters"]["duration_multiplier"] for _, record in modifiers)
        source_ids = {config.source_id}
        event_ids: set[str] = set()
        for _, record in modifiers:
            event_ids.add(record["start_event_id"])
            source_ids.update(record["assumption_ids"])
            source_ids.add(record["source_id"])
        demand.append(
            ZoneDemandView(
                action=action,
                probability_multiplier=min(
                    config.max_probability_multiplier,
                    max(config.min_probability_multiplier, probability),
                ),
                duration_multiplier=min(
                    2.0,
                    max(0.5, duration),
                )
                if action in {"rest", "sleep"}
                else 1.0,
                modifier_event_ids=tuple(sorted(event_ids)),
                assumption_ids=tuple(sorted(source_ids)),
            )
        )

    noise_sources = {config.source_id}
    noise_event_ids: set[str] = set()
    for _, record in noise_entries:
        noise_event_ids.add(record["start_event_id"])
        noise_sources.add(record["source_id"])
        noise_sources.update(record["assumption_ids"])
    return ZoneEnvironmentView(
        ambient_noise_level=noise_level,
        rest_efficiency_multiplier=rest_efficiency,
        noise_modifier_event_ids=tuple(sorted(noise_event_ids)),
        noise_assumption_ids=tuple(sorted(noise_sources)) if noise_entries else (),
        demand_modifiers=tuple(demand),
        min_rest_decision_minutes=config.min_rest_decision_minutes,
        max_rest_decision_minutes=config.max_rest_decision_minutes,
        ventilation_effectiveness=ventilation,
        lighting_level=lighting,
        privacy_level=privacy,
        temperature_c=temperature,
        ventilation_hygiene_growth_per_minute=hygiene_formula.ventilation_hygiene_growth(
            ventilation, formula_config
        ),
        privacy_discontent_per_minute=privacy_formula.privacy_discontent(privacy, formula_config),
        temperature_stress_per_minute=(
            degree_deviation * config.temperature_stress_per_degree_minute
        ),
        condition_modifier_event_ids=condition_event_ids,
        condition_assumption_ids=(
            condition_sources
            if condition_records or config.policy_version == "zone-environment-v2"
            else ()
        ),
    )


def _has_exact_schema(state: Any) -> bool:
    return (
        isinstance(state, dict)
        and set(state) == {"schema_version", "modifiers"}
        and state.get("schema_version") == 1
        and isinstance(state.get("modifiers"), dict)
    )


def validate_zone_environment_runtime_state(
    world: WorldState,
    *,
    validate_event_history: bool = True,
    tick_memo: Any = None,
) -> None:
    """Validate opt-in config and active modifier/event/provenance links.

    With a tick_memo a config that passed is not read again while it and the zones are unchanged.
    """
    config_data = world.model_configs.get("zone_environment")
    state = world.model_state.get(ZONE_ENVIRONMENT_STATE_KEY)
    if (
        tick_memo is not None
        and tick_memo.configs_unchanged
        and tick_memo.zones_unchanged
        and tick_memo.environment_config_passed
    ):
        if _has_exact_schema(state):
            return
        raise ValueError("zone_environment_v1 has an invalid exact schema")
    if config_data is None:
        if state is not None:
            raise ValueError("zone environment state requires a zone_environment config")
        pending_events = world.event_queue.pending()
        if any(
            event.event_type.startswith("effect_program_environment") for event in pending_events
        ):
            raise ValueError("zone environment events require the zone_environment policy")
        if validate_event_history and any(
            event_type.startswith("effect_program_environment")
            for event_type in world.event_log.event_types()
        ):
            raise ValueError("zone environment events require the zone_environment policy")
        return
    config = ScenarioZoneEnvironmentConfig.model_validate(config_data)
    canonical_config = config.model_dump(mode="json")
    legacy_config = {
        key: value
        for key, value in canonical_config.items()
        if key
        not in {
            "lighting_sleep_efficiency_loss",
            "ventilation_hygiene_growth_per_minute",
            "privacy_discontent_per_minute",
            "temperature_stress_per_degree_minute",
            "comfortable_min_temperature_c",
            "comfortable_max_temperature_c",
            "baseline_by_zone",
        }
    }
    if config_data not in (canonical_config, legacy_config):
        raise ValueError("zone_environment config must contain canonical v1 values")
    if set(config.baseline_by_zone) - set(world.zones):
        raise ValueError("zone environment baseline references an unknown zone")
    if not _has_exact_schema(state):
        raise ValueError("zone_environment_v1 has an invalid exact schema")
    if tick_memo is not None:
        tick_memo.environment_config_passed = True

    if not validate_event_history:
        return

    by_id = EventsById(world.event_log, world.event_queue.pending())
    counts: dict[str, int] = defaultdict(int)
    for modifier_id, record in state["modifiers"].items():
        fields = {
            "modifier_id",
            "program_id",
            "effect_id",
            "op",
            "zone_id",
            "parameters",
            "start_event_id",
            "end_event_id",
            "start_minute",
            "end_minute",
            "source_id",
            "assumption_ids",
        }
        if (
            not isinstance(modifier_id, str)
            or not re.fullmatch(r"modifier-[0-9a-f]{32}", modifier_id)
            or not isinstance(record, dict)
            or set(record) != fields
            or record.get("modifier_id") != modifier_id
            or record.get("op")
            not in {
                "set_ambient_noise",
                "modify_demand_rate",
                "set_ventilation",
                "set_lighting",
                "set_privacy",
                "set_temperature",
            }
            or record.get("zone_id") not in world.zones
            or not isinstance(record.get("program_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", record["program_id"])
            or not isinstance(record.get("effect_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", record["effect_id"])
            or not isinstance(record.get("start_event_id"), str)
            or not isinstance(record.get("end_event_id"), str)
            or not isinstance(record.get("source_id"), str)
            or not isinstance(record.get("assumption_ids"), list)
            or any(not isinstance(item, str) or not item for item in record["assumption_ids"])
            or len(record["assumption_ids"]) != len(set(record["assumption_ids"]))
            or any(
                not isinstance(record.get(name), int) or isinstance(record.get(name), bool)
                for name in ("start_minute", "end_minute")
            )
            or not 0 <= record["start_minute"] < record["end_minute"] < world.duration_minutes
        ):
            raise ValueError("zone environment modifier record is invalid")
        parameter_model = {
            "set_ambient_noise": AmbientNoiseParameters,
            "modify_demand_rate": DemandRateParameters,
            "set_ventilation": VentilationParameters,
            "set_lighting": LightingParameters,
            "set_privacy": PrivacyParameters,
            "set_temperature": TemperatureParameters,
        }[record["op"]]
        parameters = parameter_model.model_validate(record["parameters"])
        if (
            record["op"] in {"set_ventilation", "set_lighting", "set_privacy", "set_temperature"}
            and config.policy_version != "zone-environment-v2"
        ):
            raise ValueError("condition modifiers require zone-environment-v2")
        if record["op"] == "modify_demand_rate":
            if (
                parameters.action in {"fetch_water", "collect_meal"}
                and "resource_service_v1" not in world.model_state
            ):
                raise ValueError("zone demand modifier requires the resource-service policy")
            if (
                parameters.probability_multiplier < config.min_probability_multiplier
                or parameters.probability_multiplier > config.max_probability_multiplier
                or (
                    parameters.action in {"rest", "sleep"}
                    and (
                        parameters.duration_multiplier < 0.5 or parameters.duration_multiplier > 2.0
                    )
                )
            ):
                raise ValueError("zone demand modifier exceeds configured limits")
        if record["source_id"] != config.source_id:
            raise ValueError(
                "zone environment modifier source does not match its assumption config"
            )

        start_event = by_id.get(record["start_event_id"])
        end_event = by_id.get(record["end_event_id"])
        if (
            start_event is None
            or end_event is None
            or start_event.event_type != "effect_program_environment"
            or end_event.event_type != "effect_program_environment_expire"
            or start_event.payload
            != {
                "program_id": record["program_id"],
                "effect_id": record["effect_id"],
                "modifier_id": modifier_id,
                "zone_id": record["zone_id"],
                "op": record["op"],
                "parameters": parameters.model_dump(mode="json"),
                "end_minute": record["end_minute"],
            }
            or end_event.payload
            != {
                "program_id": record["program_id"],
                "effect_id": record["effect_id"],
                "modifier_id": modifier_id,
                "zone_id": record["zone_id"],
            }
            or start_event.due_minute != record["start_minute"]
            or end_event.due_minute != record["end_minute"]
            or end_event.causation_id != start_event.event_id
            or not isinstance(start_event.provenance.get("effect_program_v1"), Mapping)
        ):
            raise ValueError("zone environment modifier has invalid start/expiry event links")
        marker = start_event.provenance["effect_program_v1"]
        marker_dependencies = marker.get("policy_dependencies")
        marker_assumptions = marker.get("assumption_ids")
        if (
            marker.get("phase") != "start"
            or marker.get("program_id") != record["program_id"]
            or marker.get("effect_id") != record["effect_id"]
            or marker.get("op") != record["op"]
            or not isinstance(marker_dependencies, (list, tuple))
            or tuple(marker_dependencies) != (ZONE_ENVIRONMENT_POLICY,)
            or not isinstance(marker_assumptions, (list, tuple))
            or tuple(marker_assumptions) != tuple(record["assumption_ids"])
            or not isinstance(record["assumption_ids"], list)
            or record["assumption_ids"] != sorted(set(record["assumption_ids"]))
        ):
            raise ValueError("zone environment modifier provenance does not match its start event")
        counts[record["zone_id"]] += 1
    if any(count > config.max_active_modifiers_per_zone for count in counts.values()):
        raise ValueError("zone environment runtime exceeds its per-zone modifier limit")
