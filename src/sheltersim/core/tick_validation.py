"""What the check at the end of a tick reads of the configs, for native/src/rules/after_tick.rs.

WorldState.validate_after_tick is one call to the kernel, which keeps the memo of the last passed
check (its TickMemo, in world.__dict__["_tick_memo"]) and runs every rule of the per-tick check.
A state it doesn't take, and every world without the kernels, gets validate(validate_event_history
=False), with no memo, which passes or raises the error text.

The configs are parsed here, by the contracts that validate parses them with (pydantic models and
the canonical_* functions, which stay in Python: docs/rust-port/step4-plan.md, decision D3). The
kernel calls tick_plan when it has no plan for the configs it holds, which is the first tick of a
memo and the tick after a write to model_configs, and body_config_ids when the saved body config
changed. Either raises for a config the per-tick check would turn down: the kernel then says
Unsupported and validate raises its own text. Neither reads the simulation state beyond the zone
IDs and the saved body config, and neither writes anything.
"""

from __future__ import annotations

from typing import Any

from sheltersim import native


def tick_plan(world: Any) -> Any:
    """The configs as the per-tick check reads them, or an error where it would turn them down."""
    from sheltersim.contracts.policies import selected_models
    from sheltersim.contracts.runtime import declared_states
    from sheltersim.domain.environment import (
        LEGACY_ZONE_ENVIRONMENT_FIELDS,
        ScenarioZoneEnvironmentConfig,
    )
    from sheltersim.domain.facility_queues import independent_queues_required
    from sheltersim.domain.infrastructure import (
        INFRASTRUCTURE_EVENT_TYPES,
        ScenarioInfrastructureSpec,
    )
    from sheltersim.domain.models import InformationSpreadConfig
    from sheltersim.domain.operational import H03_EVENT_TYPES, operational_events_enabled
    from sheltersim.domain.psychological_state import MEAN_REVERTING_PSYCHOLOGY_POLICIES
    from sheltersim.domain.simulator_state import simulator_signatures
    from sheltersim.experiments.scenario import ResourceServiceConfig

    configs = world.model_configs
    psychology = selected_models(configs)["psychology"]
    declared = declared_states(configs)
    required = sorted(
        key.removeprefix("simulator:")
        for key in simulator_signatures(configs)
        if key.startswith("simulator:")
    )
    # WorldState.validate: resource_service's config whole. Without it the world is a v5 one and
    # there is none; the kernel compares this with the world's schema_version at every tick.
    resource_config = None
    if "resource_service" in configs:
        resource_config = ResourceServiceConfig.model_validate(configs["resource_service"])
        if configs["resource_service"] != resource_config.model_dump(mode="json"):
            raise ValueError("resource_service config is not canonical")
    information = InformationSpreadConfig.from_mapping(configs.get("information_spread"))
    if "information_spread" in configs and configs["information_spread"] != information.to_dict():
        raise ValueError("information_spread config is not canonical")
    try:
        operational_enabled: bool | None = operational_events_enabled(configs, ())
    except ValueError:
        # The rule reads it only when the state is missing (domain/operational.py).
        operational_enabled = None
    # validate_zone_environment_runtime_state's config part; the zones are checked by the kernel.
    environment_zones = None
    if (raw := configs.get("zone_environment")) is not None:
        environment = ScenarioZoneEnvironmentConfig.model_validate(raw)
        canonical = environment.model_dump(mode="json")
        legacy = {k: v for k, v in canonical.items() if k not in LEGACY_ZONE_ENVIRONMENT_FIELDS}
        if raw not in (canonical, legacy):
            raise ValueError("zone_environment config is not canonical")
        environment_zones = sorted(environment.baseline_by_zone)
    # validate_infrastructure_runtime_state's config part.
    if (raw := configs.get("infrastructure")) is not None:
        if not isinstance(raw, dict):
            raise ValueError("infrastructure config is not an object")
        if raw != ScenarioInfrastructureSpec.model_validate(raw).model_dump(mode="json"):
            raise ValueError("infrastructure config is not canonical")
    return native.rules("after_tick").Plan(
        declared=[(name, item.schema_version) for name, item in declared.items()],
        required=required,
        independent_queues=independent_queues_required(configs),
        information_enabled=information.enabled,
        mean_reverting=psychology in MEAN_REVERTING_PSYCHOLOGY_POLICIES,
        shared_configured="shared_operations" in configs,
        operational_enabled=operational_enabled,
        environment_zones=environment_zones,
        infrastructure_configured=configs.get("infrastructure") is not None,
        # "resource_delivery" opts in only with a shipment ID; the Python rule tells which.
        operational_events=frozenset(H03_EVENT_TYPES | {"resource_delivery"}),
        infrastructure_events=frozenset(INFRASTRUCTURE_EVENT_TYPES),
        resource_config=resource_config,
    )


def body_config_ids(world: Any) -> list[str]:
    """validate_body_state's check of the saved config, which raises where it would. Returns the
    people its per-person parameters name: the kernel checks at each tick that each is here or in
    the population state's history, which is all the rest the check reads."""
    from sheltersim.domain.body_state import BODY_STATE_KEY, validate_body_config

    return sorted(validate_body_config(world, world.model_state[BODY_STATE_KEY]))


def saved_json(value: Any) -> bytes | None:
    """The value's canonical JSON, or None if it has none, which never compares as same."""
    try:
        return native.canonical_json(value)
    except ValueError:
        # For example a str that cannot be encoded; the rules decide about it.
        return None
