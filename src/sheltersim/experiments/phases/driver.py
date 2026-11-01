"""The tick's phases in Rust: native/src/rules/tick.rs.

`TickEngine.step` asks a handler's `run_tick` first. The driver refuses a tick that a hook watches
or changes, before anything has happened, and `step` then runs the phases through `phase_handler`,
so a test's patch, a trace, a Provider or a tick_probe all see what they saw before. Without the
Rust kernels the controller has no `run_tick`. See docs/rust-port/lanes/m6-driver.md.
"""

from __future__ import annotations

from typing import Any

from sheltersim import native
from sheltersim.core.engine import TICK_PHASE_ORDER, TickExecutionError
from sheltersim.core.rng import KeyedRandom
from sheltersim.core.world import WorldState
from sheltersim.domain import facilities, population_lifecycle, resource_service, staff
from sheltersim.experiments.phases import arbitrate, dispatch, update

# The controller's steps the driver calls no entry of, or that it stands for. A patch of one sends
# the tick through `phase_handler`. A step that the driver calls through Python is not here.
DRIVEN_STEPS = (
    "__call__",
    "_run_phase",
    "_run_dispatch_phase",
    "_run_update_phase",
    "_update_movement",
    "_view_after_movement",
    "_psychology_update",
    "_social_propagation",
    "_record_psychology_trace",
    "_update_long_term_psychology",
    "_apply_state_deltas",
    "_run_propose_phase",
    "_view_for_proposals",
    "_apply_due_proposals",
    "_propose_due",
    "_observed_state",
    "_apply_proposal",
    "_apply_effect",
    "_record_outcome",
    "_run_arbitrate_phase",
    "_arbitrate_services",
    "_assign_staff_support",
    "_run_sample_phase",
    "_sample_information_contacts",
    "_run_metrics_phase",
    "_admit_pending_returns",
)

# What those steps call, which the driver calls the kernels of.
DRIVEN_RULES = (
    (update, "advance_movement"),
    (arbitrate, "update_staff_dynamics"),
    (arbitrate, "update_staff_operations"),
    (arbitrate, "assign_staff_operations"),
    (arbitrate, "schedule_staff_operation_relief"),
    (dispatch, "admit_pending_arrivals"),
    (population_lifecycle, "admit_pending_returns"),
    (facilities.ToiletServiceManager, "arbitrate"),
    (facilities.ToiletServiceManager, "_facilities_to_serve"),
    (facilities.ToiletServiceManager, "_has_room"),
    (facilities.ToiletServiceManager, "_start_head"),
    (facilities, "prepare_facility_queue"),
    (facilities, "progress_physical_queue_positions"),
    (facilities, "progress_physical_queue_head_to_service"),
    (facilities, "facility_has_independent_queues"),
    (resource_service.ResourceServiceManager, "arbitrate"),
    (resource_service.ResourceServiceManager, "_facilities_to_serve"),
    (resource_service.ResourceServiceManager, "_serve_head"),
    (resource_service, "prepare_facility_queue"),
    (resource_service, "progress_physical_queue_positions"),
    (resource_service, "facility_has_independent_queues"),
    (staff.StaffService, "assign_pending"),
)


def install_driver(controller: type) -> None:
    """Give this class `run_tick`, once its segments are installed."""
    if not native.available():
        return
    rules = native.rules("tick")
    rules.install(
        controller,
        (
            facilities.ToiletServiceManager,
            resource_service.ResourceServiceManager,
            staff.StaffService,
        ),
        list(DRIVEN_STEPS),
        list(DRIVEN_RULES),
        list(TICK_PHASE_ORDER),
        (arbitrate, update, facilities),
        TickExecutionError,
    )
    run_kernel = rules.run_tick
    unsupported = native.Unsupported

    def run_tick(
        self: Any, world: WorldState, minute: int, rng: KeyedRandom, observer: Any
    ) -> bool:
        """One tick in Rust, or false with nothing changed."""
        self._refresh_config_scopes(world, minute)
        first, second, third = self._config_scopes()
        with first, second, third:
            try:
                run_kernel(self, world, minute, rng, observer)
            except unsupported:
                return False
        return True

    controller.run_tick = run_tick  # type: ignore[attr-defined]
