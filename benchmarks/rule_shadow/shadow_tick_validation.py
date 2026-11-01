"""Whether the per-tick check is right to take a row's word, at every check of a run.

Lane T took away the Rust checks that read the trees of the owned namespaces, so a kernel's yes
alone now skips a Python rule. At each rust_validated of those namespaces this runs the rule on the
tree too: a yes the rule turns down is a difference, and so is a row that differs from its tree. A
no the rule passes is counted with its reason: it only costs time.

A yes says little on a state with nothing wrong, so every PROBE_EVERY-th yes also breaks a restored
copy of the world: a value 1% beyond the rule's bound, written through the tree, and the tree stored
past the table. The kernel must say no to both.
"""

from __future__ import annotations

import copy
from typing import Any

from shadow_kit import Recorder, values_in

PROBE_EVERY = 997
PSYCHOLOGY = "psychological_distress_v1"
INFORMATION = "information_spread_v1"
STAFF = "staff_dynamics_v1"
SOCIAL_NETWORK = "social_network_v1"


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _beyond_the_bound(world: Any, name: str) -> bool:
    """Write a value 1% out of range through the tree. False if the state has nothing to break."""
    state = world.model_state.get(name)
    if state is None:
        return False
    if name == PSYCHOLOGY:
        holder, key = next(iter(state["agents"].values()), None), "long_term_distress"
    elif name == INFORMATION:
        holder, key = state["query_pressure"], next(iter(state["query_pressure"]), None)
    elif name == SOCIAL_NETWORK:
        holder, key = next(iter(state["relationships"].values()), None), "trust"
    else:
        holder, key = next(iter(state["staff"].values()), None), "fatigue"
    if holder is None or key is None:
        return False
    holder[key] = 1.01
    return True


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    from sheltersim.contracts import runtime
    from sheltersim.core import state_mirror
    from sheltersim.core import world as world_module
    from sheltersim.domain import (
        information,
        psychological_state,
        simulator_state,
        social_network,
        staff_dynamics,
    )

    recorder = Recorder(output, "tick_validation")
    rules = {
        PSYCHOLOGY: lambda world: psychological_state.validate_psychological_state(
            world.model_state.get(PSYCHOLOGY)
        ),
        INFORMATION: lambda world: information.validate_information_runtime_state(
            world.model_state.get(INFORMATION),
            agent_ids=set(world.agents),
            zone_ids=set(world.zones),
            duration_minutes=world.duration_minutes,
        ),
        STAFF: staff_dynamics.validate_staff_dynamics_state,
        SOCIAL_NETWORK: social_network.validate_social_network_state,
    }
    rust_validated = state_mirror.rust_validated

    def refusal(world: Any, name: str) -> str:
        table = world.__dict__.get("model_state")
        core = world.__dict__.get("_core")
        if core is None:
            return "no core"
        if not isinstance(table, state_mirror.NamespaceTable):
            return "model_state is not the table"
        if name not in table:
            return "the namespace is absent"
        if not core.entities().follows(world.__dict__):
            return "the rows do not follow the maps"
        mirror = table._mirrors.get(name)
        if mirror is None:
            return "not owned"
        if mirror.tainted is not None:
            return f"tainted: {mirror.tainted}"
        return "the kernel said no"

    probing = [False]

    def restored(document: dict[str, Any]) -> Any:
        return world_module.WorldState.from_dict(copy.deepcopy(document))

    def probe(world: Any, name: str) -> None:
        try:
            document = world.to_dict()
            broken = restored(document)
        except (ValueError, TypeError, KeyError):
            # A world in the middle of being put together has no document yet.
            recorder.seen[f"{name}.probe_no_document"] += 1
            return
        if not rust_validated(broken, name):
            recorder.seen[f"{name}.probe_copy_refused"] += 1
            return
        if not _beyond_the_bound(broken, name):
            recorder.seen[f"{name}.probe_nothing_to_break"] += 1
            return
        recorder.seen[f"{name}.probes"] += 1
        if rust_validated(broken, name):
            recorder.differ(f"{name}: the kernel took a value 1% beyond the bound")
        swapped = restored(document)
        swapped.__dict__["model_state"] = {
            key: _plain(value) for key, value in swapped.model_state.items()
        }
        recorder.seen[f"{name}.probes"] += 1
        if rust_validated(swapped, name):
            recorder.differ(f"{name}: the kernel took a tree stored past the table")

    def shadow(world: Any, name: str) -> bool:
        accepted = rust_validated(world, name)
        rule = rules.get(name)
        if rule is None or probing[0]:
            return accepted
        recorder.seen[f"{name}.calls"] += 1
        try:
            rule(world)
            error = None
        except (ValueError, KeyError, TypeError) as raised:
            error = raised
        if not accepted:
            recorder.seen[f"{name}.refused"] += 1
            if error is None:
                recorder.reasons[f"{name}: {refusal(world, name)}"] += 1
            return accepted
        recorder.seen[f"{name}.accepted"] += 1
        if error is not None:
            recorder.differ(f"{name}: the kernel took what Python turned down: {error}")
        mirror = world.__dict__["_core"].mirror(name)
        if not mirror.verify():
            recorder.differ(f"{name}: the row differs from the tree at minute {world.minute_index}")
        recorder.seen[f"{name}.values_compared"] += values_in(world.model_state.get(name, {}))
        if recorder.seen[f"{name}.accepted"] % PROBE_EVERY == 1:
            probing[0] = True
            try:
                probe(world, name)
            finally:
                probing[0] = False
        return accepted

    for module in (world_module, simulator_state, runtime):
        module.rust_validated = shadow
