"""Test-only probes of the tick, installed from outside src/ and absent from ordinary runs.

They hook TickEngine, whichever module the phases live in, and CounterRNG.random:

- DigestStream writes a digest of each part of the world after every phase.
- DrawRecorder logs each random draw with its key and what the draw was compared with.
- ReversedOrder reverses the order of model_state's dicts and world.agents.

benchmarks/first_difference.py and benchmarks/gate_all_paths.py use them. The rules are in
docs/testing.md, and the recipes in docs/rust-port/lanes/p0-digest.md.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from sheltersim import native
from sheltersim.core.engine import TickEngine, TickPhase
from sheltersim.core.rng import CounterRNG
from sheltersim.core.state_mirror import NamespaceTable
from sheltersim.core.world import WorldState

EVENT_LOG = "event_log"
MODEL_STATE_PREFIX = "model_state/"
ENGINE_START = "engine_start"
# Long lists that only grow at the end or lose their oldest items, by part: most of the state's
# size, so each item is hashed once. Only a rule's edit of an old item would break this, and
# the periodic hash from the first item catches that.
APPEND_ONLY_LISTS = {
    "model_state/body_state_v1": ("history",),
    "model_state/shared_operations_v1": ("history",),
    "model_state/resource_ledger_v1": ("audit",),
    "model_state/resource_service_v1": ("request_history", "queue_stall_history"),
}


class Observer(Protocol):
    """Called by the hook on TickEngine; must leave the state's values and draws alone."""

    def engine_started(self, engine: int, world: WorldState) -> None: ...

    def before_phase(
        self, engine: int, world: WorldState, minute: int, phase: TickPhase
    ) -> None: ...

    def after_phase(
        self, engine: int, world: WorldState, minute: int, phase: TickPhase
    ) -> None: ...


def install(observers: Iterable[Observer]) -> Callable[[], None]:
    """Make every TickEngine built from now on call the observers; returns the undo.

    Engines are numbered in the order they are built, which a run repeats.
    """
    chosen = tuple(observers)
    built = TickEngine.__init__
    count = [0]

    def __init__(self: TickEngine, *args: Any, **kwargs: Any) -> None:
        built(self, *args, **kwargs)
        engine = count[0]
        count[0] += 1
        inner = self.phase_handler

        def handler(phase: TickPhase, world: WorldState, minute: int, rng: Any) -> None:
            for observer in chosen:
                observer.before_phase(engine, world, minute, phase)
            inner(phase, world, minute, rng)
            for observer in chosen:
                observer.after_phase(engine, world, minute, phase)

        self.phase_handler = handler
        for observer in chosen:
            observer.engine_started(engine, self.world)

    TickEngine.__init__ = __init__  # type: ignore[method-assign]

    def undo() -> None:
        TickEngine.__init__ = built  # type: ignore[method-assign]

    return undo


# Digests ---------------------------------------------------------------------------------


def encoded(value: Any) -> bytes:
    """The canonical JSON of a value, whichever kernels run, and also of a corrupt value."""
    try:
        raw = native.canonical_json(value)
    except ValueError:
        raw = None
    if raw is not None:
        return raw
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=True,
        default=WorldState._json_default,
    ).encode("utf-8", "surrogatepass")


def _digest(raw: bytes) -> str:
    return hashlib.blake2b(raw, digest_size=12).hexdigest()


def world_parts(world: WorldState) -> dict[str, Any]:
    """Every part of the world's canonical document but the EventLog, each namespace apart."""
    parts = world._canonical_data(include_event_log=False)
    del parts[EVENT_LOG]
    for name, value in parts.pop("model_state").items():
        parts[MODEL_STATE_PREFIX + name] = value
    return parts


