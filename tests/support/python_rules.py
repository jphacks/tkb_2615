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
