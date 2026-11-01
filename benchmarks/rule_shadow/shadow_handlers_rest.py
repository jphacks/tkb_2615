"""Compare the rest of events/handlers.py's kernels with their Python rules at each call.

Handlers that copy the world and rare in-place ones: each call runs the Python rule on one copy of
the world and the kernel on another, and compares their outcomes and canonical documents as JSON
text, in which a float keeps its bits and 1 differs from 1.0. Every row must write the bytes of its
tree on the kernel's copy. Then the kernel runs on the real world.

A refusal of what the Python rule applied is a difference: the world's validation can turn down a
wrong result, and the Python rule then quietly gets it right, which no hash would ever see.

Also here: the other social events (`complaint_response`, `cooperation`, `information_reaction`, an
untyped one), the walks to a bed and of a relocation, `_reject_room_approach`, `ingest`, and which
requests `_consider_due_complaints` asks the policy about.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from types import SimpleNamespace
from typing import Any

from shadow_effects import report_stats
from shadow_kit import Recorder, text, values_in

HANDLERS = (
    ("handlers", "proximity", "ProximityContactHandler"),
    ("information_events", "rumor", "InformationRumorHandler"),
    ("information_events", "correction", "InformationCorrectionHandler"),
    ("information_events", "contact", "InformationContactHandler"),
    ("handlers", "resource_ledger", "ResourceLedgerHandler"),
    ("handlers", "facility_outage", "FacilityOutageHandler"),
    ("handlers", "facility_recovery", "FacilityRecoveryHandler"),
    ("handlers", "infrastructure", "InfrastructureEventHandler"),
    ("handlers", "service_cancellation", "ResourceServiceCancellationHandler"),
    ("handlers", "population", "EffectProgramPopulationHandler"),
    ("handlers", "delivery_delay", "DeliveryDelayHandler"),
    ("handlers", "water_outage", "WaterOutageHandler"),
    ("handlers", "water_restoration", "WaterRestorationHandler"),
    ("handlers", "staff_unavailable", "StaffUnavailabilityHandler"),
    ("handlers", "staff_restoration", "StaffRestorationHandler"),
    ("staff_operation_events", "arrive", "StaffOperationArrivalHandler"),
    ("staff_operation_events", "relief", "StaffOperationReliefHandler"),
    ("staff_operation_events", "complete", "StaffOperationCompletionHandler"),
)
SOCIAL_KINDS = frozenset({"complaint_response", "cooperation", "information_reaction", None})
WALK_KINDS = frozenset({"sleep_bed", "presence_relocation"})
RECEIPT_TYPES = frozenset(
    {
        "care_support_transition",
        "staff_support_continuity",
        "facility_recovery_transition",
        "staff_handoff",
        "staff_operation_transition",
    }
)


def _outcome(call: Any) -> tuple[str, Any]:
    from sheltersim.events.models import EventApplicationError, EventSuppressed

    try:
        result = call()
    except EventSuppressed as raised:
        return "suppressed", str(raised)
    except EventApplicationError as raised:
        return "failed", str(raised)
    except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
        return type(raised).__name__, str(raised)
    if isinstance(result, tuple):
        return "applied", [getattr(item, "to_dict", lambda item=item: item)() for item in result]
    return "applied", result


def _worlds(world: Any) -> tuple[Any, Any]:
    """Two copies. A world in the middle of a phase need not pass `to_dict`'s validation."""
    from sheltersim.core.world import StateInvariantError, WorldState

    try:
        document = world.to_dict()
    except StateInvariantError:
        return copy.deepcopy(world), copy.deepcopy(world)
    return WorldState.from_dict(document), WorldState.from_dict(document)


def compare_worlds(recorder: Recorder, label: str, python_world: Any, kernel_world: Any) -> None:
    # Without the world's validation: a world in the middle of a phase need not pass it.
    python_after = json.loads(python_world._canonical_text(python_world._canonical_data()))
    kernel_after = json.loads(kernel_world._canonical_text(kernel_world._canonical_data()))
    if text(python_after) != text(kernel_after):
        for key, value in python_after.items():
            if text(value) != text(kernel_after.get(key)):
                recorder.differ(f"{label}: {key} differs")
    else:
        recorder.seen[f"{label}.values_compared"] += values_in(python_after)
    for name, mirror in kernel_world.model_state._mirrors.items():
        if name in kernel_world.model_state and not mirror.verify():
            recorder.differ(f"{label}: the row of {name} is not its tree")


