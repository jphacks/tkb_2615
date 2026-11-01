"""Compare native/src/rules/metrics.rs and tick_validation.rs with their Python rules at each call.

The KPI kernels only read, so the Python rule runs on the same world and the two must be the same
text of JSON. The check kernels answer yes or Unsupported, so at each yes the Python rules run too
and must pass; a yes alone says little on a good state, so every PROBE_EVERY-th yes also writes a
value past the rules into a restored copy, one rule at a time, and the kernel must turn each down.
"""

from __future__ import annotations

import json
from typing import Any

from shadow_kit import Recorder, pure_shadow, values_in

PROBE_EVERY = 997


def _write(item: Any, name: str, value: Any) -> Any:
    """A write that reaches only the object, which is what the kernel must read."""
    item.__dict__[name] = value


def _breaks(world: Any) -> list[tuple[str, Any]]:
    """(what is broken, the write). Each is undone before the next."""
    found: list[tuple[str, Any, str, Any]] = []
    agent = next(iter(world.agents.values()), None)
    if agent is not None:
        found.append(("stress 1% over", agent, "stress", 1.01))
        found.append(("need 1% over", agent.needs, "hunger", 1.01))
        found.append(("unknown zone", agent, "zone_id", "nowhere"))
        found.append(("next decision below 0", agent, "next_decision_at", -1))
    facility = next(iter(world.facilities.values()), None)
    if facility is not None:
        found.append(
            ("working units above capacity", facility, "working_units", facility.capacity + 1)
        )
    member = next(iter(world.staff.values()), None)
    if member is not None:
        found.append(("busy without a task", member, "busy_until", member.busy_until + 1))
    task = next(iter(world.active_tasks.values()), None)
    if task is not None:
        found.append(("due before start", task, "due_minute", task.start_minute - 1))
    return [(what, (item, name, value)) for what, item, name, value in found]


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "metrics")
    unsupported = native.Unsupported
    metric_rules = native.rules("metrics")
    check_rules = native.rules("tick_validation")

    def snapshot_rule() -> Any:
        from sheltersim.experiments import metrics

        return metrics._snapshot_fields.__wrapped__

    def kpi_rule() -> Any:
        from sheltersim.experiments import operational_kpis

        return operational_kpis.operational_kpi_values.__wrapped__

    def snapshot_values(fields: Any) -> None:
        # The fields of the snapshot, and each value in its JSON.
        recorder.seen["snapshot.values_compared"] += (
            len(fields) - 1 + values_in(json.loads(fields[14]))
        )

    metric_rules.snapshot_fields = pure_shadow(
        recorder,
        "snapshot",
        metric_rules.snapshot_fields,
        snapshot_rule,
        unsupported,
        detail=snapshot_values,
    )
    metric_rules.kpi_values = pure_shadow(
        recorder, "kpi", metric_rules.kpi_values, kpi_rule, unsupported
    )

    entity_kernel = check_rules.entity_rules
    population_kernel = check_rules.population_dynamics
    accepted = [0]

    def probe(world: Any, config: Any, initial: Any, needs: Any) -> None:
        from sheltersim.core.world import WorldState

        try:
            copy = WorldState.from_dict(world.to_dict())
        except (ValueError, TypeError, KeyError):
            recorder.seen["entity.probe_no_document"] += 1
            return
        requests = list(copy.support_requests.values())
        try:
            entity_kernel(copy, requests, config, initial, needs)
        except unsupported:
            recorder.seen["entity.probe_copy_refused"] += 1
            return
        for what, (item, name, value) in _breaks(copy):
            before = item.__dict__[name]
            _write(item, name, value)
            recorder.seen["entity.probes"] += 1
            try:
                entity_kernel(copy, requests, config, initial, needs)
                recorder.differ(f"entity: the kernel took {what}")
            except unsupported:
                pass
            _write(item, name, before)

    def entity_shadow(world: Any, requests: Any, config: Any, initial: Any, needs: Any) -> Any:
        recorder.seen["entity.calls"] += 1
        try:
            left = entity_kernel(world, requests, config, initial, needs)
        except unsupported as reason:
            recorder.reasons[f"entity: {reason}"] += 1
            raise
        recorder.seen["entity.kernel_ran"] += 1
        if left:
            recorder.seen["entity.zones_left_to_python"] += len(left)
        memo = world.__dict__["_tick_memo"]
        try:
            pending = world._validate_entity_rules(config, memo)
            world._validate_event_rules(pending, False)
        except Exception as error:  # noqa: BLE001 - only compared with what the kernel did
            recorder.differ(f"entity: the kernel took what Python turned down: {error}")
        else:
            recorder.seen["entity.values_compared"] += (
                len(world.agents)
                + len(world.facilities)
                + len(world.staff)
                + len(world.active_tasks)
                + len(requests)
                + len(pending)
            )
            recorder.seen["entity.requests_read"] += len(requests)
            recorder.seen["entity.queue_entries"] += sum(
                len(item.queue_agent_ids) + len(item.resource_queue_agent_ids)
                for item in world.facilities.values()
            )
            recorder.seen["entity.service_entries"] += sum(
                len(item.active_agent_ids) + len(item.resource_active_agent_ids)
                for item in world.facilities.values()
            )
        accepted[0] += 1
        if accepted[0] % PROBE_EVERY == 0:
            probe(world, config, initial, needs)
        return left

    def population_shadow(world: Any) -> bool:
        recorder.seen["population.calls"] += 1
        try:
            answer = population_kernel(world)
        except unsupported as reason:
            recorder.reasons[f"population: {reason}"] += 1
            raise
        recorder.seen["population.kernel_ran"] += 1
        if answer is True:
            from sheltersim.domain.population import validate_population_dynamics_state

            try:
                validate_population_dynamics_state(world)
            except Exception as error:  # noqa: BLE001 - only compared with what the kernel did
                recorder.differ(f"population: the kernel took what Python turned down: {error}")
            else:
                recorder.seen["population.values_compared"] += len(world.agents)
        return answer

    check_rules.entity_rules = entity_shadow
    check_rules.population_dynamics = population_shadow
