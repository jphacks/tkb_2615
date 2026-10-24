"""Lifecycle hooks for the versioned simulator extension namespaces."""

from __future__ import annotations

from typing import Any

from sheltersim.contracts.operations import (
    SHARED_OPERATIONS_KEY,
    SHARED_OPERATIONS_POLICY,
    validate_shared_operations_config,
)
from sheltersim.contracts.policies import selected_models
from sheltersim.core.world import WorldState
from sheltersim.domain.action_lifecycle import ACTION_LIFECYCLE_POLICY_VERSION
from sheltersim.domain.agent_dynamics import (
    AGENT_DYNAMICS_STATE_KEY,
    canonical_agent_dynamics_config,
    create_agent_dynamics_state,
    validate_agent_dynamics_state,
)
from sheltersim.domain.body_state import (
    BODY_POLICY_VERSION,
    BODY_STATE_KEY,
    create_body_state,
    validate_body_state,
)
from sheltersim.domain.common_operations import (
    create_shared_operations_state,
    validate_shared_operations_state,
)
from sheltersim.domain.facility_queues import (
    FACILITY_QUEUES_LEGACY_MODE_KEY,
    FACILITY_QUEUES_POLICY_VERSION,
    FACILITY_QUEUES_STATE_KEY,
    create_facility_queue_state,
    independent_queues_required,
    is_legacy_queue_checkpoint,
    validate_facility_queue_state,
)
from sheltersim.domain.geometry_overlay import (
    GEOMETRY_OVERLAY_KEY,
    GEOMETRY_OVERLAY_POLICY,
    TEMPORARY_ROOM_BED_POLICY_VERSION,
    TEMPORARY_ROOM_INTAKE_POLICY_VERSION,
    create_geometry_overlay_state,
    validate_geometry_overlay_state,
)
from sheltersim.domain.movement import (
    MOVEMENT_POLICY_VERSION,
    MOVEMENT_STATE_KEY,
    create_movement_runtime_state,
    validate_movement_runtime_state,
)
from sheltersim.domain.population import (
    POPULATION_DYNAMICS_POLICY_VERSION,
    POPULATION_DYNAMICS_STATE_KEY,
    create_population_dynamics_state,
    validate_population_dynamics_state,
)
from sheltersim.domain.population_lifecycle import (
    POPULATION_LIFECYCLE_POLICY_VERSION,
    POPULATION_LIFECYCLE_STATE_KEY,
    create_population_lifecycle_state,
    validate_population_lifecycle_state,
)
from sheltersim.domain.social_network import (
    PROXIMITY_CONTACT_POLICY_VERSION,
    SOCIAL_NETWORK_POLICY_VERSION,
    SOCIAL_NETWORK_STATE_KEY,
    canonical_proximity_contact_config,
    create_social_network_state,
    validate_social_network_state,
)
from sheltersim.domain.staff_dynamics import (
    STAFF_DYNAMICS_POLICY_VERSION,
    STAFF_DYNAMICS_SOURCE_ID,
    STAFF_DYNAMICS_STATE_KEY,
    canonical_staff_dynamics_config,
    create_staff_dynamics_state,
    validate_staff_dynamics_state,
)
from sheltersim.domain.staff_operations import (
    STAFF_OPERATIONS_POLICY_VERSION,
    STAFF_OPERATIONS_STATE_KEY,
    create_staff_operations_state,
    validate_staff_operations_state,
)
from sheltersim.domain.temporary_spaces import (
    TEMPORARY_SPACES_POLICY_VERSION,
    TEMPORARY_SPACES_STATE_KEY,
    create_temporary_spaces_state,
    validate_temporary_spaces_state,
)


