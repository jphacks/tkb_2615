"""Compare the Rust steps with their Python rules at every call, in any process.

This file has the long-term psychology step, psychology_update.py the other psychology steps,
social_propagation_shadow.py the social one, movement_shadow.py the state of movement_v1,
information_shadow.py the information one, whose results are in DIR/information,
world_view_shadow.py the views, in DIR/world_view, and behavior_shadow.py the proposals, in
DIR/behavior.

Hashes do not see a 1% change in a value that doesn't reach them yet, and a kernel may never run if
it always says Unsupported. Put this directory first on PYTHONPATH and run anything:

    SHELTERSIM_RULE_SHADOW=DIR PYTHONPATH=benchmarks/rule_shadow:src:native/pylib \
        python benchmarks/gate_all_paths.py --output OUT --expect fixtures/gate-all-paths.json
    python benchmarks/rule_shadow/summary.py DIR

Each step runs the Python rules on plain copies first, then the kernel on the world. The two must
be the same text of JSON, in which a float keeps its bits and 1 differs from 1.0, and the row must
write the bytes of the tree. Each process writes what it saw to DIR at exit, so the worker
processes of a Project count too. The kernel's result is what goes on, so the hashes of such a run
must still pass. Without SHELTERSIM_RULE_SHADOW this file does nothing.

psychology_update.py does the same for the other two steps. This is the pattern for the next
kernels; docs/rust-port/lanes/p5-template-namespace.md says how.
A new shadow is a file shadow_<name>.py with prepare(output) beside this one (see shadow_kit.py).
Each runs first, so it can replace a kernel before the module that looks it up is imported.
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

OUTPUT = os.environ.get("SHELTERSIM_RULE_SHADOW")


def _floats(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_floats(item) for item in value.values())
    return 1


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _install(output: str) -> None:
    import psychology_update

    from sheltersim import native
    from sheltersim.core import state_mirror
    from sheltersim.domain.distress_monitor import record_distress_monitor_tick
    from sheltersim.domain.psychological_state import (
        PSYCHOLOGICAL_STATE_KEY,
        validate_psychological_state,
    )
    from sheltersim.experiments.phases.update import UpdatePhase
    from sheltersim.experiments.runner import OfflineSimulationController

    if not native.available():
        return
    seen: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    differences: list[str] = []
    kernel = native.rules("psychology").update_long_term
    python_step = UpdatePhase._update_long_term_psychology

    def step(self: Any, world: Any, view: Any, final_deltas: Any, minute: int) -> None:
        seen["steps"] += 1
        policy = self.psychology
        update_long_term = getattr(policy, "update_long_term_state", None)
        python_metrics = None
        python_state = None
        python_error = None
        records_before = 0
        if callable(update_long_term):
            python_metrics = dict(world.metric_state)
            records_before = len(
                world.model_state.get(PSYCHOLOGICAL_STATE_KEY, {}).get("agents", {})
            )
            try:
                record_distress_monitor_tick(
                    python_metrics,
                    previous_by_agent={key: agent.stress for key, agent in view.agents.items()},
                    current_by_agent={delta.agent_id: delta.stress for delta in final_deltas},
                    threshold=policy.parameters.high_distress_threshold,
                )
                python_state = update_long_term(
                    world.model_state[PSYCHOLOGICAL_STATE_KEY], minute, final_deltas
                )
            except Exception as error:  # noqa: BLE001 - only compared with what the kernel does
                python_error = error
        try:
            kernel(self, world, view, final_deltas, minute)
        except native.Unsupported as reason:
            reasons[str(reason)] += 1
            python_step(self, world, view, final_deltas, minute)
            return
        seen["kernel_ran"] += 1
        if python_error is not None:
            differences.append(f"minute {minute}: the kernel took what Python turned down")
            return
        state = world.model_state[PSYCHOLOGICAL_STATE_KEY]
        if _text(python_metrics) != _text(world.metric_state):
            differences.append(f"minute {minute}: metric_state")
        if _text(python_state) != _text(state):
            differences.append(f"minute {minute}: {PSYCHOLOGICAL_STATE_KEY}")
        seen["values_compared"] += _floats(python_metrics) + _floats(python_state)
        for name in (PSYCHOLOGICAL_STATE_KEY, "metric_state"):
            if not world._core.mirror(name).verify():
                differences.append(f"minute {minute}: the row and the tree of {name}")
        # What the comparison reached, since a branch no scenario takes is not compared.
        seen["records_added"] += len(state["agents"]) - records_before
        if (minute + 1) % 1440 == 0:
            seen["day_ends"] += 1
            seen["records_at_day_ends"] += len(state["agents"])
        high = policy.parameters.high_distress_threshold
        seen["people_high"] += sum(delta.stress >= high for delta in final_deltas)

    def validator(world: Any) -> bool:
        accepted = rust_validate(world)
        seen["validator_accepted" if accepted else "validator_refused"] += 1
        if accepted:
            # Raises, and fails the run, if the kernel took what the Python rules turn down.
            validate_psychological_state(world.model_state[PSYCHOLOGICAL_STATE_KEY])
        return accepted

    rust_validate = native.state("psychological_distress").validate
    state_mirror.register_rust_validator(PSYCHOLOGICAL_STATE_KEY, validator)
    OfflineSimulationController._update_long_term_psychology = step  # type: ignore[method-assign]
    psychology_update.install(seen, reasons, differences)

    import social_propagation_shadow

    social_propagation_shadow.install(seen, reasons, differences)

    import movement_shadow

    movement_shadow.install(seen, reasons, differences)

    def report() -> None:
        if seen or reasons or differences:
            target = Path(output) / f"{os.getpid()}.json"
            document = {"seen": seen, "unsupported": reasons, "differences": differences[:20]}
            target.write_text(json.dumps(document), encoding="utf-8")

    atexit.register(report)


if OUTPUT and os.environ.get("SHELTERSIM_DISPATCH_COUNT_ONLY") == "1":
    # A plain run that only says what the dispatch loop's direct calls did (shadow_dispatch.py).
    Path(OUTPUT).mkdir(parents=True, exist_ok=True)
    importlib.import_module("shadow_dispatch").prepare(OUTPUT)
    importlib.import_module("shadow_effects").prepare(OUTPUT)
    importlib.import_module("shadow_tick").count_ticks(OUTPUT)
elif OUTPUT:
    Path(OUTPUT).mkdir(parents=True, exist_ok=True)
    for shadow in sorted(Path(__file__).parent.glob("shadow_*.py")):
        if shadow.stem != "shadow_kit":
            importlib.import_module(shadow.stem).prepare(OUTPUT)
    _install(OUTPUT)
    from information_shadow import install as _install_information

    _install_information(OUTPUT)
    import capture_shadow

    capture_shadow.install(OUTPUT)
    import events_shadow

    events_shadow.install(OUTPUT)
    from world_view_shadow import install as _install_world_view

    _install_world_view(OUTPUT)
    from behavior_shadow import install as _install_behavior

    _install_behavior(OUTPUT)
