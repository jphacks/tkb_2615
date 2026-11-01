"""The `python_rules` mark: a test that runs with the Python rules instead of the Rust kernels.

A kernel doesn't call the Python functions it replaces, so a test that patches one of them would
patch nothing. `@pytest.mark.python_rules` removes the kernels of experiments/phases/segments.py
and the validators of core/state_mirror.py for that test. Python functions then run, on the same
rows and mirrors. Patches don't reach a spawned worker of `ProjectAPI`, so neither does this mark.
Without the kernels (`SHELTERSIM_NATIVE=0`) it changes nothing.

tests/unit/repo/test_python_rules_lint.py fails when a name below is patched without the mark.
A lane adds the names of the Python functions its kernel stops calling.
"""

from __future__ import annotations

import pytest

# Names that mean a ported function wherever they are patched.
PORTED_NAMES = frozenset(
    {
        # domain/facility_queues.py: rules/facility_queues.rs.
        "independent_queues_required",
        "is_legacy_queue_checkpoint",
        "create_facility_queue_state",
        "choose_queue_id",
        "queue_ids_for_facility",
        "facility_has_independent_queues",
        "queue_head_agent_ids",
        "request_for_task",
        "request_for_agent",
        "queue_agent_ids",
        "enqueue",
        "start_service",
        "finish_service",
        "mark_stall",
        "record_queue_transition",
        "validate_facility_queue_state",
        # domain/queueing.py: rules/queues.rs. domain/resource_service.py: rules/service.rs.
        "prepare_independent_facility_queue",
        "_serve_independent_head",
        "_record_independent_queue_stall",
        # domain/temporary_spaces.py, domain/geometry_overlay.py: rules/overlay.rs.
        "capacity_metrics",
        "effective_zone_capacity",
        "effective_total_zone_capacity",
        "incoming_travelers",
        "register_temporary_space",
        "expire_temporary_space",
        "validate_temporary_spaces_state",
        "reserve_sleep_bed",
        "release_sleep_bed",
        "available_temporary_room",
        "admit_to_temporary_room",
        "release_temporary_room_admission",
        "room_admission_point",
        "installed_facility_placement_enabled",
        "installed_facility_point",
        "place_installed_facility",
        "validate_geometry_change",
        "apply_geometry_change",
        "validate_geometry_overlay_state",
        # events/effects.py: rules/effects.rs, through `kernel_first`.
        "should_apply_effect_guard",
        "has_active_facility_modifier",
        "has_pending_facility_modifier",
        "validate_effect_program_state",
        # domain/movement.py: rules/movement.rs. Inside `advance_movement` a patch reaches only
        # its Python rule, `advance_movement.__wrapped__`, which a test calls by that name.
        "advance_movement",
        "start_movement",
        "finish_movement",
        "route_with_movement_time",
        "route_is_physically_clear",
        "plan_in_room_route",
        "facility_approach_route",
        "_plan_in_room_route",
        "_queue_slot_position",
        "_set_stationary_position",
        "_room_path_is_clear",
        "_route_point",
        "_point_along_polyline",
        "_reserved_bed_point",
        "_facility_move_invalidated_journey",
        "_zone_center",
        # domain/staff.py, domain/staff_operations.py, experiments/metrics.py: rules/staff.rs.
        # The mark runs `_support_wait_snapshot`'s Python rule; the others have kernels of
        # `kernel_first` that a test reaches through `__wrapped__`.
        "support_wait_reason_codes",
        "support_waited_minutes",
        "support_required_skills",
        "_support_kind",
        "_support_priority",
        "_support_assignment_order",
        "_route_candidates",
        "_support_route_wait_reasons",
        # domain/common_operations.py: rules/common_operations.rs, likewise.
        "_event_payload_matches",
        "_validate_observed_state_changes",
        # experiments/metrics.py: rules/metrics.rs. core/world.py: rules/tick_validation.rs.
        "_snapshot_fields",
        "_validate_entity_rules",
        "_validate_event_rules",
        # policies/world_view.py: rules/world_view.rs
        "_view_state_hash",
        "_zone_environment",
        "_parsed_person_contexts",
        "_person_fields",
        "active_environmental_stress",
        "infrastructure_event_context_ids",
        "population_context_people",
        # policies/behavior.py, experiments/phases/propose.py: rules/behavior.rs
        "propose_due",
        "_candidate",
        "_demand_trigger",
        "_decision_interval_minutes",
        "_sleep_debt_utility",
        "_target_for_action",
        "_agent_view_snapshot",
        # policies/psychology.py, domain/distress_monitor.py, domain/psychological_state.py:
        # rules/psychology.rs
        "deltas_without_traces",
        "explain_update",
        "apply_state_deltas",
        "update_long_term_state",
        "record_distress_monitor_tick",
        "update_psychological_state",
        # policies/social.py, policies/alternatives.py: rules/social_propagation.rs
        "_contact_buckets",
        "_select_contacts",
        # policies/information.py: rules/information.rs
        "_transmission_probability",
        "_sender_sharing_factor",
        # experiments/phases/dispatch.py, the rules it calls: rules/dispatch.rs. An event type's
        # handler is read at each event, and a patch of a class's `apply` shows there.
        "_dispatch_due_events",
        "_record_applied_outcomes",
        "_refresh_space_after_geometry_change",
        "_interrupt_trips_on_closed_routes",
        "_consider_social_contact_events",
        "effect_event_guard_or_suppress",
        "should_apply_effect_guard",
        "record_social_event",
        "observe_event",
        "record_applied_action_completion",
        "learn_facility",
        # events/handlers.py, domain/care_receipts.py, domain/facilities.py, domain/staff.py:
        # rules/care_receipts.rs, rules/toilet.rs, rules/support.rs, rules/travel_completion.rs
        "append_applied_care_transition",
        "make_care_transition_record",
        "care_source_ids_for_agent",
        "support_kind_for_agent",
        "complete_toilet_service",
        "timeout_support_request",
        "complete_support_task",
        "_finish_stopped_journey",
        "_schedule_room_approach",
        "_reject_room_approach",
        # domain/resources.py: rules/resources.rs, state/resource_ledger.rs
        "apply_resource_operation",
        "resource_metric_values",
        "_validate_audit_record",
        # domain/population.py, domain/population_lifecycle.py: rules/population.rs,
        # rules/population_lifecycle.rs, of `kernel_first`: a test reaches a rule by `__wrapped__`.
        "queue_arrivals",
        "queue_departures",
        "admit_pending_arrivals",
        "admit_pending_returns",
        "apply_arrival_approach_completion",
        "complete_presence_relocation",
        "schedule_population_retry",
        "current_household_id",
        "validate_population_dynamics_state",
        "validate_population_lifecycle_state",
        "_zone_has_room",
        "_choose_admission_target",
        "_apply_presence_action",
        "_admit_profile",
        "_depart_agent",
        "_archive_person",
        "_restore_person",
        "_schedule_relocation",
        "_set_presence",
        # experiments/phases/propose.py, policies/space.py: rules/proposals.rs, rules/travel.rs
        "reserve_sleep_bed",
        "release_sleep_bed",
        "capture_social_state_context",
    }
)

