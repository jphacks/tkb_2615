"""Whether the mirror's generation is right to stand in for reading the state, at every tick.

The end-of-tick check (native/src/rules/after_tick.rs) skips the rules of a namespace no write
reached since its last passed check, by the generation of its mirror. Results don't depend on this
unless a write gets past the generation, and then only a bad state would go unseen until the next
save, so no hash would show it. At each check the kernel passes, this keeps the canonical JSON of
each namespace and of model_configs, and at the next:

- a state the memo calls unchanged must have the same bytes as at the last check;
- a row must have the bytes of its tree;
- the Python rule of the dynamics, whose skip doesn't read the clock, must pass when skipped.

The bytes are the values compared. Writes the generation saw are counted, so a run with no write at
all says so. It runs the kernel as shadow_after_tick.py does, past the shadows' Python path.
"""

from __future__ import annotations

from typing import Any

from shadow_kit import Recorder

NAMES = (
    "population_presence_v1",
    "population_lifecycle_v1",
    "population_dynamics_v1",
    "temporary_spaces_v1",
    "geometry_overlay_v1",
)


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    from sheltersim.core import state_mirror, tick_validation

    recorder = Recorder(output, "population")
    rules = native.rules("after_tick")
    kernel = rules.after_tick
    last: dict[int, dict[str, bytes | None]] = {}

    def checked(world: Any, memo: Any, _from_shadow: bool = False) -> None:
        now = {name: tick_validation.saved_json(world.model_state.get(name)) for name in NAMES}
        now["model_configs"] = tick_validation.saved_json(world.model_configs)
        kernel(world, memo, True)
        seen = last.get(id(memo))
        # One entry per live memo; a new one starts with nothing to compare.
        last[id(memo)] = now
        recorder.seen["checks"] += 1
        table = world.model_state
        mirrors = getattr(table, "_mirrors", {})
        for name in NAMES:
            mirror = mirrors.get(name)
            if mirror is None or name not in table:
                continue
            recorder.seen[f"{name}.owned"] += 1
            if state_mirror.namespace_stamp(world, name) is None:
                recorder.reasons[f"{name}: no stamp"] += 1
            if mirror.tainted is not None:
                recorder.reasons[f"{name}: tainted"] += 1
            elif not mirror.verify():
                recorder.differ(f"{name}: the row differs from its tree at {world.minute_index}")
            recorder.seen["bytes_compared"] += len(now[name] or b"")
        core = world.__dict__.get("_core")
        configs_mirror = None if core is None else core.mirror("model_configs")
        if configs_mirror is not None:
            if state_mirror.field_stamp(world, "model_configs") is None:
                recorder.reasons["model_configs: no stamp"] += 1
            elif configs_mirror.tainted is not None:
                recorder.reasons["model_configs: tainted"] += 1
            elif not configs_mirror.verify():
                recorder.differ(f"model_configs: the row differs at {world.minute_index}")
        if seen is None:
            return
        unchanged = memo.facts()["unchanged"]
        for flag, name in (
            ("presence", "population_presence_v1"),
            ("lifecycle", "population_lifecycle_v1"),
            ("dynamics", "population_dynamics_v1"),
            ("overlay", "geometry_overlay_v1"),
            ("configs", "model_configs"),
        ):
            if not unchanged[flag]:
                recorder.seen[f"{flag}_unchanged.no"] += 1
                if seen[name] != now[name]:
                    recorder.seen[f"{flag}_unchanged.written"] += 1
                continue
            recorder.seen[f"{flag}_unchanged.yes"] += 1
            recorder.seen["bytes_compared"] += len(now[name] or b"")
            if seen[name] != now[name]:
                recorder.differ(f"{flag} unchanged at {world.minute_index}, but its bytes changed")
        if unchanged["dynamics"]:
            # Here: shadow_population_rules.py replaces this rule's kernel before it is imported.
            from sheltersim.domain.population import validate_population_dynamics_state

            try:
                validate_population_dynamics_state(world)
            except Exception as error:  # noqa: BLE001 - only compared with the skip
                recorder.differ(f"dynamics check skipped, and the rule says: {error}")

    rules.after_tick = checked
