"""The update phase: movement, then each person's psychology and social propagation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import KeyedRandom
from sheltersim.core.world import WorldState
from sheltersim.domain.care_receipts import (
    ingest_applied_shared_operation_records,
    make_staff_support_continuity_record,
)
from sheltersim.domain.distress_monitor import record_distress_monitor_tick
from sheltersim.domain.geometry_overlay import release_sleep_bed, reserve_sleep_bed
from sheltersim.domain.models import Activity, SupportRequestStatus
from sheltersim.domain.movement import MOVEMENT_STATE_KEY, advance_movement, finish_movement
from sheltersim.domain.psychological_state import PSYCHOLOGICAL_STATE_KEY
from sheltersim.domain.social_network import record_observed_contact
from sheltersim.events.models import EventKind, EventLogRecord, EventStatus, ScheduledEvent
from sheltersim.policies.psychology import PsychologyUpdateResult, apply_state_deltas
from sheltersim.policies.social import SOCIAL_RUNTIME_STATE_KEY
from sheltersim.policies.space import ZoneRoute
from sheltersim.policies.world_view import WorldView


class UpdatePhase:
    """Steps of TickPhase.UPDATE_AGENT_STATE, a mixin of OfflineSimulationController."""

    def _run_update_phase(self, world: WorldState, minute: int, rng: KeyedRandom) -> None:
        self._update_movement(minute)
        view = self._view_after_movement(world)
        psychology_result = self._psychology_update(view, minute)
        final_deltas = self._social_propagation(world, view, psychology_result.deltas, minute, rng)
        self._record_psychology_trace(psychology_result, final_deltas, minute)
        self._update_long_term_psychology(world, view, final_deltas, minute)
        apply_state_deltas(world, final_deltas)
        self._view_before_proposals = view

    def _view_after_movement(self, world: WorldState) -> WorldView:
        return WorldView.from_world(
            world,
            include_state_hash=False,
            person_cache=self._person_contexts,
            environment_cache=self._zone_environments,
        )

    def _psychology_update(self, view: WorldView, minute: int) -> PsychologyUpdateResult:
        deltas_without_traces = getattr(self.psychology, "deltas_without_traces", None)
        if self.artifacts.trace_level is TraceLevel.OFF and deltas_without_traces is not None:
            # No psychology trace is recorded, so its per-agent parts are not built.
            # The Rust kernel computes the same deltas when it is installed.
            native_deltas = self.psychology.native_deltas(view, minute)
            psychology_result = PsychologyUpdateResult(
                native_deltas if native_deltas is not None else deltas_without_traces(view, minute),
                (),
            )
        else:
            psychology_result = self.psychology.explain_update(view, minute)
        return psychology_result

    def _social_propagation(
        self, world: WorldState, view: WorldView, base_deltas: Any, minute: int, rng: KeyedRandom
    ) -> Any:
        if self.stress_policy.parameters.enabled:
            # The Rust kernel computes the same result when it is installed, and is
            # left out when a trace needs the Python contact records.
            native_update = getattr(self.stress_policy, "native_update", None)
            result = (
                native_update(
                    view,
                    base_deltas,
                    rng,
                    zone_neighbors=self.zone_neighbors,
                    runtime_state=world.model_state.get(SOCIAL_RUNTIME_STATE_KEY),
                )
                if native_update is not None and self.artifacts.trace_level is TraceLevel.OFF
                else None
            )
            if result is None:
                result = self.stress_policy.update(
                    view,
                    base_deltas,
                    rng,
                    zone_neighbors=self.zone_neighbors,
                    runtime_state=world.model_state.get(SOCIAL_RUNTIME_STATE_KEY),
                )
            final_deltas = result.deltas
            world.model_state[SOCIAL_RUNTIME_STATE_KEY] = result.runtime_state
            for contact in result.contacts if "social_network_v1" in world.model_state else ():
                record_observed_contact(
                    world,
                    recipient_id=contact.recipient_id,
                    source_id=contact.source_id,
                    minute=minute,
                )
            if self.artifacts.trace_level is not TraceLevel.OFF:
                self.artifacts.record_social_contact_trace(result.trace_document(minute))
        else:
            final_deltas = base_deltas
        return final_deltas

    def _record_psychology_trace(
        self, psychology_result: PsychologyUpdateResult, final_deltas: Any, minute: int
    ) -> None:
        base_deltas = psychology_result.deltas
        if self.artifacts.trace_level is not TraceLevel.OFF:
            final_by_agent = {delta.agent_id: delta for delta in final_deltas}
            base_by_agent = {delta.agent_id: delta for delta in base_deltas}
            traced_result = replace(
                psychology_result,
                traces=tuple(
                    replace(
                        trace,
                        stress_after=final_by_agent[trace.agent_id].stress,
                        social_stress_delta=(
                            final_by_agent[trace.agent_id].stress
                            - base_by_agent[trace.agent_id].stress
                        ),
                    )
                    for trace in psychology_result.traces
                ),
            )
            self.artifacts.record_psychology_trace(
                traced_result.trace_document(
                    minute,
                    source_id=self.psychology.source_id,
                    full=self.artifacts.trace_level is TraceLevel.FULL,
                ),
                agent_contributions=(
                    {
                        "agent_id": trace.agent_id,
                        "stress_before": trace.stress_before,
                        "stress_after": final_by_agent[trace.agent_id].stress,
                        "raw_stress_delta": trace.raw_stress_delta,
                        "social_stress_delta": (
                            final_by_agent[trace.agent_id].stress
                            - base_by_agent[trace.agent_id].stress
                        ),
                        "component_contributions": dict(trace.component_contributions),
                    }
                    for trace in psychology_result.traces
                ),
            )

    def _update_long_term_psychology(
        self, world: WorldState, view: WorldView, final_deltas: Any, minute: int
    ) -> None:
        update_long_term = getattr(self.psychology, "update_long_term_state", None)
        if callable(update_long_term):
            record_distress_monitor_tick(
                world.metric_state,
                previous_by_agent={
                    agent_id: agent.stress for agent_id, agent in view.agents.items()
                },
                current_by_agent={delta.agent_id: delta.stress for delta in final_deltas},
                threshold=self.psychology.parameters.high_distress_threshold,
            )
            runtime_state = world.model_state.get(PSYCHOLOGICAL_STATE_KEY)
            if not isinstance(runtime_state, dict):
                raise ValueError("mean-reverting psychology runtime state is missing")
            world.model_state[PSYCHOLOGICAL_STATE_KEY] = update_long_term(
                runtime_state, minute, final_deltas
            )

    def _update_movement(self, minute: int) -> None:
        """Advance route interpolation and atomically reschedule changed paths."""
        state = self.world.model_state.get(MOVEMENT_STATE_KEY)
        if state is None:
            return
        for agent_id in advance_movement(self.world, minute):
            journey = dict(state["journeys"].get(agent_id, {}))
            agent = self.world.agents.get(agent_id)
            if not journey or agent is None:
                continue
            old_event = next(
                (
                    event
                    for event in self.world.event_queue.pending()
                    if event.event_id == journey["event_id"]
                ),
                None,
            )
            if old_event is not None:
                self.world.event_queue.remove(old_event.event_id)
                self.world.event_log.append(
                    EventLogRecord(
                        event=old_event,
                        realized_minute=minute,
                        status=EventStatus.SUPPRESSED,
                        detail="the route changed before travel completion",
                    )
                )
            self.world.active_tasks.pop(journey["task_id"], None)
            old_position = state["positions"].get(agent_id, {})
            agent.zone_id = old_position.get("zone_id", agent.zone_id)
            agent.activity = Activity.IDLE
            agent.target_id = None
            # Keep the last applied physical point while planning the replacement path.
            # For a same-zone facility move, the new in-room route therefore begins
            # at the stopped actor position instead of a zone-centre placeholder.
            state["journeys"].pop(agent_id, None)
            target_zone = journey["destination_zone_id"]
            facility_id = journey.get("facility_id")
            if journey.get("purpose") == "staff_support_travel":
                if not self._replan_staff_support_trip(agent_id, journey, minute):
                    self.world.metric_state["movement_replan_blocked_count"] = (
                        self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                    )
                self.world.metric_state["movement_replan_count"] = (
                    self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                )
                continue
            if journey.get("purpose") == "sleep_bed":
                reservation = reserve_sleep_bed(self.world, agent_id)
                if reservation is not None and reservation["travel_minutes"] > 0:
                    route = ZoneRoute(
                        origin_zone_id=agent.zone_id,
                        destination_zone_id=agent.zone_id,
                        zone_ids=(agent.zone_id, agent.zone_id),
                        edge_ids=(),
                        travel_minutes=reservation["travel_minutes"],
                    )
                    scheduled = self._schedule_travel(
                        agent_id=agent.id,
                        route=route,
                        route_points=reservation["route_points"],
                        purpose="sleep_bed",
                        facility_id=None,
                        minute=minute,
                        causation_id=journey["event_id"],
                        validate_state=False,
                    )
                    if scheduled:
                        self.world.metric_state["movement_replan_count"] = (
                            self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                        )
                        continue
                if reservation is None or reservation["travel_minutes"] > 0:
                    self.world.metric_state["movement_replan_blocked_count"] = (
                        self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                    )
                    release_sleep_bed(self.world, agent_id)
                else:
                    state["positions"][agent_id].update(
                        {
                            "zone_id": agent.zone_id,
                            "point": reservation["point"].model_dump(mode="json"),
                            "edge_id": None,
                            "progress": 1.0,
                            "position_kind": "physical",
                        }
                    )
                agent.activity = Activity.SLEEPING
                agent.target_id = None
                self.world.metric_state["movement_replan_count"] = (
                    self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                )
                continue
            if facility_id in self.world.facilities:
                target_zone = self.world.facilities[facility_id].zone_id
                if agent.zone_id == target_zone:
                    room_route = self._facility_approach(agent_id, facility_id)
                    if room_route is not None and room_route[1]:
                        if room_route[0].travel_minutes > 0 and len(room_route[1]) > 1:
                            self._schedule_travel(
                                agent_id=agent_id,
                                route=room_route[0],
                                route_points=room_route[1],
                                purpose=journey["purpose"],
                                facility_id=facility_id,
                                minute=minute,
                                causation_id=journey["event_id"],
                                validate_state=False,
                            )
                        else:
                            state["positions"][agent_id] = {
                                "zone_id": agent.zone_id,
                                "point": room_route[1][-1].model_dump(mode="json"),
                                "edge_id": None,
                                "progress": 0.0,
                                "position_kind": "physical",
                            }
                    else:
                        self.world.metric_state["movement_replan_blocked_count"] = (
                            self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                        )
                    self.world.metric_state["movement_replan_count"] = (
                        self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
                    )
                    continue
            route = self._route(agent.zone_id, target_zone, agent_id)
            if route is not None and route.edge_ids:
                self._schedule_travel(
                    agent_id=agent_id,
                    route=route,
                    purpose=journey["purpose"],
                    facility_id=facility_id,
                    minute=minute,
                    causation_id=journey["event_id"],
                    validate_state=False,
                )
            else:
                self.world.metric_state["movement_replan_blocked_count"] = (
                    self.world.metric_state.get("movement_replan_blocked_count", 0.0) + 1.0
                )
            self.world.metric_state["movement_replan_count"] = (
                self.world.metric_state.get("movement_replan_count", 0.0) + 1.0
            )

    def _replan_staff_support_trip(
        self, agent_id: str, journey: dict[str, Any], minute: int
    ) -> bool:
        """Re-route an assigned resident or requeue support if its route closed."""
        world = self.world
        request_id = journey.get("support_request_id")
        request = world.support_requests.get(request_id) if isinstance(request_id, str) else None
        agent = world.agents.get(agent_id)
        staff = world.staff.get(request.assigned_staff_id or "") if request is not None else None
        task = world.active_tasks.get(request.task_id or "") if request is not None else None
        if (
            request is None
            or request.status is not SupportRequestStatus.ASSIGNED
            or agent is None
            or staff is None
            or task is None
            or task.kind != "staff_support"
        ):
            raise ValueError("replanned resident trip lost its assigned support task")
        completions = [
            event
            for event in world.event_queue.pending()
            if event.event_type == "staff_support_complete"
            and event.payload.get("task_id") == task.id
        ]
        if len(completions) != 1 or completions[0].due_minute != task.due_minute:
            raise ValueError("replanned resident trip has inconsistent support completion")
        previous_completion = completions[0]
        service_minutes = task.due_minute - task.start_minute
        route = self._route(agent.zone_id, staff.zone_id, agent.id)
        due_minute = minute + (route.travel_minutes if route is not None else 0) + service_minutes
        can_schedule = route is not None and due_minute < world.duration_minutes
        if can_schedule and route is not None and route.travel_minutes > 0:
            can_schedule = self._schedule_travel(
                agent_id=agent.id,
                route=route,
                purpose="staff_support_travel",
                facility_id=None,
                minute=minute,
                causation_id=journey.get("event_id"),
                support_request_id=request.id,
                support_staff_id=staff.id,
                validate_state=False,
            )
        elif can_schedule:
            agent.activity = Activity.SEEKING_HELP
            agent.target_id = None
        if can_schedule:
            world.event_queue.remove(previous_completion.event_id)
            world.event_log.append(
                EventLogRecord(
                    event=previous_completion,
                    realized_minute=minute,
                    status=EventStatus.SUPPRESSED,
                    detail="support completion was rescheduled after a route change",
                )
            )
            sequence = world.allocate_event_sequence()
            task.start_minute = minute + (route.travel_minutes if route is not None else 0)
            task.due_minute = due_minute
            staff.busy_until = due_minute
            world.schedule_event(
                ScheduledEvent(
                    event_id=f"staff-support-complete-{sequence}",
                    kind=EventKind.COMPLETION,
                    due_minute=due_minute,
                    priority=0,
                    sequence=sequence,
                    event_type="staff_support_complete",
                    payload={
                        "request_id": request.id,
                        "agent_id": agent.id,
                        "staff_id": staff.id,
                        "task_id": task.id,
                        "support_duration_minutes": service_minutes,
                    },
                    source_id=task.id,
                    causation_id=journey.get("event_id"),
                    provenance={
                        "source_type": "assumption",
                        "source_id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
                        "policy_version": self.staff_service.policy_version,
                        "rescheduled_after_route_change": True,
                    },
                )
            )
            return True

        world.event_queue.remove(previous_completion.event_id)
        world.event_log.append(
            EventLogRecord(
                event=previous_completion,
                realized_minute=minute,
                status=EventStatus.SUPPRESSED,
                detail="support completion was cancelled because the staff route is unavailable",
            )
        )
        del world.active_tasks[task.id]
        staff.work_queue.clear()
        staff.active_task_id = None
        staff.busy_until = 0
        request.assigned_staff_id = None
        request.task_id = None
        agent.target_id = None
        if request.timeout_minute <= minute:
            request.status = SupportRequestStatus.UNRESOLVED
            request.resolved_minute = minute
            request.cooldown_until = minute + self.staff_service.cooldown_minutes
            agent.activity = Activity.IDLE
            agent.support_request_id = None
            agent.support_cooldown_until = request.cooldown_until
            world.metric_state["support_unresolved_count"] = (
                world.metric_state.get("support_unresolved_count", 0.0) + 1.0
            )
            continuity_status = "unresolved"
        else:
            request.status = SupportRequestStatus.PENDING
            agent.activity = Activity.SEEKING_HELP
            agent.support_request_id = request.id
            continuity_status = "requeued"
        continuity = make_staff_support_continuity_record(
            world,
            request_id=request.id,
            agent_id=agent.id,
            task_id=task.id,
            previous_staff_id=staff.id,
            status=continuity_status,
            causation_id=journey.get("event_id") or task.id,
            cause_type="route_unreachable",
        )
        world.event_log.append(continuity)
        ingest_applied_shared_operation_records(world, (continuity,))
        finish_movement(world, agent.id)
        return False
