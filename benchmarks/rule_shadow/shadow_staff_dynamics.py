"""Compare every kernel of native/src/rules/staff_dynamics.rs with its Python rule at each call."""

from __future__ import annotations

import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, pure_shadow, text, values_in

PURE = (
    ("on_shift", "staff_dynamics_on_shift"),
    ("is_available", "staff_dynamics_is_available"),
    ("canonical_config", "canonical_staff_dynamics_config"),
    ("validate_override", "validate_staff_shift_windows_override"),
    ("create_state", "create_staff_dynamics_state"),
)


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    rules = native.rules("staff_dynamics")
    recorder = Recorder(output, "staff_dynamics")
    unsupported = native.Unsupported
    module: list[Any] = []

    def rule_of(name: str) -> Any:
        return lambda: getattr(module[0], name).__wrapped__

    def answers(label: str) -> Any:
        def detail(result: Any) -> None:
            recorder.seen[f"{label}.{bool(result)}"] += 1

        return detail

    for kernel_name, rule_name in PURE:
        detail = answers(kernel_name) if kernel_name in {"on_shift", "is_available"} else None
        setattr(
            rules,
            kernel_name,
            pure_shadow(
                recorder,
                kernel_name,
                getattr(rules, kernel_name),
                rule_of(rule_name),
                unsupported,
                **({"detail": detail} if detail else {}),
            ),
        )

    update_kernel = rules.update
    add_kernel = rules.add_staff

    def on_copy(world: Any, state: Any) -> tuple[Any, Any]:
        """A plain copy of the namespace, and a world for the Python rule that holds it."""
        plain = json.loads(json.dumps(state))
        table = dict(world.model_state)
        table[module[0].STAFF_DYNAMICS_STATE_KEY] = plain
        return plain, SimpleNamespace(
            model_state=table,
            staff=world.staff,
            model_configs=world.model_configs,
            duration_minutes=world.duration_minutes,
        )

    def compare(world: Any, label: str, minute: Any, plain: Any) -> None:
        key = module[0].STAFF_DYNAMICS_STATE_KEY
        state = world.model_state[key]
        if text(plain) != text(state):
            recorder.differ(f"{label} at {minute}: {key}")
        recorder.seen[f"{label}.values_compared"] += values_in(plain)
        if not world._core.mirror(key).verify():
            recorder.differ(f"{label} at {minute}: the row and the tree")

    def update(world: Any, minute: Any) -> None:
        recorder.seen["update.calls"] += 1
        key = module[0].STAFF_DYNAMICS_STATE_KEY
        state = world.model_state.get(key)
        if state is None:
            recorder.seen["update.no_namespace"] += 1
            update_kernel(world, minute)
            return
        plain, shim = on_copy(world, state)
        before = json.loads(json.dumps(state))
        python_error = None
        try:
            module[0].update_staff_dynamics.__wrapped__(shim, minute)
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            python_error = raised
        try:
            update_kernel(world, minute)
        except unsupported as reason:
            recorder.reasons[f"update: {reason}"] += 1
            raise
        recorder.seen["update.kernel_ran"] += 1
        if python_error is not None:
            recorder.differ(f"update at {minute}: the kernel took what Python turned down")
            return
        compare(world, "update", minute, plain)
        # What the comparison reached, since a branch no scenario takes is not compared.
        for staff_id, record in plain["staff"].items():
            old = before["staff"][staff_id]
            branches: Counter[str] = Counter()
            branches[f"activity_{record['last_activity']}"] += 1
            if record["rest_until_minute"] > old["rest_until_minute"]:
                branches["rest_started"] += 1
            if old["rest_until_minute"] and not record["rest_until_minute"]:
                branches["rest_ended"] += 1
            if record["fatigue"] < old["fatigue"] and record["fatigue"] > 0.0:
                branches["fatigue_recovering"] += 1
            if 0.0 < record["fatigue"] and record["last_activity"] == "working":
                branches["fatigue_rising"] += 1
            recorder.seen.update({f"update.{name}": n for name, n in branches.items()})

    def add_staff(world: Any, staff_id: Any) -> None:
        recorder.seen["add_staff.calls"] += 1
        key = module[0].STAFF_DYNAMICS_STATE_KEY
        state = world.model_state.get(key)
        if state is None:
            add_kernel(world, staff_id)
            return
        plain, shim = on_copy(world, state)
        module[0].add_staff.__wrapped__(shim, staff_id)
        try:
            add_kernel(world, staff_id)
        except unsupported as reason:
            recorder.reasons[f"add_staff: {reason}"] += 1
            raise
        recorder.seen["add_staff.kernel_ran"] += 1
        compare(world, "add_staff", staff_id, plain)

    rules.update = update
    rules.add_staff = add_staff

    from sheltersim.core import state_mirror
    from sheltersim.domain import staff_dynamics

    module.append(staff_dynamics)
    rust_validate = native.state("staff_dynamics").validate

    def validator(world: Any) -> bool:
        accepted = rust_validate(world)
        recorder.seen["validator_accepted" if accepted else "validator_refused"] += 1
        try:
            staff_dynamics.validate_staff_dynamics_state(world)
        except ValueError as error:
            if accepted:
                recorder.differ(f"the validator took what Python turned down: {error}")
        else:
            if not accepted:
                recorder.seen["validator_refused_what_python_passes"] += 1
        return accepted

    state_mirror.register_rust_validator(staff_dynamics.STAFF_DYNAMICS_STATE_KEY, validator)
