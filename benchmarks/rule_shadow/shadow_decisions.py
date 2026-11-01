"""Compare `record_outcome` of native/src/rules/decisions.rs with `_record_outcome` at each call.

Rust calls `commit_action` and `remember_decision` itself, so shadow_action_lifecycle.py and
shadow_agent_dynamics.py see only what they leave to Python. Here each call runs the Python rules
first, with the sink's method putting back what it appended, and the two other rules on the same
kind of twins as those shadows. Then the kernel runs on the real world. They must give:

- the same row in `sink.observations`, compared bit by bit with the keys in their order, and the
  same row the index reads;
- the same receipts, event counter and open actions;
- the same record of the person in `agent_dynamics_v1`.

The calls of those two shadows' `record_action_proposal` and `record_proposal` count how often the
kernel left a rule to Python after its own record.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from behavior_shadow import exact
from shadow_action_lifecycle import _CollectingLog, _python_json, _Stub
from shadow_kit import Recorder, text, values_in

KEY = "agent_dynamics_v1"


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("decisions")
    kernel = rules.record_outcome
    recorder = Recorder(output, "decisions")
    unsupported = native.Unsupported
    row_class = rules.ProposalRow
    bits: Counter[str] = Counter()

    def run(
        controller: Any,
        proposal: Any,
        *,
        target_id: Any,
        next_decision_at: Any,
        committed: Any,
        observed_state: Any,
    ) -> None:
        options = {
            "target_id": target_id,
            "next_decision_at": next_decision_at,
            "committed": committed,
            "observed_state": observed_state,
        }
        recorder.seen["calls"] += 1
        if type(proposal) is not row_class:
            recorder.seen["python_proposals"] += 1
            kernel(controller, proposal, **options)
            return
        from sheltersim.domain import action_lifecycle, agent_dynamics
        from sheltersim.experiments.timeline import _IndexedMetricSink

        world, sink, active = controller.world, controller.artifacts, controller.active_actions
        recorded = proposal.with_outcome(target_id, next_decision_at)

        expected = None
        if type(sink) is _IndexedMetricSink:
            _IndexedMetricSink.record_decision(sink, recorded, observed_state=observed_state)
            expected = sink.observations.pop()

        plain = copy.deepcopy(active.snapshot())
        log = world.event_log
        before, counter = len(log), world.id_counters.get("event", 0)
        twin_log = _CollectingLog(log)
        stub = _Stub(
            agents=world.agents,
            model_state=world.model_state,
            minute_index=world.minute_index,
            event_log=twin_log,
            counter=counter,
        )
        action_lifecycle.record_action_proposal.__wrapped__(
            stub, recorded, target_id=target_id, committed=committed, active_actions=plain
        )

        state = world.model_state.get(KEY)
        memory = None
        if state is not None and proposal.agent_id in state["agents"]:
            document = {**state, "agents": {proposal.agent_id: state["agents"][proposal.agent_id]}}
            memory = json.loads(json.dumps(document))
            table = dict(world.model_state)
            table[KEY] = memory
            agent_dynamics.record_proposal.__wrapped__(
                SimpleNamespace(model_state=table, agents=world.agents),
                recorded,
                target_id=target_id,
            )

        count = len(getattr(sink, "observations", ()))
        try:
            kernel(controller, proposal, **options)
        except unsupported as reason:
            recorder.reasons[f"record_outcome: {reason}"] += 1
            raise
        recorder.seen["kernel_ran"] += 1
        recorder.seen[f"action_{recorded.action.value}"] += 1
        recorder.seen["committed"] += committed
        recorder.seen["with_target"] += target_id is not None
        where = f"minute {world.minute_index}, {proposal.agent_id}"

        if expected is None:
            recorder.seen["silent_sink"] += 1
        else:
            recorder.seen["indexed_sink"] += 1
            if len(sink.observations) != count + 1 or type(sink.observations[-1]) is not bytes:
                recorder.differ(f"{where}: the sink did not get one row of bytes")
            else:
                raw = sink.observations[-1]
                row = json.loads(raw)
                recorder.seen["row_bytes_compared"] += len(raw)
                if json.dumps(exact(row, bits)) != json.dumps(exact(expected, bits)):
                    recorder.differ(f"{where}: {row!r:.600} is not {expected!r:.600}")
                if native.index_row(row)[:4] != native.index_row(expected)[:4]:
                    recorder.differ(f"{where}: the index reads the rows differently")
                recorder.seen["row_values_compared"] += values_in(expected)
                recorder.seen["rows_with_environment"] += (
                    "zone_environment" in expected["observed_state"]
                )
                recorder.seen["rows_with_household"] += expected["household_id"] is not None

        raws = log.items_bytes(before, len(log))
        if len(raws) != len(twin_log.appended):
            recorder.differ(f"{where}: {len(raws)} receipts, not {len(twin_log.appended)}")
        else:
            for raw, record in zip(raws, twin_log.appended, strict=True):
                if raw != _python_json(record):
                    recorder.differ(f"{where}: {raw!r:.300} is not {_python_json(record)!r:.300}")
                recorder.seen["receipts_compared"] += 1
                recorder.seen["receipt_bytes_compared"] += len(raw)
        if world.id_counters.get("event", 0) != stub.counter:
            recorder.differ(f"{where}: the event counter is not {stub.counter}")
        if text(active.snapshot()) != text(plain):
            recorder.differ(f"{where}: the open actions differ")
        recorder.seen["open_action_values_compared"] += values_in(plain)

        if memory is not None:
            mine = text(world.model_state[KEY]["agents"][proposal.agent_id])
            theirs = text(memory["agents"][proposal.agent_id])
            if mine != theirs:
                recorder.differ(f"{where}: {mine:.400} is not {theirs:.400}")
            recorder.seen["memory_values_compared"] += values_in(memory["agents"])
        recorder.seen["values_in_rows"] = bits["behavior_values_compared"]

    rules.record_outcome = run
