"""Compare each kernel of native/src/rules/effects.rs with its Python rule at each call.

A handler's event is rare and a handler copies the world, so each call compares whole worlds: the
Python rule on one copy and the kernel on another, with their outcomes and `to_dict()` as JSON text,
in which a float keeps its bits, 1 differs from 1.0 and the key order shows. The row of
`effect_program_v1` must write the bytes of its tree. Then the kernel runs on the real world.

`should_apply_guard`, `has_active_modifier`, `has_pending_modifier` and `validate_state` only read,
and go through shadow_kit.pure_shadow. When Python turns down what `validate_state` accepts, that is
a difference: a check that takes a bad state is what a hash cannot see.

Plain runs of the gate or of 7 days say what ran: put SHELTERSIM_RULE_SHADOW and
SHELTERSIM_DISPATCH_COUNT_ONLY=1 in the environment, and this only writes `effects.stats()`.
"""

from __future__ import annotations

import atexit
import os
from typing import Any

from shadow_kit import Recorder, pure_shadow, text, values_in

PURE = (
    ("should_apply_guard", "should_apply_effect_guard"),
    ("has_active_modifier", "has_active_facility_modifier"),
    ("has_pending_modifier", "has_pending_facility_modifier"),
    ("validate_state", "validate_effect_program_state"),
)
HANDLERS = {
    "facility": "EffectProgramFacilityHandler",
    "environment": "EffectProgramEnvironmentHandler",
    "temporary_space": "EffectProgramTemporarySpaceHandler",
    "geometry": "EffectProgramGeometryHandler",
    "structure": "EffectProgramStructureHandler",
}


def report_stats(recorder: Recorder, module: Any) -> None:
    def report() -> None:
        counts, reasons = module.stats()
        for key, count in counts.items():
            recorder.seen[f"ran.{key}"] += count
        for key, count in reasons.items():
            recorder.reasons[key] += count

    # After Recorder's own: atexit runs the last registered first.
    atexit.register(report)


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
    return "applied", [record.to_dict() for record in result or ()]


def _handler_shadow(recorder: Recorder, label: str, kernel: Any, rule: Any) -> Any:
    from sheltersim import native

    unsupported = native.Unsupported

    def shadow(handler: Any, world: Any, event: Any) -> Any:
        from sheltersim.core.world import WorldState

        op = event.provenance.get("effect_program_v1", {}).get("op", "?")
        recorder.seen[f"{label}.calls"] += 1
        recorder.seen[f"op.{op}.calls"] += 1
        try:
            document = world.to_dict()
            python_world = WorldState.from_dict(document)
            kernel_world = WorldState.from_dict(document)
        except Exception as raised:  # noqa: BLE001 - both rules copy the world first
            recorder.reasons[f"{label}: {op}: the copy raised {type(raised).__name__}"] += 1
            return kernel(handler, world, event)
        expected = _outcome(lambda: rule()(handler, python_world, event))
        try:
            result = kernel(handler, kernel_world, event)
        except unsupported as reason:
            recorder.reasons[f"{label}: {reason}"] += 1
            # The Python rule then runs on the real world, as in a plain run. A wrong result the
            # world's validation turns down comes here too, and no hash would ever see it.
            if expected[0] == "applied":
                recorder.seen[f"{label}.refused_a_success"] += 1
                recorder.differ(f"{label} {op}: refused what the Python rule applied: {reason}")
            raise
        except Exception as raised:  # noqa: BLE001 - only compared with the rule's
            actual = (type(raised).__name__, str(raised))
        else:
            actual = ("applied", [record.to_dict() for record in result or ()])
        recorder.seen[f"{label}.kernel_ran"] += 1
        recorder.seen[f"op.{op}.kernel_ran"] += 1
        if text(actual) != text(expected):
            recorder.differ(f"{label} {op}: {actual!r:.300} is not {expected!r:.300}")
        elif actual[0] == "applied":
            python_after = python_world.to_dict()
            kernel_after = kernel_world.to_dict()
            if text(python_after) != text(kernel_after):
                for key, value in python_after.items():
                    if text(value) != text(kernel_after.get(key)):
                        recorder.differ(f"{label} {op}: {key} differs")
            else:
                recorder.seen[f"{label}.values_compared"] += values_in(python_after)
            mirror = kernel_world.model_state._mirrors.get("effect_program_v1")
            if mirror is not None and "effect_program_v1" in kernel_world.model_state:
                recorder.seen[f"{label}.rows_verified"] += 1
                if not mirror.verify():
                    recorder.differ(f"{label} {op}: the row is not its tree")
        return kernel(handler, world, event)

    return shadow


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "effects")
    rules = native.rules("effects")
    report_stats(recorder, rules)
    if os.environ.get("SHELTERSIM_DISPATCH_COUNT_ONLY") == "1":
        return
    unsupported = native.Unsupported
    module: list[Any] = []

    def effects() -> Any:
        if not module:
            from sheltersim.events import effects as loaded

            module.append(loaded)
        return module[0]

    for kernel_name, rule_name in PURE:
        setattr(
            rules,
            kernel_name,
            pure_shadow(
                recorder,
                kernel_name,
                getattr(rules, kernel_name),
                lambda rule_name=rule_name: getattr(effects(), rule_name).__wrapped__,
                unsupported,
            ),
        )
    for kernel_name, class_name in HANDLERS.items():
        setattr(
            rules,
            kernel_name,
            _handler_shadow(
                recorder,
                kernel_name,
                getattr(rules, kernel_name),
                lambda class_name=class_name: getattr(effects(), class_name).apply.__wrapped__,
            ),
        )