class _Chain:
    """A digest of a growing sequence that hashes each item once, however it grew.

    Items already hashed are taken as unchanged while the first and last of them are the same
    objects. In-place edits of older items show when ``restart`` hashes everything again.
    """

    def __init__(self) -> None:
        self._first: Any = None
        self._last: Any = None
        self._count = 0
        self._hash = hashlib.blake2b(digest_size=12)

    def restart(self) -> None:
        self.__init__()  # type: ignore[misc]

    def digest(self, items: list[Any]) -> str:
        count = len(items)
        if count < self._count or (
            self._count
            and (items[0] is not self._first or items[self._count - 1] is not self._last)
        ):
            self.restart()
        if count > self._count:
            for item in items[self._count :]:
                self.add(item)
            self._first, self._last, self._count = items[0], items[-1], count
        return self._hash.hexdigest()

    def add(self, item: Any) -> None:
        self._hash.update(encoded(item))
        self._hash.update(b"\n")

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class _EngineDigests:
    """What one engine's stream remembers from phase to phase."""

    def __init__(self) -> None:
        self.last: dict[str, str] = {}
        self.lists: dict[tuple[str, str], _Chain] = {}
        self.log: Any = None
        self.log_chain = _Chain()
        self.log_count = 0
        self.appended: list[Any] = []

    def restart(self) -> None:
        for chain in self.lists.values():
            chain.restart()
        self.log_chain.restart()

    def part(self, name: str, value: Any) -> str:
        lists = APPEND_ONLY_LISTS.get(name)
        if lists is None or not isinstance(value, dict):
            return _digest(encoded(value))
        rest = dict(value)
        chains = []
        for field in lists:
            items = rest.get(field)
            if isinstance(items, list):
                del rest[field]
                chain = self.lists.setdefault((name, field), _Chain())
                chains.append(chain.digest(items))
        return _digest(encoded(rest) + ",".join(chains).encode())

    def log_digest(self, world: WorldState) -> str:
        log = world.event_log
        # A swapped world brings another log: its records are hashed from the first.
        if log is not self.log or len(log) < self.log_count:
            self.log, self.log_count = log, 0
            self.log_chain.restart()
        self.appended = log.records_from(self.log_count)
        for record in self.appended:
            self.log_chain.add(record.to_dict())
        self.log_count += len(self.appended)
        return self.log_chain.hexdigest()


class DigestStream:
    """Writes one line per phase: the digests of the parts that changed in it.

    ``dump`` is (line number, parts, file): at that line the parts' values go to the file and
    the process ends, which is how first_difference.py gets values without storing them.
    ``full_every_minutes`` is how often the append-only histories and the EventLog are hashed
    from their first item, so an edit of an old item shows at that minute's last phase.
    """

    def __init__(
        self,
        path: Path,
        *,
        dump: tuple[int, list[str], Path] | None = None,
        every_minutes: int = 1,
        minutes: tuple[int, int] | None = None,
        full_every_minutes: int = 60,
    ) -> None:
        self._file = path.open("w", encoding="utf-8")
        self._dump = dump
        self._every = every_minutes
        self._minutes = minutes
        self._full_every = full_every_minutes
        self._line = 0
        self._engines: dict[int, _EngineDigests] = {}

    def _write(self, engine: int, world: WorldState, minute: int | None, phase: str) -> None:
        state = self._engines.setdefault(engine, _EngineDigests())
        if phase == TickPhase.COLLECT_METRICS and minute % self._full_every == 0:
            state.restart()
            state.log_count = 0
        parts = world_parts(world)
        digests = {name: state.part(name, value) for name, value in parts.items()}
        digests[EVENT_LOG] = state.log_digest(world)
        changed = {k: v for k, v in digests.items() if state.last.get(k) != v}
        changed.update(dict.fromkeys(state.last.keys() - digests.keys(), ""))
        state.last = digests
        row: dict[str, Any] = {"e": engine, "m": minute, "p": phase, "d": changed}
        if phase == ENGINE_START:
            row["w"] = world.scenario_id
        self._file.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        if self._dump is not None and self._line == self._dump[0]:
            self._write_dump(parts, state.appended, digests)
        self._line += 1

    def _write_dump(
        self, parts: dict[str, Any], appended: list[Any], digests: dict[str, str]
    ) -> None:
        _, names, path = self._dump  # type: ignore[misc]
        values = {
            name: json.loads(encoded(parts[name]).decode("utf-8", "surrogatepass"))
            for name in names
            if name in parts
        }
        if EVENT_LOG in names:
            values[EVENT_LOG] = [record.to_dict() for record in appended]
        document = {"values": values, "digests": {k: digests.get(k, "") for k in names}}
        path.write_text(json.dumps(document, default=WorldState._json_default), encoding="utf-8")
        self.close()
        # What follows this phase was not asked for, and can take long.
        os._exit(0)

    def engine_started(self, engine: int, world: WorldState) -> None:
        if self._minutes is None or self._minutes[0] <= world.minute_index <= self._minutes[1]:
            self._write(engine, world, world.minute_index, ENGINE_START)

    def before_phase(self, engine: int, world: WorldState, minute: int, phase: TickPhase) -> None:
        pass

    def after_phase(self, engine: int, world: WorldState, minute: int, phase: TickPhase) -> None:
        if self._minutes is not None and not self._minutes[0] <= minute <= self._minutes[1]:
            return
        # A coarse stream has one line per step, after the last phase of the tick.
        if self._every > 1 and (phase is not TickPhase.COLLECT_METRICS or minute % self._every):
            return
        self._write(engine, world, minute, phase.value)

    def close(self) -> None:
        self._file.close()


