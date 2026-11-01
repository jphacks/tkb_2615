"""Whether the mirror's generation is right to stand in for reading the state, at every tick.

The end-of-tick check of a Rust-owned namespace (core/tick_validation.py) trusts "no write reached
it" where it used to encode the state and compare. Results don't depend on this unless a write gets
past the generation, and then only a bad state would go unseen until the next save, so no hash
would show it. At each TickValidationMemo.begin this keeps the canonical JSON of each namespace and
of model_configs as it was at the last check, and:

- a state the memo calls unchanged must have the same bytes as at the last check;
- a row must have the bytes of its tree;
- the Python rule of each skipped check that doesn't read the clock must pass, for dynamics, whose
  skip this lane added. The other skips only changed how they learn the state is the same.

The bytes are the values compared. Writes the generation saw are counted, so a run with no write at
all says so.
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
    from sheltersim.domain.population import validate_population_dynamics_state

    recorder = Recorder(output, "population")
    begin = tick_validation.TickValidationMemo.begin
    last: dict[int, dict[str, bytes | None]] = {}

    def checked_begin(self: Any, world: Any) -> None:
        seen = last.pop(id(self), None)
        now = {name: tick_validation.saved_json(world.model_state.get(name)) for name in NAMES}
        now["model_configs"] = tick_validation.saved_json(world.model_configs)
        begin(self, world)
        # One entry per live memo; a new one starts with nothing to compare.
        last[id(self)] = now
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
        for flag, name in (
            ("presence_unchanged", "population_presence_v1"),
            ("lifecycle_unchanged", "population_lifecycle_v1"),
            ("dynamics_unchanged", "population_dynamics_v1"),
            ("overlay_unchanged", "geometry_overlay_v1"),
            ("configs_unchanged", "model_configs"),
        ):
            if not getattr(self, flag):
                recorder.seen[f"{flag}.no"] += 1
                if seen[name] != now[name]:
                    recorder.seen[f"{flag}.written"] += 1
                continue
            recorder.seen[f"{flag}.yes"] += 1
            recorder.seen["bytes_compared"] += len(now[name] or b"")
            if seen[name] != now[name]:
                recorder.differ(f"{flag} at {world.minute_index}, but its bytes changed")
        if self.dynamics_unchanged:
            try:
                validate_population_dynamics_state(world)
            except Exception as error:  # noqa: BLE001 - only compared with the skip
                recorder.differ(f"dynamics check skipped, and the rule says: {error}")

    tick_validation.TickValidationMemo.begin = checked_begin  # type: ignore[method-assign]
