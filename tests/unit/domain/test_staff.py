"""Support requests record why they wait for a qualified worker."""

from __future__ import annotations

from sheltersim.core.world import WorldState
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.staff import StaffService, support_wait_reason_codes


def test_waiting_for_an_absent_worker_counts_as_waiting_for_someone_not_working(
    generated_scenario_with,
) -> None:
    # Only Scenarios of four days or more hold staff absences (#65).
    world = WorldState.from_scenario(generated_scenario_with(duration_days=4))
    # staff_001 is the only support worker. Marking them away stands in for an
    # applied absence event, so the state checks that compare it with the event
    # history are skipped; test_a_staff_absence_shows_in_the_support_wait_reasons
    # runs the real intervention.
    world.model_state[OPERATIONAL_EVENTS_KEY]["staff_status"]["staff_001"]["available"] = False
    service = StaffService(world, task_duration_minutes=10, unresolved_after_minutes=30)

    request = service.request("A001", validate_state=False)

    assert service.assign_pending(0, validate_state=False) == ()
    assert support_wait_reason_codes(world, request.request_id) == ("staff_unavailable",)
    # Before #74 this wait was counted under no reason at all.
    assert world.metric_state["support_wait_staff_off_shift_request_minutes"] == 1.0
    assert "support_wait_staff_busy_request_minutes" not in world.metric_state