# Draws -----------------------------------------------------------------------------------


def number_text(value: Any) -> Any:
    """A float as its exact hex; other numbers as they are, and anything else as its repr."""
    if isinstance(value, float):
        return float.hex(value)
    return value if isinstance(value, int) else repr(value)


class ObservedDraw(float):
    """A draw that logs what it is compared with or multiplied by, and is otherwise a float.

    Python tries a float subclass's reflected method first, so ``p > draw`` is logged as well.
    The results of arithmetic are plain floats: what the rule goes on to do is not logged.
    """

    __slots__ = ("_uses",)
    _uses: list[Any]

    def __new__(cls, value: float, uses: list[Any]) -> ObservedDraw:
        draw = super().__new__(cls, value)
        draw._uses = uses
        return draw

    # Copies and pickles leave the observation behind.
    def __copy__(self) -> float:
        return float(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> float:
        return float(self)

    def __reduce_ex__(self, protocol: Any) -> tuple[Any, ...]:
        return (float, (float(self),))


def _observing(name: str) -> Callable[[ObservedDraw, Any], Any]:
    plain = getattr(float, name)

    def method(self: ObservedDraw, other: Any) -> Any:
        result = plain(self, other)
        if result is not NotImplemented:
            use = [name.strip("_"), number_text(other)]
            if type(result) is bool:
                use.append(result)
            self._uses.append(use)
        return result

    method.__name__ = name
    return method


for _name in (
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__mul__",
    "__rmul__",
    "__add__",
    "__radd__",
    "__sub__",
    "__rsub__",
    "__truediv__",
    "__rtruediv__",
):
    setattr(ObservedDraw, _name, _observing(_name))
del _name


class DrawRecorder:
    """Logs CounterRNG's draws in Python, grouped by phase; a Rust rule's draws are not seen.

    Patching the class's method keeps ``type(rng) is CounterRNG``, so a run takes the same
    kernels as an unrecorded one. Each row: engine, minute, phase, seed, key, draw, uses.
    """

    def __init__(self, path: Path) -> None:
        self._file = path.open("w", encoding="utf-8")
        self._rows: list[tuple[Any, ...]] = []
        self._label: tuple[Any, Any, Any] = (None, None, "outside_ticks")
        self._original = CounterRNG.random
        self.count = 0
        original = self._original
        rows = self._rows

        def random(rng: CounterRNG, *key_parts: Any) -> float:
            value = original(rng, *key_parts)
            uses: list[Any] = []
            rows.append((rng.seed, key_parts, value, uses))
            return ObservedDraw(value, uses)

        CounterRNG.random = random  # type: ignore[method-assign]

    def _flush(self) -> None:
        engine, minute, phase = self._label
        for seed, key, value, uses in self._rows:
            row = {
                "e": engine,
                "m": minute,
                "p": phase,
                "s": seed,
                "k": key,
                "d": float.hex(value),
                "u": uses,
            }
            self._file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.count += len(self._rows)
        self._rows.clear()

    def engine_started(self, engine: int, world: WorldState) -> None:
        pass

    def before_phase(self, engine: int, world: WorldState, minute: int, phase: TickPhase) -> None:
        self._flush()
        self._label = (engine, minute, phase.value)

    def after_phase(self, engine: int, world: WorldState, minute: int, phase: TickPhase) -> None:
        self._flush()
        self._label = (None, None, "outside_ticks")

    def close(self) -> None:
        self._flush()
        self._file.close()
        CounterRNG.random = self._original  # type: ignore[method-assign]


def uses_of_draws(path: Path, label: str) -> list[tuple[str, list[Any]]]:
    """What the recorded draws under a key's first part were used with: (draw, uses) rows."""
    found = []
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            row = json.loads(line)
            if row["k"][0] == label:
                found.append((row["d"], row["u"]))
    return found


# Order -----------------------------------------------------------------------------------


def _reversed_in_place(mapping: dict[Any, Any]) -> None:
    sink = type(getattr(mapping, "_sink", None)).__name__
    if type(mapping) is NamespaceTable or sink == "NamespaceMirror":
        # Order is not state: Rust's rows have no part in it, and `clear` would taint them.
        items = list(dict.items(mapping))
        dict.clear(mapping)
        for key, value in reversed(items):
            dict.__setitem__(mapping, key, value)
        return
    items = list(mapping.items())
    mapping.clear()
    mapping.update(reversed(items))


def reverse_order(world: WorldState) -> None:
    """Reverse world.agents and every dict in model_state, keeping each object and its values.

    Lists keep their order: it is part of their value.
    """
    _reversed_in_place(world.agents)
    seen: set[int] = set()
    stack: list[Any] = [world.model_state]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if id(value) not in seen:
                seen.add(id(value))
                stack.extend(value.values())
                _reversed_in_place(value)
        elif isinstance(value, (list, tuple)):
            stack.extend(value)


# Where a rule has finished with its iterators, so the order can change in the middle of a
# phase: (module, class, method). A method with an after_event callback changes it after each
# event too. Name the new place when a rule moves; ReversedOrder fails if one is missing.
WITHIN_PHASE = (
    ("sheltersim.events.handlers", "EventDispatcher", "dispatch_due"),
    ("sheltersim.experiments.phases.propose", "ProposePhase", "_apply_proposal"),
    ("sheltersim.policies.world_view", "WorldView", "from_world"),
    ("sheltersim.policies.world_view", "WorldView", "after_state_update"),
    # With the Rust kernels these make the views, and the two above don't run. Only then do they
    # exist on this class (phases/segments.py).
    ("sheltersim.experiments.runner", "OfflineSimulationController", "_view_after_movement"),
    ("sheltersim.experiments.runner", "OfflineSimulationController", "_view_for_proposals"),
    ("sheltersim.domain.facilities", "ToiletServiceManager", "arbitrate"),
    ("sheltersim.domain.resource_service", "ResourceServiceManager", "arbitrate"),
    ("sheltersim.domain.staff", "StaffService", "assign_pending"),
)


class ReversedOrder:
    """Reverses the order at every phase's end and, ``within_phase``, inside phases too."""

    def __init__(self, *, within_phase: bool = False) -> None:
        self.reversals = 0
        self.fired: dict[str, int] = {}
        self._undo: list[Callable[[], None]] = []
        self._world: WorldState | None = None
        if within_phase:
            for module, owner, method in WITHIN_PHASE:
                self._wrap(module, owner, method)

    def _reverse(self, world: WorldState | None) -> None:
        if world is not None:
            reverse_order(world)
            self.reversals += 1

    def _wrap(self, module: str, owner: str, method: str) -> None:
        holder = getattr(__import__(module, fromlist=[owner]), owner)
        raw = holder.__dict__.get(method)
        if raw is None:
            return
        is_class_method = isinstance(raw, classmethod)
        original = raw.__func__ if is_class_method else raw
        label = f"{owner}.{method}"

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self.fired[label] = self.fired.get(label, 0) + 1
            self._reverse(self._world)
            if "after_event" in kwargs:
                after_event = kwargs["after_event"]

                def after_each(records: Any) -> None:
                    if after_event is not None:
                        after_event(records)
                    self._reverse(self._world)

                kwargs["after_event"] = after_each
            return original(*args, **kwargs)

        # A shadow reaches the Python rule of a kernel_first method through this.
        wrapped.__wrapped__ = getattr(original, "__wrapped__", original)  # type: ignore[attr-defined]
        setattr(holder, method, classmethod(wrapped) if is_class_method else wrapped)
        self._undo.append(lambda: setattr(holder, method, raw))

    def engine_started(self, engine: int, world: WorldState) -> None:
        pass

    def before_phase(self, engine: int, world: WorldState, minute: int, phase: TickPhase) -> None:
        self._world = world

    def after_phase(self, engine: int, world: WorldState, minute: int, phase: TickPhase) -> None:
        self._world = None
        self._reverse(world)

    def close(self) -> None:
        for undo in self._undo:
            undo()