# Names that mean a ported function only on this class, the last part of what is patched.
PORTED_METHODS = frozenset(
    {
        ("WorldView", "from_world"),
        ("WorldView", "after_state_update"),
        ("_IndexedMetricSink", "record_decision"),
        ("UtilityBehaviorPolicy", "propose"),
        ("PriorityRuleBehaviorPolicy", "propose"),
        ("StressPropagationPolicy", "update"),
        ("ExpressivenessChannelOpennessPolicy", "update"),
        ("DeGrootContactPolicy", "update"),
        ("InformationSpreadPolicy", "update"),
        ("HazardInformationPolicy", "update"),
        ("EvidenceAwareInformationPolicy", "update"),
        ("EventDispatcher", "dispatch_due"),
        ("EventDispatcher", "settle"),
        ("ResourceServiceManager", "_serve_head"),
        ("ResourceServiceManager", "_facilities_to_serve"),
        ("ResourceServiceManager", "complete"),
        ("ResourceServiceManager", "request"),
        ("ResourceServiceManager", "record_request_outcome"),
        ("ShortestTravelTimePolicy", "route"),
        ("ToiletServiceManager", "request"),
        ("ToiletServiceManager", "_facilities_to_serve"),
        ("ToiletServiceManager", "_has_room"),
        ("ToiletServiceManager", "_start_head"),
        ("StaffService", "request"),
    }
)


def segment_steps() -> frozenset[str]:
    """The steps a kernel replaces: patching one on the controller replaces the kernel too."""
    from sheltersim.experiments.phases.segments import SEGMENTS

    return frozenset(SEGMENTS)


@pytest.fixture(autouse=True)
def _python_rules(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("python_rules") is None:
        return
    from sheltersim import native
    from sheltersim.core import state_mirror
    from sheltersim.core import world as world_module
    from sheltersim.experiments import metrics
    from sheltersim.experiments.phases.segments import SEGMENTS
    from sheltersim.experiments.runner import OfflineSimulationController

    if not native.available():
        return
    for step in SEGMENTS:
        # install_segments set it on this class, and the phases' mixins keep the Python step.
        monkeypatch.delattr(OfflineSimulationController, step)
    monkeypatch.setattr(state_mirror, "_RUST_VALIDATORS", {})
    monkeypatch.setattr(
        metrics, "_support_wait_snapshot", metrics._support_wait_snapshot.__wrapped__
    )
    monkeypatch.setattr(metrics, "_snapshot_fields", metrics._snapshot_fields.__wrapped__)
    monkeypatch.setattr(world_module, "_entity_rules_kernel", None)
