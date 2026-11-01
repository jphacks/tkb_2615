"""The arbitrate phase: services and staff take up what the proposals asked for."""

from __future__ import annotations

from typing import Any

from sheltersim.core.rng import KeyedRandom
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    append_applied_care_transition,
    care_receipt_for_request,
    care_source_ids_for_agent,
)
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.staff_dynamics import update_staff_dynamics
from sheltersim.domain.staff_operations import (
    assign_staff_operations,
    schedule_staff_operation_relief,
    update_staff_operations,
)


class ArbitratePhase:
    """Steps of TickPhase.ARBITRATE_ACTIONS, a mixin of OfflineSimulationController."""

    def _run_arbitrate_phase(self, world: WorldState, minute: int, rng: KeyedRandom) -> None:
        update_staff_dynamics(world, minute)
        update_staff_operations(world, minute)
        self._arbitrate_services(minute, rng)
        self._assign_staff_support(world, minute)
        # Reserve eligible staff for direct occupant support before background patrol,
        # outreach, sanitation and maintenance work consumes the remaining staff.
        assign_staff_operations(world, minute, self.staff_space)
        self._consider_due_complaints(minute)
        schedule_staff_operation_relief(world, minute)

    def _arbitrate_services(self, minute: int, rng: KeyedRandom) -> None:
        self.toilet_service.arbitrate(minute, rng, validate_state=False)
        if self.resource_service is not None:
            self.resource_service.arbitrate(minute, rng, validate_state=False)

    def _assign_staff_support(self, world: WorldState, minute: int) -> None:
        self._record_assigned_support(
            world, self.staff_service.assign_pending(minute, validate_state=False)
        )

    def _record_assigned_support(self, world: WorldState, assigned_requests: Any) -> None:
        for request_id in assigned_requests:
            prior = care_receipt_for_request(world, request_id)
            request = world.support_requests.get(request_id)
            if prior is None or request is None or request.assigned_staff_id is None:
                continue
            # Recorded with the request's household, not the current one (#179).
            append_applied_care_transition(
                world,
                request_id=request.id,
                agent_id=request.agent_id,
                household_id=prior.get("household_id"),
                support_kind=prior["support_kind"],
                status="assigned",
                staff_id=request.assigned_staff_id,
                causation_id=request.task_id,
                source_ids=care_source_ids_for_agent(world, request.agent_id),
            )
            if world.agents[request.agent_id].activity is Activity.SEEKING_HELP:
                append_applied_care_transition(
                    world,
                    request_id=request.id,
                    agent_id=request.agent_id,
                    household_id=prior.get("household_id"),
                    support_kind=prior["support_kind"],
                    status="started",
                    staff_id=request.assigned_staff_id,
                    causation_id=request.task_id,
                    source_ids=care_source_ids_for_agent(world, request.agent_id),
                )

    def _consider_due_complaints(self, minute: int) -> None:
        """Evaluate one request exactly when it reaches the modeled wait threshold."""
        if self.social_policy is None:
            return
        threshold = self.social_policy.limits.complaint_wait_minutes
        # A complaint decision does not make a request pending, so only requests pending
        # now can qualify below.
        due = [
            (request.id, request)
            for request in self.world.unsettled_support_requests()
            if request.status is SupportRequestStatus.PENDING
            and request.requested_minute + threshold == minute
        ]
        for request_id, request in sorted(due):
            if (
                request.status is SupportRequestStatus.PENDING
                and request.requested_minute + threshold == minute
            ):
                self.social_policy.consider_complaint_response(
                    self.world,
                    agent_id=request.agent_id,
                    request_id=request_id,
                )