def _on(handler: Any, world: Any) -> Any:
    """A handler with a service manager gets the manager of this copy of the world."""
    changes = {}
    for name in ("service", "resource_service", "toilet_service"):
        manager = getattr(handler, name, None)
        if manager is not None:
            copied = copy.copy(manager)
            copied.world = world
            changes[name] = copied
    return dataclasses.replace(handler, **changes) if changes else handler


def _shadow(recorder: Recorder, label_of: Any, kernel: Any, rule: Any) -> Any:
    """`kernel(handler, world, event, *rest)` and `rule()(handler, copy, event, *rest)`."""

    def shadow(handler: Any, world: Any, event: Any, *rest: Any) -> Any:
        label = label_of(event)
        if label is None:
            return kernel(handler, world, event, *rest)
        recorder.seen[f"{label}.calls"] += 1
        try:
            python_world, kernel_world = _worlds(world)
        except Exception as raised:  # noqa: BLE001 - both rules copy the world first
            recorder.reasons[f"{label}: the copy raised {type(raised).__name__}"] += 1
            return kernel(handler, world, event, *rest)
        expected = _outcome(lambda: rule()(_on(handler, python_world), python_world, event, *rest))
        actual = _outcome(lambda: kernel(_on(handler, kernel_world), kernel_world, event, *rest))
        if actual[0] == "Unsupported":
            recorder.reasons[f"{label}: {actual[1]}"] += 1
            if expected[0] == "applied":
                recorder.differ(f"{label}: refused what the Python rule applied: {actual[1]}")
                recorder.seen[f"{label}.refused_a_success"] += 1
            else:
                recorder.seen[f"{label}.refused_{expected[0]}"] += 1
        else:
            recorder.seen[f"{label}.kernel_ran"] += 1
            if text(actual) != text(expected):
                recorder.differ(f"{label}: {actual!r:.300} is not {expected!r:.300}")
            elif actual[0] == "applied":
                compare_worlds(recorder, label, python_world, kernel_world)
        return kernel(handler, world, event, *rest)

    return shadow


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "handlers_rest")
    report_stats(recorder, native.rules("handlers"))
    if not _comparing():
        return

    def handlers() -> Any:
        from sheltersim.events import handlers as loaded

        return loaded

    for module, name, class_name in HANDLERS:
        rules = native.rules(module)
        setattr(
            rules,
            name,
            _shadow(
                recorder,
                lambda event, name=name: f"{name}.{event.event_type}",
                getattr(rules, name),
                lambda class_name=class_name: getattr(handlers(), class_name).apply.__wrapped__,
            ),
        )

    social = native.rules("social_interaction")
    social.apply = _shadow(
        recorder,
        lambda event: (
            f"social.{event.payload.get('interaction_type')}"
            if event.payload.get("interaction_type") in SOCIAL_KINDS
            else None
        ),
        social.apply,
        lambda: handlers().SocialInteractionHandler.apply.__wrapped__,
    )
    walks = native.rules("travel_completion")
    walks.apply = _shadow(
        recorder,
        lambda event: (
            f"walk.{event.payload.get('purpose')}"
            if event.payload.get("purpose") in WALK_KINDS
            else None
        ),
        walks.apply,
        lambda: handlers().AgentTravelCompletionHandler.apply.__wrapped__,
    )
    walks.reject_room_approach = _shadow(
        recorder,
        lambda event: "reject_room_approach",
        walks.reject_room_approach,
        lambda: handlers().AgentTravelCompletionHandler._reject_room_approach.__wrapped__,
    )

    receipts = native.rules("care_receipts")
    ingest_kernel = receipts.ingest

    def ingest(world: Any, outcomes: Any) -> Any:
        from sheltersim.domain import care_receipts

        if not any(
            getattr(getattr(outcome, "event", None), "event_type", None) in RECEIPT_TYPES
            for outcome in outcomes
        ):
            return ingest_kernel(world, outcomes)
        recorder.seen["ingest.calls"] += 1
        try:
            python_world, kernel_world = _worlds(world)
        except Exception as raised:  # noqa: BLE001 - a world in the middle of a phase
            recorder.reasons[f"ingest: the copy raised {type(raised).__name__}"] += 1
            return ingest_kernel(world, outcomes)
        rule = care_receipts.ingest_applied_shared_operation_records.__wrapped__
        expected = _outcome(lambda: rule(python_world, outcomes))
        actual = _outcome(lambda: ingest_kernel(kernel_world, outcomes))
        if actual[0] == "Unsupported":
            recorder.reasons[f"ingest: {actual[1]}"] += 1
            if expected[0] == "applied":
                recorder.differ(f"ingest: refused what the Python rule applied: {actual[1]}")
        else:
            recorder.seen["ingest.kernel_ran"] += 1
            if text(actual) != text(expected):
                recorder.differ(f"ingest: {actual!r:.300} is not {expected!r:.300}")
            else:
                compare_worlds(recorder, "ingest", python_world, kernel_world)
        return ingest_kernel(world, outcomes)

    receipts.ingest = ingest

    staff = native.rules("staff")
    complaints_kernel = staff.consider_due_complaints

    def consider_due_complaints(controller: Any, minute: Any) -> Any:
        from sheltersim.experiments.phases.arbitrate import ArbitratePhase

        policy = controller.social_policy
        if policy is None:
            return complaints_kernel(controller, minute)
        recorder.seen["complaints.calls"] += 1
        expected: list[tuple[str, str]] = []

        def note(_world: Any, *, agent_id: str, request_id: str) -> None:
            expected.append((agent_id, request_id))

        ArbitratePhase._consider_due_complaints(
            SimpleNamespace(
                world=controller.world,
                social_policy=SimpleNamespace(
                    limits=policy.limits, consider_complaint_response=note
                ),
            ),
            minute,
        )
        actual: list[tuple[str, str]] = []

        def forward(world: Any, *, agent_id: str, request_id: str) -> Any:
            actual.append((agent_id, request_id))
            return policy.consider_complaint_response(
                world, agent_id=agent_id, request_id=request_id
            )

        result = complaints_kernel(
            SimpleNamespace(
                world=controller.world,
                social_policy=SimpleNamespace(
                    limits=policy.limits, consider_complaint_response=forward
                ),
            ),
            minute,
        )
        recorder.seen["complaints.kernel_ran"] += 1
        recorder.seen["complaints.asked"] += len(actual)
        # A decision may settle a later request of the minute, which the note above cannot see.
        if actual != expected[: len(actual)] or any(pair not in expected for pair in actual):
            recorder.differ(f"complaints at {minute}: {actual!r} and {expected!r}")
        return result

    staff.consider_due_complaints = consider_due_complaints

    work_kernel = staff.assign_work

    def assign_work(service: Any, minute: Any, *, validate_state: bool = True) -> Any:
        recorder.seen["assign_work.calls"] += 1
        try:
            python_world, kernel_world = _worlds(service.world)
        except Exception as raised:  # noqa: BLE001 - a world in the middle of a phase
            recorder.reasons[f"assign_work: the copy raised {type(raised).__name__}"] += 1
            return work_kernel(service, minute, validate_state=validate_state)

        def twin(world: Any) -> Any:
            copied = copy.copy(service)
            copied.world = world
            for name in ("route_resolver", "travel_scheduler"):
                bound = getattr(service, name)
                owner = getattr(bound, "__self__", None)
                if owner is None:
                    continue
                controller = copy.copy(owner)
                controller.world = world
                for key, value in vars(owner).items():
                    if isinstance(value, (dict, list, set)):
                        setattr(controller, key, copy.copy(value))
                setattr(copied, name, getattr(controller, bound.__name__))
            return copied

        from sheltersim.domain.staff import StaffService

        rule = StaffService.assign_pending.__wrapped__
        expected = _outcome(lambda: rule(twin(python_world), minute, validate_state=validate_state))
        actual = _outcome(
            lambda: work_kernel(twin(kernel_world), minute, validate_state=validate_state)
        )
        if actual[0] == "Unsupported":
            recorder.reasons[f"assign_work: {actual[1]}"] += 1
            if expected[0] == "applied":
                recorder.differ(f"assign_work: refused what the Python rule applied: {actual[1]}")
        else:
            recorder.seen["assign_work.kernel_ran"] += 1
            if actual[0] == "applied":
                recorder.seen["assign_work.assigned"] += len(actual[1])
            if text(actual) != text(expected):
                recorder.differ(f"assign_work: {actual!r:.300} is not {expected!r:.300}")
            elif actual[0] == "applied":
                compare_worlds(recorder, "assign_work", python_world, kernel_world)
        return work_kernel(service, minute, validate_state=validate_state)

    staff.assign_work = assign_work


def _comparing() -> bool:
    import os

    return os.environ.get("SHELTERSIM_DISPATCH_COUNT_ONLY") != "1"