def simulator_signatures(configs: dict[str, Any]) -> dict[str, str]:
    """Freeze enabled extension implementations alongside installed policy models."""
    if "geometry_overlay" in configs and "facility_geometry" not in configs:
        raise ValueError("geometry_overlay requires facility_geometry")
    if "shared_operations" in configs:
        validate_shared_operations_config(configs["shared_operations"])
    body_policy_enabled = selected_models(configs)["psychology"] == BODY_POLICY_VERSION
    proximity = canonical_proximity_contact_config(configs.get("social_proximity"))
    if proximity is not None and (
        "facility_geometry" not in configs or not body_policy_enabled
    ):
        raise ValueError(
            "social_proximity requires facility_geometry and life-state-feedback-v1"
        )
    if (
        not configs.get("simulator_extensions", {}).get("enabled", False)
        and "facility_geometry" not in configs
        and "population" not in configs
        and "staff_operations" not in configs
        and "staff_dynamics" not in configs
        and "shared_operations" not in configs
        and not body_policy_enabled
    ):
        standalone = {
            "operator:action_lifecycle": f"{ACTION_LIFECYCLE_POLICY_VERSION}@1:state1"
        }
        if "population_context" in configs:
            standalone["operator:autonomy_behavior"] = "contextual-autonomy@2:stateless"
        if "zone_environment" in configs:
            from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
            environment = ScenarioZoneEnvironmentConfig.model_validate(configs["zone_environment"])
            standalone["operator:zone_environment"] = f"{environment.policy_version}@1:state1"
        return standalone
    versions = {
        AGENT_DYNAMICS_STATE_KEY: canonical_agent_dynamics_config(
            configs.get("agent_dynamics")
        )["policy_version"],
        POPULATION_DYNAMICS_STATE_KEY: POPULATION_DYNAMICS_POLICY_VERSION,
        SOCIAL_NETWORK_STATE_KEY: SOCIAL_NETWORK_POLICY_VERSION,
        STAFF_DYNAMICS_STATE_KEY: STAFF_DYNAMICS_POLICY_VERSION,
    }
    if independent_queues_required(configs):
        versions[FACILITY_QUEUES_STATE_KEY] = FACILITY_QUEUES_POLICY_VERSION
    if "shared_operations" in configs:
        versions[SHARED_OPERATIONS_KEY] = SHARED_OPERATIONS_POLICY
    versions[STAFF_DYNAMICS_STATE_KEY] = canonical_staff_dynamics_config(
        configs.get("staff_dynamics")
    )["policy_version"]
    if (
        configs.get("simulator_extensions", {}).get("enabled", False)
        or "population" in configs
    ):
        versions[POPULATION_LIFECYCLE_STATE_KEY] = POPULATION_LIFECYCLE_POLICY_VERSION
        if configs.get("simulator_extensions", {}).get("enabled", False):
            versions[TEMPORARY_SPACES_STATE_KEY] = TEMPORARY_SPACES_POLICY_VERSION
    if (
        configs.get("simulator_extensions", {}).get("enabled", False)
        or "staff_operations" in configs
        or body_policy_enabled
    ):
        versions[STAFF_OPERATIONS_STATE_KEY] = STAFF_OPERATIONS_POLICY_VERSION
    if "facility_geometry" in configs:
        versions[MOVEMENT_STATE_KEY] = MOVEMENT_POLICY_VERSION
    if "geometry_overlay" in configs:
        overlay_config = configs["geometry_overlay"]
        if (not isinstance(overlay_config, dict)
                or set(overlay_config) != {"schema_version"}
                or type(overlay_config["schema_version"]) is not int
                or overlay_config["schema_version"] not in {1, 2, 3}):
            raise ValueError("unsupported geometry_overlay config")
        versions[GEOMETRY_OVERLAY_KEY] = GEOMETRY_OVERLAY_POLICY
    if body_policy_enabled:
        versions[BODY_STATE_KEY] = BODY_POLICY_VERSION
    signatures = {
        f"simulator:{key}": _simulator_signature(key, version)
        for key, version in versions.items()
    }
    geometry = configs.get("facility_geometry", {})
    service_points = geometry.get("service_points", []) if isinstance(geometry, dict) else []
    if service_points:
        import hashlib
        import json
        service_hash = hashlib.sha256(json.dumps(
            service_points, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()).hexdigest()
        signatures["operator:facility_service_points"] = f"facility-service-points@1:{service_hash}"
    if configs.get("simulator_extensions",{}).get("enabled",False) or "population_context" in configs or body_policy_enabled:
        signatures["operator:autonomy_behavior"]="contextual-autonomy@2:stateless"
    if "zone_environment" in configs:
        from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
        environment = ScenarioZoneEnvironmentConfig.model_validate(configs["zone_environment"])
        signatures["operator:zone_environment"] = f"{environment.policy_version}@1:state1"
    if proximity is not None:
        signatures["operator:social_proximity"] = (
            f"{PROXIMITY_CONTACT_POLICY_VERSION}@1:state1:"
            f"distance={proximity['max_distance_m']}:"
            f"interval={proximity['sample_interval_minutes']}:"
            f"pairs={proximity['max_pairs_per_sample']}"
        )
    overlay_config = configs.get("geometry_overlay", {})
    if isinstance(overlay_config, dict) and overlay_config.get("schema_version") in {2, 3}:
        signatures["operator:temporary_room_beds"] = (
            f"{TEMPORARY_ROOM_BED_POLICY_VERSION}@1:state1"
        )
    if isinstance(overlay_config, dict) and overlay_config.get("schema_version") == 3:
        signatures["operator:temporary_room_intake"] = (
            f"{TEMPORARY_ROOM_INTAKE_POLICY_VERSION}@1:state1"
        )
    signatures["operator:action_lifecycle"] = (
        f"{ACTION_LIFECYCLE_POLICY_VERSION}@1:state1"
    )
    return signatures


def _simulator_signature(key: str, version: str) -> str:
    if key == STAFF_DYNAMICS_STATE_KEY:
        implementation_version = "1" if version == "continuous-duty-fatigue-v1" else "2"
    else:
        implementation_version = (
            "3" if key == BODY_STATE_KEY else "2"
            if key in {BODY_STATE_KEY, STAFF_OPERATIONS_STATE_KEY, SOCIAL_NETWORK_STATE_KEY}
            else "1"
        )
    state_version = "2" if key == MOVEMENT_STATE_KEY else "1"
    return f"{version}@{implementation_version}:state{state_version}"


def initialize_simulator_extensions(world: WorldState) -> None:
    """Create opt-in runtime namespaces before initial checkpoint capture."""
    enabled = world.model_configs.get("simulator_extensions", {}).get("enabled", False)
    body_policy_enabled = selected_models(world.model_configs)["psychology"] == BODY_POLICY_VERSION
    if (
        not enabled
        and "facility_geometry" not in world.model_configs
        and "population" not in world.model_configs
        and "staff_operations" not in world.model_configs
        and "staff_dynamics" not in world.model_configs
        and "shared_operations" not in world.model_configs
        and not body_policy_enabled
    ):
        return
    if "body_dynamics" in world.model_configs and not body_policy_enabled:
        raise ValueError("body_dynamics config requires the life-state-feedback-v1 psychology policy")
    factories = {
        AGENT_DYNAMICS_STATE_KEY: lambda: create_agent_dynamics_state(world),
        POPULATION_DYNAMICS_STATE_KEY: lambda: create_population_dynamics_state(world),
        SOCIAL_NETWORK_STATE_KEY: create_social_network_state,
        STAFF_DYNAMICS_STATE_KEY: lambda: create_staff_dynamics_state(world),
    }
    if "shared_operations" in world.model_configs:
        validate_shared_operations_config(world.model_configs["shared_operations"])
        factories[SHARED_OPERATIONS_KEY] = create_shared_operations_state
    if enabled or "population" in world.model_configs:
        factories[POPULATION_LIFECYCLE_STATE_KEY] = create_population_lifecycle_state
        if world.model_configs.get("simulator_extensions", {}).get("enabled", False):
            factories[TEMPORARY_SPACES_STATE_KEY] = create_temporary_spaces_state
    if enabled or "staff_operations" in world.model_configs or body_policy_enabled:
        factories[STAFF_OPERATIONS_STATE_KEY] = lambda: create_staff_operations_state(world)
    if body_policy_enabled:
        factories[BODY_STATE_KEY] = lambda: create_body_state(world)
    if "facility_geometry" in world.model_configs:
        factories[MOVEMENT_STATE_KEY] = lambda: create_movement_runtime_state(world)
    if independent_queues_required(world.model_configs) and not is_legacy_queue_checkpoint(world):
        factories[FACILITY_QUEUES_STATE_KEY] = lambda: create_facility_queue_state(world)
    if "geometry_overlay" in world.model_configs:
        factories[GEOMETRY_OVERLAY_KEY] = lambda: create_geometry_overlay_state(world)
    for key, factory in factories.items():
        if key not in world.model_state:
            world.model_state[key] = factory()
    validate_simulator_extensions(world)


def validate_simulator_extensions(world: WorldState) -> None:
    """Validate each extension independently for checkpoint and restore hooks."""
    required = {key.removeprefix("simulator:") for key in simulator_signatures(world.model_configs) if key.startswith("simulator:")}
    if TEMPORARY_SPACES_STATE_KEY in world.model_state and TEMPORARY_SPACES_STATE_KEY not in required:
        raise ValueError("temporary_spaces_v1 requires an enabled, signed state contract")
    missing = required - set(world.model_state)
    if is_legacy_queue_checkpoint(world):
        missing.discard(FACILITY_QUEUES_STATE_KEY)
    if FACILITY_QUEUES_LEGACY_MODE_KEY in world.model_state and not is_legacy_queue_checkpoint(world):
        raise ValueError("legacy facility queue checkpoint marker is invalid or mixed with new state")
    if missing:
        raise ValueError(f"required simulator state is missing: {sorted(missing)}")
    validate_agent_dynamics_state(world)
    validate_body_state(world)
    validate_population_dynamics_state(world)
    validate_population_lifecycle_state(world)
    validate_social_network_state(world)
    validate_staff_dynamics_state(world)
    validate_staff_operations_state(world)
    validate_temporary_spaces_state(world)
    validate_movement_runtime_state(world)
    validate_geometry_overlay_state(world)
    validate_facility_queue_state(world)
    if SHARED_OPERATIONS_KEY in world.model_state and "shared_operations" not in world.model_configs:
        raise ValueError("shared_operations_v1 requires its signed Scenario contract")
    validate_shared_operations_state(world)


def restore_simulator_extensions(world: WorldState, model_state: dict[str, Any]) -> None:
    """Restore opaque model namespaces through exact per-extension validators."""
    world.model_state = model_state
    validate_simulator_extensions(world)


def simulator_state_catalog() -> dict[str, Any]:
    """Installed built-in signatures; no world or mutable namespace is created."""
    return {
        MOVEMENT_STATE_KEY: {
            "schema_version": 2, "implementation_id": MOVEMENT_POLICY_VERSION,
            "implementation_version": "1", "entity_kind": "agent",
            "enabled_by": "facility_geometry config", "source_id": "ASM-SPATIAL-MOVEMENT-V2",
            "fields": {"positions": {"unit": "metre_2d"},
                       "journeys": {"unit": "route_progress_with_room_waypoints"}},
            "external_validation": False,
        },
        FACILITY_QUEUES_STATE_KEY: {
            "schema_version": 1,
            "implementation_id": FACILITY_QUEUES_POLICY_VERSION,
            "implementation_version": "1",
            "entity_kind": "facility",
            "enabled_by": "multiple service points or non-facility queue_id",
            "source_id": "ASM-FACILITY-QUEUES-V1",
            "fields": {
                "queued_request_ids": {"unit": "independent_fifo_request_id"},
                "active_request_ids": {"unit": "concurrent_service_reservation"},
            },
            "history": "GET branch/history?kind=operations",
            "external_validation": False,
        },
        BODY_STATE_KEY: {
            "schema_version": 1, "implementation_id": BODY_POLICY_VERSION,
            "implementation_version": "3", "entity_kind": "agent",
            "enabled_by": "policy_registry.psychology=life-state-feedback-v1",
            "source_id": "ASM-LIFE-STATE-FEEDBACK-V1",
            "fields": {"sleep_debt_minutes": {"unit": "minute", "lower": 0, "upper": 1440},
                **{key: {"unit": "normalized_0_1", "lower": 0, "upper": 1}
                   for key in ("uncertainty", "reassurance", "discontent", "positive_affect",
                               "negative_affect", "hygiene_need")}},
            "history": "GET branch/history?kind=social", "retention_limit": 20000,
            "external_validation": False,
        },
        STAFF_OPERATIONS_STATE_KEY: {
            "schema_version": 1, "implementation_id": STAFF_OPERATIONS_POLICY_VERSION,
            "implementation_version": "2", "entity_kind": "staff",
            "enabled_by": "staff_operations config, simulator_extensions.enabled or life-state psychology",
            "source_id": "ASM-STAFF-OPERATIONS-V1",
            "fields": {"operation": {"unit": "typed_task_record"},
                       "sanitation": {"unit": "normalized_0_1", "lower": 0, "upper": 1}},
            "history": "GET branch/history?kind=staff", "retention_limit": 20000,
            "external_validation": False,
        },
        POPULATION_LIFECYCLE_STATE_KEY: {
            "schema_version": 1, "implementation_id": POPULATION_LIFECYCLE_POLICY_VERSION,
            "implementation_version": "1", "entity_kind": "agent",
            "enabled_by": "population config or simulator_extensions.enabled",
            "source_id": "ASM-POPULATION-LIFECYCLE-V1",
            "fields": {
                "people": {"unit": "retained_same_identity_state"},
                "relationship_archive": {"unit": "sparse_pair_state"},
                "pending_returns": {"unit": "capacity_waiting_person"},
                "pending_relocations": {"unit": "route_bound_task"},
            },
            "external_validation": False,
        },
        TEMPORARY_SPACES_STATE_KEY: {
            "schema_version": 1,
            "implementation_id": TEMPORARY_SPACES_POLICY_VERSION,
            "implementation_version": "1",
            "entity_kind": "zone",
            "enabled_by": "population config or simulator_extensions.enabled",
            "source_id": "ASM-TEMPORARY-CAPACITY-V1",
            "fields": {
                "people_capacity": {"unit": "person", "input": "approved EffectProgram"},
                "sleeping_capacity": {"unit": "sleeping_place", "input": "approved EffectProgram"},
                "geometry_status": {"value": "zone_only_capacity"},
            },
            "history": "GET branch/history?kind=population",
            "retention_limit": 20000,
            "external_validation": False,
        },
        GEOMETRY_OVERLAY_KEY: {
            "schema_version": 1, "implementation_id": GEOMETRY_OVERLAY_POLICY,
            "implementation_version": "1", "entity_kind": "system",
            "enabled_by": "geometry_overlay.schema_version=1, 2 or 3",
            "source_id": "ASM-BOUNDED-RUNTIME-GEOMETRY-V1",
            "fields": {
                "facility_points": {"unit": "metre_2d", "limit": 256},
                "partitions": {"unit": "bounded_polygon_metre_2d", "limit": 256},
                "route_modifiers": {"unit": "boolean_with_expiry", "limit": 256},
                "bed_reservations": {
                    "unit": "person_to_sleep_bed_assignment",
                    "limit": 10000,
                    "enabled_by": "geometry_overlay.schema_version=2",
                },
                "room_admissions": {
                    "unit": "resident_to_physical_room_capacity_assignment",
                    "limit": 10000,
                    "enabled_by": "geometry_overlay.schema_version=3",
                },
            },
            "history": "GET branch/history?kind=events or kind=causal",
            "retention_limit": 20000,
            "external_validation": False,
        },
        SHARED_OPERATIONS_KEY: {
            "schema_version": 1, "implementation_id": SHARED_OPERATIONS_POLICY,
            "implementation_version": "1", "entity_kind": "system",
            "enabled_by": "shared_operations.schema_version=1",
            "fields": {
                "support_requests": {"unit": "typed_request_lifecycle", "identity": "synthetic_agent_id"},
                "history": {"unit": "applied_transition_receipt", "time_unit": "simulation_minute"},
            },
            "history_routes": {
                "care": "GET /api/v1/projects/{project}/runs/{run}/branches/{branch}/history?kind=care",
                "operations": "GET /api/v1/projects/{project}/runs/{run}/branches/{branch}/history?kind=operations",
            },
            "retention_limit": 20000,
            "external_validation": False,
        },
        STAFF_DYNAMICS_STATE_KEY: {
            "schema_version": 1,
            "implementation_id": STAFF_DYNAMICS_POLICY_VERSION,
            "implementation_version": "1",
            "entity_kind": "staff",
            "enabled_by": "simulator_extensions.enabled or staff_dynamics config",
            "source_id": STAFF_DYNAMICS_SOURCE_ID,
            "fields": {
                "fatigue": {"unit": "normalized_0_1", "lower": 0, "upper": 1},
                "consecutive_work_minutes": {"unit": "minute"},
                "total_work_minutes": {"unit": "minute"},
                "total_rest_minutes": {"unit": "minute"},
            },
            "external_validation": False,
        },
    }

