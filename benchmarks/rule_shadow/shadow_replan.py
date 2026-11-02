"""Compare `replan_journeys` of native/src/rules/replan.rs with `_replan_journeys` at each call.

The Python step runs on a whole copy of the world, with a controller of its own, before the kernel
runs on the world: a replan is rare enough to pay for that. The two worlds must have the same bytes,
the same counters and the same pending events. tests/integration/test_replan_journeys.py compares
what the gate and the 7-day run don't reach.
"""

from __future__ import annotations

import copy
from typing import Any

from shadow_kit import Recorder


def _pending(world: Any) -> list[Any]:
    return [
        (event.event_id, event.sequence, event.due_minute, sorted(event.payload.items()))
        for event in world.event_queue.pending()
    ]


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("replan")
    recorder = Recorder(output, "replan")
    unsupported = native.Unsupported
    kernel = rules.replan_journeys

    def replan_journeys(controller: Any, state: Any, agent_ids: Any, minute: int) -> None:
        from sheltersim.experiments.metrics import MetricsRecorder
        from sheltersim.experiments.phases.update import UpdatePhase
        from sheltersim.experiments.timeline import _MetricSink

        world = controller.world
        recorder.seen["calls"] += 1
        twin = copy.deepcopy(world)
        twin_controller = type(controller)(twin, _MetricSink(MetricsRecorder()))
        error = None
        try:
            UpdatePhase._replan_in_python(
                twin_controller, twin.model_state["movement_v1"], agent_ids, minute
            )
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            error = raised
        try:
            kernel(controller, state, agent_ids, minute)
        except unsupported as reason:
            recorder.reasons[f"{reason}"] += 1
            raise
        recorder.seen["kernel_ran"] += 1
        recorder.seen["people"] += len(agent_ids)
        where = f"{tuple(agent_ids)!r:.100} at {minute}"
        if error is not None:
            recorder.differ(f"{where}: the kernel took what Python turned down: {error!r:.300}")
            return
        mine, theirs = world._canonical_data(), twin._canonical_data()
        if world._canonical_text(mine) != twin._canonical_text(theirs):
            recorder.differ(f"{where}: the worlds")
        if world.id_counters != twin.id_counters:
            recorder.differ(f"{where}: the counters")
        if _pending(world) != _pending(twin):
            recorder.differ(f"{where}: the pending events")
        recorder.seen["worlds_compared"] += 1

    rules.replan_journeys = replan_journeys
