"""Compare every kernel of native/src/rules/agent_dynamics.rs with its Python rule at each call.

The readers and builders are compared by shadow_kit.pure_shadow. A writer's Python rule runs first
on a plain copy of the namespace (of one record, for the two that write one record each time), then
the kernel on the world: the JSON text must be the same. Every so often the row must also write the
bytes of the whole tree, and `verify` must pass.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, pure_shadow, text, values_in

KEY = "agent_dynamics_v1"
PURE = (
    ("agent_knows_facility", "agent_knows_facility"),
    ("canonical_config", "canonical_agent_dynamics_config"),
    ("create_state", "create_agent_dynamics_state"),
)
ROW_CHECK_EVERY = 25


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("agent_dynamics")
    state_module = native.state("agent_dynamics")
    recorder = Recorder(output, "agent_dynamics")
    unsupported = native.Unsupported
    module: list[Any] = []

    def domain() -> Any:
        if not module:
            from sheltersim.domain import agent_dynamics

            module.append(agent_dynamics)
        return module[0]

    def rule_of(name: str) -> Any:
        return lambda: getattr(domain(), name).__wrapped__

    def answers(result: Any) -> None:
        recorder.seen[f"agent_knows_facility.{bool(result)}"] += 1

    for kernel_name, rule_name in PURE:
        options = {"detail": answers} if kernel_name == "agent_knows_facility" else {}
        setattr(
            rules,
            kernel_name,
            pure_shadow(
                recorder,
                kernel_name,
                getattr(rules, kernel_name),
                rule_of(rule_name),
                unsupported,
                **options,
            ),
        )

    def twin_of(world: Any, state: Any, agent_id: Any) -> tuple[Any, Any]:
        """A plain copy, of one record if agent_id is a known one, and a world that holds it."""
        document = (
            {**state, "agents": {agent_id: state["agents"][agent_id]}}
            if agent_id is not None and agent_id in state["agents"]
            else state
        )
        plain = json.loads(json.dumps(document))
        table = dict(world.model_state)
        table[KEY] = plain
        return plain, SimpleNamespace(
            model_state=table,
            agents=world.agents,
            facilities=world.facilities,
            model_configs=world.model_configs,
        )

    def row_check(world: Any, label: str) -> None:
        recorder.seen["row_checks.due"] += 1
        if recorder.seen["row_checks.due"] % ROW_CHECK_EVERY:
            return
        mirror = world._core.mirror(KEY)
        if not mirror.verify():
            recorder.differ(f"{label}: the row and the tree")
        if mirror.row_json() != native.canonical_json(world.model_state[KEY]):
            recorder.differ(f"{label}: the row's bytes and the tree's")
        recorder.seen["row_checks.made"] += 1

    def writer(name: str, kernel: Any, agent_of: Any, whole: bool) -> Any:
        def run(world: Any, *arguments: Any, **options: Any) -> None:
            recorder.seen[f"{name}.calls"] += 1
            state = world.model_state.get(KEY)
            if state is None:
                try:
                    kernel(world, *arguments, **options)
                except unsupported as reason:
                    recorder.reasons[f"{name}: {reason}"] += 1
                    raise
                recorder.seen[f"{name}.no_namespace"] += 1
                return
            agent_id = None if whole else agent_of(*arguments)
            plain, twin = twin_of(world, state, agent_id)
            before = text(plain)
            error = None
            try:
                getattr(domain(), name).__wrapped__(twin, *arguments, **options)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
                error = raised
            try:
                kernel(world, *arguments, **options)
            except unsupported as reason:
                recorder.reasons[f"{name}: {reason}"] += 1
                raise
            recorder.seen[f"{name}.kernel_ran"] += 1
            if error is not None:
                recorder.differ(f"{name}: the kernel took what Python turned down: {error}")
                return
            where = f"{name} {arguments[1:]!r:.120}"
            if agent_id is None:
                mine, theirs = text(world.model_state[KEY]), text(plain)
            elif agent_id in world.model_state[KEY]["agents"]:
                mine = text(world.model_state[KEY]["agents"][agent_id])
                theirs = text(plain["agents"][agent_id])
            else:
                mine, theirs = "", ""
            if mine != theirs:
                recorder.differ(f"{where}: {mine:.400} is not {theirs:.400}")
            recorder.seen[f"{name}.values_compared"] += values_in(plain)
            if before != text(plain):
                recorder.seen[f"{name}.changed"] += 1
            row_check(world, where)

        return run

    rules.record_proposal = writer(
        "record_proposal", rules.record_proposal, lambda proposal: proposal.agent_id, False
    )
    rules.learn_facility = writer(
        "learn_facility", rules.learn_facility, lambda agent_id, _facility: agent_id, False
    )
    rules.announce_installed_facility = writer(
        "announce_installed_facility", rules.announce_installed_facility, None, True
    )
    rules.add_agent = writer("add_agent", rules.add_agent, None, True)

    rust_validate = state_module.validate

    def validator(world: Any) -> bool:
        accepted = rust_validate(world)
        recorder.seen["validator_accepted" if accepted else "validator_refused"] += 1
        try:
            domain().validate_agent_dynamics_state(world)
        except ValueError as error:
            if accepted:
                recorder.differ(f"the validator took what Python turned down: {error}")
        else:
            if not accepted:
                recorder.seen["validator_refused_what_python_passes"] += 1
        return accepted

    state_module.validate = validator
