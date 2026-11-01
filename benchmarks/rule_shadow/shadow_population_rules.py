"""Compare the kernels of domain/population.py and domain/population_lifecycle.py with their rules.

- A rule that writes: the whole world is copied, the Python rule runs on the copy, the kernel on
  the world, and the two must leave the same canonical JSON and return the same value. A rule that
  raises must have a kernel that refuses. Inside the Python rule every kernel of these two modules
  refuses, so all of it is Python.
- A rule that reads: `pure_shadow`, the rule's result or that the kernel refused.
- After a write, the rows of the namespaces these rules write must be the bytes of their trees.

These rules run at few events of a Run, so the gate and
the 7 days reach them far less than the tests do: run the tests with this shadow too.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from shadow_kit import Recorder, pure_shadow, text

WRITTEN = (
    "population_presence_v1",
    "population_lifecycle_v1",
    "population_dynamics_v1",
    "geometry_overlay_v1",
    "movement_v1",
    "body_state_v1",
    "agent_dynamics_v1",
    "information_spread_v1",
    "social_network_v1",
    "social_propagation_v1",
    "resource_service_v1",
    "effect_program_v1",
)


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "population_rules")
    unsupported = native.Unsupported
    state = {"python": 0}

    def copy_of(world: Any) -> Any:
        from copy import deepcopy

        return deepcopy(world)

    def document(world: Any) -> str:
        # Not canonical_json(): in the middle of a dispatch a person may have arrived before their
        # task ended, which validation refuses.
        return world._canonical_text(world._canonical_data())

    def rule_of(module: str, name: str) -> Callable[[], Callable[..., Any]]:
        def rule() -> Callable[..., Any]:
            import importlib

            found = getattr(importlib.import_module(f"sheltersim.domain.{module}"), name)
            return found.__wrapped__

        return rule

    def checked_rows(world: Any, label: str) -> None:
        mirrors = getattr(world.model_state, "_mirrors", {})
        for name in WRITTEN:
            mirror = mirrors.get(name)
            if mirror is None or name not in world.model_state:
                continue
            if mirror.tainted is not None:
                recorder.reasons[f"{label}: {name} is tainted"] += 1
            elif not mirror.verify():
                recorder.differ(f"{label}: the row of {name} differs from its tree")
            else:
                recorder.seen[f"{label}.rows_compared"] += 1

    def writer(
        label: str,
        kernel: Callable[..., Any],
        rule: Callable[[], Callable[..., Any]],
        worlds: tuple[int, ...] = (0,),
        idle: Callable[..., bool] = lambda *arguments, **options: False,
    ) -> Callable[..., Any]:
        def shadow(*arguments: Any, **options: Any) -> Any:
            if state["python"]:
                raise unsupported("the Python rule of the comparison")
            recorder.seen[f"{label}.calls"] += 1
            if idle(*arguments, **options):
                recorder.seen[f"{label}.nothing_to_do"] += 1
                return kernel(*arguments, **options)
            target = arguments[worlds[-1]]
            copied = list(arguments)
            try:
                for place in worlds:
                    copied[place] = copy_of(arguments[place])
            except Exception as error:  # noqa: BLE001 - the comparison can't be made here
                recorder.reasons[f"{label}: copy refused: {type(error).__name__}"] += 1
                result = kernel(*arguments, **options)
                recorder.seen[f"{label}.kernel_ran"] += 1
                return result
            state["python"] += 1
            error = None
            expected = None
            try:
                expected = rule()(*copied, **options)
            except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
                error = raised
            finally:
                state["python"] -= 1
            try:
                result = kernel(*arguments, **options)
            except unsupported as reason:
                recorder.reasons[f"{label}: {reason}"] += 1
                raise
            except Exception as raised:
                if error is None or type(raised) is not type(error) or str(raised) != str(error):
                    recorder.differ(f"{label}: the kernel raised {raised!r}, Python {error!r}")
                else:
                    recorder.seen[f"{label}.same_error"] += 1
                raise
            recorder.seen[f"{label}.kernel_ran"] += 1
            if error is not None:
                recorder.differ(f"{label}: the kernel took what Python raised: {error}")
                return result
            if text(result) != text(expected):
                recorder.differ(f"{label}: returned {result!r:.200}, not {expected!r:.200}")
            try:
                own = document(target)
                other = document(copied[worlds[-1]])
            except Exception as error_after:  # noqa: BLE001 - the state can't be written out
                recorder.reasons[f"{label}: no document after: {type(error_after).__name__}"] += 1
            else:
                recorder.seen[f"{label}.bytes_compared"] += len(own)
                if own != other:
                    place = next(
                        (i for i, (a, b) in enumerate(zip(own, other)) if a != b),
                        min(len(own), len(other)),
                    )
                    recorder.differ(
                        f"{label}: at minute {getattr(target, 'minute_index', '?')} differs at "
                        f"{place}: {own[max(0, place - 80) : place + 60]!r} / "
                        f"{other[max(0, place - 80) : place + 60]!r}"
                    )
            checked_rows(target, label)
            return result

        return shadow

    def reader(
        label: str, kernel: Callable[..., Any], rule: Callable[[], Any]
    ) -> Callable[..., Any]:
        compared = pure_shadow(recorder, label, kernel, rule, unsupported)

        def shadow(*arguments: Any, **options: Any) -> Any:
            if state["python"]:
                raise unsupported("the Python rule of the comparison")
            state["python"] += 1
            try:
                return compared(*arguments, **options)
            finally:
                state["python"] -= 1

        return shadow

    def no_arrivals(world: Any, minute: Any) -> bool:
        held = world.model_state.get("population_dynamics_v1")
        return held is None or not held["pending_arrivals"]

    def no_returns(world: Any, minute: Any) -> bool:
        held = world.model_state.get("population_lifecycle_v1")
        return not isinstance(held, dict) or not held["pending_returns"]

    population = native.rules("population")
    for name, rule_name, idle in (
        ("admit_pending_arrivals", "admit_pending_arrivals", no_arrivals),
        ("queue_arrivals", "queue_arrivals", None),
        ("apply_arrival_approach_completion", "apply_arrival_approach_completion", None),
        ("schedule_population_retry", "schedule_population_retry", None),
        ("queue_departures", "queue_departures", None),
    ):
        population_rule = rule_of("population", rule_name)
        options = {"idle": idle} if idle else {}
        setattr(
            population, name, writer(name, getattr(population, name), population_rule, **options)
        )
    for name, rule_name in (
        ("zone_has_room", "_zone_has_room"),
        ("choose_admission_target", "_choose_admission_target"),
        ("validate_population_dynamics_state", "validate_population_dynamics_state"),
    ):
        setattr(
            population,
            name,
            reader(name, getattr(population, name), rule_of("population", rule_name)),
        )
    lifecycle = native.rules("population_lifecycle")
    lifecycle.apply_presence_action = writer(
        "apply_presence_action",
        lifecycle.apply_presence_action,
        rule_of("population_lifecycle", "_apply_presence_action"),
        worlds=(0, 1),
    )
    lifecycle.admit_pending_returns = writer(
        "admit_pending_returns",
        lifecycle.admit_pending_returns,
        rule_of("population_lifecycle", "admit_pending_returns"),
        idle=no_returns,
    )
    lifecycle.complete_presence_relocation = writer(
        "complete_presence_relocation",
        lifecycle.complete_presence_relocation,
        rule_of("population_lifecycle", "complete_presence_relocation"),
    )
    for name, rule_name in (
        ("zone_has_room", "_zone_has_room"),
        ("current_household_id", "current_household_id"),
        ("validate_population_lifecycle_state", "validate_population_lifecycle_state"),
    ):
        setattr(
            lifecycle,
            name,
            reader(
                f"lifecycle.{name}",
                getattr(lifecycle, name),
                rule_of("population_lifecycle", rule_name),
            ),
        )
