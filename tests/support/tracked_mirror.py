"""A test-only sink for the tracked containers of the Rust port (docs/rust-port/lanes/).

Rust owns a namespace with core/state_mirror.py, and unported Python keeps using nested dicts and
lists that forward each write to Rust. A write that is not forwarded drifts silently, so this module
puts every document of a world in the same containers and looks for such writes:

- The production TrackedDict and TrackedList forward every write through their mutators to a sink.
  Here that is a Mirror, which applies it to a plain shadow document, by path, the way Rust applies
  it to its rows. Where a container in a tree already holds a place, this keeps both places and
  reports a write to it (write_to_shared_node), to find what production's copy would change.
- A plain container inserted into a tracked one is converted on insert, so an alias of the plain
  object no longer reaches the tree. Session keeps such aliases and reports any later change.
- At each phase end Session compares every tracked document with its shadow. It can also compare
  with a stream of digests a plain run of the same scenario recorded.

Nothing here runs in production and src/ does not import this module. See
tests/support/tracked_mirror_plugin.py for the whole test suite under the mirror, and
benchmarks/tracked_mirror_gate.py for the lockstep runs over many scenarios.
"""

from __future__ import annotations

import copy
import gc
import hashlib
import json
import os
import re
import sys
import weakref
from collections import Counter, abc
from collections.abc import Iterable, Iterator
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from sheltersim.core import state_mirror
from sheltersim.core.state_mirror import TrackedDict, TrackedList

# The fields of WorldState that are plain JSON-like trees. The Rust design owns model_state and
# metric_state through this mirror; the others are here because a write to them would matter
# the same way.
TRACKED_FIELDS = (
    "model_state",
    "metric_state",
    "model_configs",
    "resources",
    "id_counters",
    "rng_state",
)
_ATOMS = frozenset({str, int, float, bool, type(None)})
_OWN_FILES = (__file__, state_mirror.__file__)
_MISSING = object()
_active: Session | None = None


class Drift(AssertionError):
    """A tracked document and what it should equal differ. The message names where."""


def _default(value: Any) -> Any:
    if isinstance(value, abc.Sequence):
        return list(value)
    return f"<{type(value).__name__}>"


def _dumps(value: Any, *, sort: bool = False) -> str:
    """A text that differs for any two documents that differ, apart from tuple versus list."""
    try:
        return json.dumps(value, sort_keys=sort, default=_default, ensure_ascii=False)
    except TypeError:
        # A key json can't sort or write. Slower, and never equal to the form above.
        return "!" + repr(_keyed(value, sort))


def _keyed(value: Any, sort: bool) -> Any:
    if isinstance(value, dict):
        rows = [(repr(key), _keyed(item, sort)) for key, item in dict.items(value)]
        return sorted(rows) if sort else rows
    if isinstance(value, (list, tuple)):
        return [_keyed(item, sort) for item in value]
    return value


def plain_copy(value: Any) -> Any:
    """A deep copy with plain containers only; tuples stay tuples and other objects are shared."""
    kind = type(value)
    if kind in _ATOMS:
        return value
    if kind is tuple:
        return tuple(plain_copy(item) for item in value)
    if isinstance(value, (TrackedDict, TrackedList)) or kind is dict or kind is list:
        if isinstance(value, dict):
            return {key: plain_copy(item) for key, item in dict.items(value)}
        return [plain_copy(item) for item in list.__iter__(value)]
    return value


def strict_difference(first: Any, second: Any, path: tuple[Any, ...] = ()) -> str | None:
    """The first place two documents differ, by type, value, bits of a float or key order."""
    first_kind = _kind(first)
    if first_kind != _kind(second):
        return f"{_show(path)}: {first_kind} != {_kind(second)}"
    if first_kind == "dict":
        first_keys = list(dict.keys(first))
        second_keys = list(dict.keys(second))
        if first_keys != second_keys:
            return f"{_show(path)}: keys {first_keys[:8]} != {second_keys[:8]}"
        for key in first_keys:
            found = strict_difference(
                dict.__getitem__(first, key), dict.__getitem__(second, key), (*path, key)
            )
            if found is not None:
                return found
        return None
    if first_kind in {"list", "tuple"}:
        if len(first) != len(second):
            return f"{_show(path)}: {len(first)} items != {len(second)}"
        for index, (left, right) in enumerate(zip(first, second, strict=True)):
            found = strict_difference(left, right, (*path, index))
            if found is not None:
                return found
        return None
    if first_kind == "float":
        same = first.hex() == second.hex() or (first != first and second != second)
    else:
        same = first == second
    return None if same else f"{_show(path)}: {first!r} != {second!r}"


def _kind(value: Any) -> str:
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _show(path: Iterable[Any]) -> str:
    return "/".join(str(part) for part in path) or "<root>"


def _plain_view(value: Any) -> str:
    """A text of a plain container, where a tracked node inside it counts only by identity.

    The nodes are live and may change through the tree; that says nothing about the plain alias.
    """
    return _dumps(_node_tokens(value))


def _node_tokens(value: Any) -> Any:
    if isinstance(value, (TrackedDict, TrackedList)):
        return f"<node {id(value)}>"
    if type(value) is dict:
        return {key: _node_tokens(item) for key, item in value.items()}
    if type(value) is list or type(value) is tuple:
        return [_node_tokens(item) for item in value]
    return value


def _caller_site() -> str:
    """file:line of the first frame outside this module and the containers."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename in _OWN_FILES:
        frame = frame.f_back
    if frame is None:
        return "<unknown>"
    name = frame.f_code.co_filename.replace(os.sep, "/")
    marker = name.rfind("/sheltersim/")
    if marker >= 0:
        name = name[marker + 1 :]
    elif "/tests/" in name:
        name = name[name.rfind("/tests/") + 1 :]
    return f"{name}:{frame.f_lineno}"


# ---- Paths ------------------------------------------------------------------------------------


def _locate(parent: Any, node: Any, hint: Any) -> Any:
    """The key or index of node in parent, or _MISSING."""
    if isinstance(parent, dict):
        return hint if dict.get(parent, hint, _MISSING) is node else _MISSING
    size = list.__len__(parent)
    if isinstance(hint, int) and 0 <= hint < size and list.__getitem__(parent, hint) is node:
        return hint
    # Indexes in a list shift, so a path into a list has to be found at write time. This is the
    # cost a real forwarded path pays too; the census counts such writes.
    for index in range(size - 1, -1, -1):
        if list.__getitem__(parent, index) is node:
            return index
    return _MISSING


class _Record:
    """What a node of this sink knows: its _node slot. Its parents hold (weak parent, key)."""

    __slots__ = ("owner", "mirror", "parents")

    def __init__(self, owner: Any, parents: list[tuple[Any, Any]]) -> None:
        self.owner = weakref.ref(owner)
        self.mirror: Mirror | None = None
        self.parents = parents


def _paths(node: Any) -> list[tuple[Mirror, tuple[Any, ...]]]:
    """Every (mirror, path) that reaches node now. More than one means a shared node."""
    found: list[tuple[Mirror, tuple[Any, ...]]] = []
    record = node._node
    if record.mirror is not None:
        found.append((record.mirror, ()))
    alive = []
    for entry in record.parents:
        parent = entry[0]()
        if parent is None:
            continue
        key = _locate(parent, node, entry[1])
        if key is _MISSING:
            continue
        alive.append((entry[0], key))
        found.extend((mirror, (*path, key)) for mirror, path in _paths(parent))
    record.parents = alive
    return found


def _bind(node: Any, parents: list[tuple[Any, Any]]) -> None:
    node._node = _Record(node, parents)
    node._sink = _SINK


def _adopt(value: Any, parent: Any, key: Any, session: Session | None, top: bool = False) -> Any:
    """What the tracked tree stores for value: atoms as they are, containers as tracked nodes."""
    kind = type(value)
    if kind in _ATOMS:
        return value
    if kind is dict or kind is list:
        node = (_dict_class(session) if kind is dict else TrackedList)()
        _bind(node, [(weakref.ref(parent), key)] if parent is not None else [])
        if kind is dict:
            for child_key, child in dict.items(value):
                if type(child_key) is not str and session is not None:
                    session.note("non_str_key", f"{type(child_key).__name__} {_caller_site()}")
                dict.__setitem__(node, child_key, _adopt(child, node, child_key, session))
        else:
            for index, child in enumerate(value):
                list.append(node, _adopt(child, node, index, session))
        if top and session is not None:
            session.note_plain_insert(value, node)
        return node
    if isinstance(value, (TrackedDict, TrackedList)):
        if value._sink is None:
            # Built by a constructor call, such as dataclasses.asdict's type(obj)(...).
            _bind(value, [])
            if isinstance(value, dict):
                for child_key, child in dict.items(value):
                    dict.__setitem__(value, child_key, _adopt(child, value, child_key, session))
            else:
                for index, child in enumerate(list(list.__iter__(value))):
                    list.__setitem__(value, index, _adopt(child, value, index, session))
        if parent is not None:
            record = value._node
            record.parents = [
                entry
                for entry in record.parents
                if entry[0]() is not None
                and (entry[0]() is not parent or (isinstance(parent, dict) and entry[1] != key))
            ]
            record.parents.append((weakref.ref(parent), key))
        return value
    if kind is tuple:
        if session is not None and any(type(item) not in _ATOMS for item in value):
            session.note("mutable_in_tuple", _caller_site())
        return value
    if session is not None:
        session.note("untracked_value", f"{kind.__name__} at {_caller_site()}")
    return value


def _dict_class(session: Session | None) -> type:
    return ReverseReadDict if session is not None and session.reverse_reads else TrackedDict


def _forward(node: Any, op: str, *arguments: Any) -> None:
    session = _active
    paths = _paths(node)
    if not paths:
        if session is not None:
            session.note("write_to_detached_node", f"{op} at {_caller_site()}")
        return
    mirror, path = paths[0]
    if len(paths) > 1 and session is not None:
        session.note(
            "write_to_shared_node",
            f"{op} at {_caller_site()}: {_show(paths[0][1])} and {_show(paths[1][1])}",
        )
    mirror.apply(path, op, arguments)


class _Sink:
    """The sink protocol of core/state_mirror.py. Nodes stay bound when they leave a tree, so a
    later write to one is seen and reported (write_to_detached_node)."""

    @staticmethod
    def dict_set(node: Any, _record: _Record, key: Any, value: Any) -> None:
        stored = _adopt(value, node, key, _active, top=True)
        dict.__setitem__(node, key, stored)
        _forward(node, "set", key, plain_copy(stored))

    @staticmethod
    def list_append(node: Any, _record: _Record, value: Any) -> None:
        stored = _adopt(value, node, list.__len__(node), _active, top=True)
        list.append(node, stored)
        _forward(node, "append", plain_copy(stored))

    @staticmethod
    def adopt(record: _Record, key: Any, value: Any) -> Any:
        return _adopt(value, record.owner(), key, _active, top=True)

    @staticmethod
    def release(_value: Any) -> None:
        pass

    @staticmethod
    def forward(node: Any, _record: _Record, op: str, *arguments: Any) -> None:
        if op == "set":
            _forward(node, op, arguments[0], plain_copy(arguments[1]))
        elif op in {"del", "clear", "del_range", "reverse"}:
            _forward(node, op, *arguments)
        elif op == "insert":
            _forward(node, op, arguments[0], plain_copy(arguments[1]))
        elif op == "extend":
            _forward(node, op, [plain_copy(item) for item in arguments[0]])
        elif op == "slice_set":
            _forward(node, op, arguments[0], [plain_copy(item) for item in arguments[1]])
        elif op == "replace_all":
            _forward(node, op, plain_copy(node))
        else:  # pragma: no cover - only the production containers forward
            raise ValueError(op)


_SINK = _Sink()


class ReverseReadDict(TrackedDict):
    """Every iteration runs backwards, to find code that depends on dict order within a phase.

    Kernels turn this class down, since they read through the C API and would not see the order.
    """

    __slots__ = ()

    def __iter__(self) -> Iterator[Any]:
        return reversed(list(dict.keys(self)))

    def __reversed__(self) -> Iterator[Any]:
        return iter(list(dict.keys(self)))

    def keys(self) -> Any:
        return abc.KeysView(self)

    def values(self) -> Any:
        return abc.ValuesView(self)

    def items(self) -> Any:
        return abc.ItemsView(self)

    def popitem(self) -> tuple[Any, Any]:
        if not self:
            raise KeyError("popitem(): dictionary is empty")
        key = next(iter(self))
        return key, self.pop(key)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        memo[id(self)] = result
        for key, value in self.items():
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result


class Mirror:
    """The receiving side of one tracked document: a plain shadow rebuilt from forwarded writes."""

    def __init__(self, field: str, session: Session | None, root: Any) -> None:
        self.field = field
        self.session = session
        self.root = weakref.ref(root)
        self.shadow = plain_copy(root)
        self.broken: str | None = None
        self.writes = 0

    def apply(self, path: tuple[Any, ...], op: str, arguments: tuple[Any, ...]) -> None:
        self.writes += 1
        try:
            target = self.shadow
            for key in path:
                target = target[key]
            self._apply_to(target, op, arguments)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # Never disturb the simulation here; the next check reports it.
            if self.broken is None:
                self.broken = f"{op} at {_show(path)}: {exc!r}"
        if self.session is not None:
            self.session.census.write(self.field, path, op, arguments)

    @staticmethod
    def _apply_to(target: Any, op: str, arguments: tuple[Any, ...]) -> None:
        if op == "set":
            target[arguments[0]] = arguments[1]
        elif op == "del":
            del target[arguments[0]]
        elif op == "del_range":
            del target[arguments[0] : arguments[1]]
        elif op == "clear":
            target.clear()
        elif op == "append":
            target.append(arguments[0])
        elif op == "extend":
            target.extend(arguments[0])
        elif op == "insert":
            target.insert(arguments[0], arguments[1])
        elif op == "slice_set":
            target[arguments[0]] = arguments[1]
        elif op == "reverse":
            target.reverse()
        elif op == "replace_all":
            target[:] = arguments[0]
        else:  # pragma: no cover - only this module forwards
            raise ValueError(op)

    def groups(self, document: Any) -> Iterator[tuple[str, Any]]:
        """model_state by namespace, so a mismatch names one; any other field whole."""
        if self.field == "model_state" and isinstance(document, dict):
            for key, value in dict.items(document):
                yield f"model_state/{key}", value
        else:
            yield self.field, document

    def difference(self, *, sort: bool = False, strict: bool = False) -> tuple[str, str] | None:
        """(where, what) of the first difference between the document and the shadow."""
        if self.broken is not None:
            return self.field, f"a forwarded write did not apply: {self.broken}"
        root = self.root()
        if root is None:
            return None
        if list(_sorted_keys(root, sort)) != list(_sorted_keys(self.shadow, sort)):
            return self.field, (
                f"keys {_sorted_keys(root, sort)[:12]} != {_sorted_keys(self.shadow, sort)[:12]}"
            )
        shadow_groups = dict(self.groups(self.shadow))
        for name, value in self.groups(root):
            other = shadow_groups[name]
            if _dumps(value, sort=sort) != _dumps(other, sort=sort):
                found = strict_difference(value, other) if not sort else None
                return name, found or "differs when written as JSON"
            if strict:
                found = strict_difference(value, other)
                if found is not None and not sort:
                    return name, found
        return None


def _sorted_keys(document: Any, sort: bool) -> list[Any]:
    if not isinstance(document, dict):
        return []
    keys = list(dict.keys(document))
    return sorted(keys, key=repr) if sort else keys


def tracked_root(value: Any, field: str = "model_state", session: Session | None = None) -> Any:
    """Convert a plain document into a tracked tree with its own Mirror."""
    session = session if session is not None else _active
    if isinstance(value, (TrackedDict, TrackedList)):
        if mirror_of(value) is not None:
            return value
        value = plain_copy(value)
    root = _adopt(value, None, None, session)
    root._node.mirror = Mirror(field, session, root)
    return root


def mirror_of(root: Any) -> Mirror | None:
    """The Mirror of a tracked document's root, or None for anything else."""
    record = getattr(root, "_node", None)
    return record.mirror if isinstance(record, _Record) else None


def check_shadow(root: Any) -> None:
    """Raise Drift if the tracked document differs from what its forwarded writes rebuilt."""
    found = mirror_of(root).difference(strict=True)
    if found is not None:
        raise Drift(f"tracked document differs from its write stream at {found[0]}: {found[1]}")


# ---- Census ------------------------------------------------------------------------------------


_LITERAL_KEY = re.compile(r"^[A-Za-z_][A-Za-z_]*(_v\d+)?$")


class Census:
    """Per namespace path: the types seen, the kinds of writes, shared objects and aliases."""

    def __init__(self) -> None:
        self.types: dict[str, Counter[str]] = {}
        self.ops: dict[str, Counter[str]] = {}
        self.writes_by_namespace: Counter[str] = Counter()
        # A write can carry a whole namespace, so the number of writes alone misleads.
        self.values_forwarded: Counter[str] = Counter()
        self.writes_into_list_items: Counter[str] = Counter()
        self.shared: dict[str, int] = {}
        self.aliases: dict[str, dict[str, Any]] = {}
        self.known_ids: set[str] = set()
        self.ticks = 0
        self._values_seen = 0
        self._alias_baseline: int | None = None

    def template(self, field: str, path: Iterable[Any]) -> str:
        parts = [field]
        for depth, key in enumerate(path):
            if isinstance(key, int) and not isinstance(key, bool):
                parts.append("[]")
            elif not isinstance(key, str):
                parts.append(f"<{type(key).__name__} key>")
            elif key in self.known_ids:
                parts.append("*")
            elif _LITERAL_KEY.match(key) or (depth == 0 and field == "model_state"):
                parts.append(key)
            else:
                parts.append("*")
        return "/".join(parts)

    def write(self, field: str, path: tuple[Any, ...], op: str, arguments: tuple[Any, ...]) -> None:
        if field == "model_state":
            namespace = f"{field}/{path[0] if path else (arguments or ('<all>',))[0]}"
        else:
            namespace = field
        self.writes_by_namespace[namespace] += 1
        before = self._values_seen
        if any(isinstance(key, int) and not isinstance(key, bool) for key in path):
            self.writes_into_list_items[self.template(field, path)] += 1
        if op in {"set", "del"}:
            where = (*path, arguments[0])
        else:
            where = (*path, 0)
        template = self.template(field, where)
        self.ops.setdefault(template, Counter())[op] += 1
        if op == "set":
            self.value(template, arguments[1])
        elif op in {"append", "insert"}:
            self.value(template, arguments[-1])
        elif op in {"extend", "slice_set", "replace_all"}:
            for item in arguments[-1]:
                self.value(self.template(field, (*path, 0)), item)
        self.values_forwarded[namespace] += self._values_seen - before

    def value(self, template: str, value: Any) -> None:
        """Count the types under template, for one written value."""
        self._values_seen += 1
        kind = type(value)
        self.types.setdefault(template, Counter())[self._type_name(value)] += 1
        if kind is dict or kind is TrackedDict or kind is ReverseReadDict:
            for key, item in dict.items(value):
                if type(key) is not str:
                    child = f"{template}/<{type(key).__name__} key>"
                elif key in self.known_ids or not _LITERAL_KEY.match(key):
                    child = f"{template}/*"
                else:
                    child = f"{template}/{key}"
                self.value(child, item)
        elif kind is list or kind is TrackedList or kind is tuple:
            for item in value:
                self.value(f"{template}/[]", item)

    @staticmethod
    def _type_name(value: Any) -> str:
        if isinstance(value, dict):
            return "dict"
        if isinstance(value, list):
            return "list"
        return type(value).__name__

    def observe_world(self, world: Any) -> None:
        """Walk everything the world holds now: types, shared objects and held aliases."""
        for name in ("agents", "zones", "zone_edges", "facilities", "staff", "support_requests"):
            self.known_ids.update(getattr(world, name))
        self.known_ids.update(event.event_id for event in world.event_queue.pending())
        seen: dict[int, str] = {}
        for field in TRACKED_FIELDS:
            root = getattr(world, field, None)
            if isinstance(root, dict):
                self._walk(root, field, (), seen)
        for name in ("agents", "facilities", "staff", "support_requests", "active_tasks"):
            for item in getattr(world, name).values():
                self._entity(name, item)

    def _entity(self, template: str, item: Any) -> None:
        for item_field in fields(item):
            value = getattr(item, item_field.name)
            child = f"entities/{template}/{item_field.name}"
            if is_dataclass(value):
                self._entity(f"{template}/{item_field.name}", value)
            else:
                self.value(child, value)

    def _walk(self, node: Any, field: str, path: tuple[Any, ...], seen: dict[int, str]) -> None:
        template = self.template(field, path)
        earlier = seen.get(id(node))
        if earlier is not None:
            key = f"{earlier} and {template}"
            self.shared[key] = self.shared.get(key, 0) + 1
            return
        seen[id(node)] = template
        if isinstance(node, dict):
            children = list(dict.items(node))
            values = [item for _, item in children]
            keys: list[Any] = [key for key, _ in children]
            del children
        else:
            values = list(list.__iter__(node))
            keys = list(range(len(values)))
        for index, key in enumerate(keys):
            value = values[index]
            if type(key) is not str and isinstance(node, dict):
                self.types.setdefault(f"{template}/<key>", Counter())[type(key).__name__] += 1
            child = (*path, key)
            kind = type(value)
            if kind in _ATOMS:
                self.types.setdefault(self.template(field, child), Counter())[kind.__name__] += 1
            elif isinstance(value, (dict, list)) and kind in {
                dict,
                list,
                TrackedDict,
                TrackedList,
                ReverseReadDict,
            }:
                self.types.setdefault(self.template(field, child), Counter())[
                    self._type_name(value)
                ] += 1
                self._note_alias(value, field, child, (node, values))
                self._walk(value, field, child, seen)
            else:
                self.types.setdefault(self.template(field, child), Counter())[kind.__name__] += 1
                if kind is tuple:
                    self.value(self.template(field, child), value)
            del value

    def _note_alias(
        self, value: Any, field: str, path: tuple[Any, ...], walking: tuple[Any, ...]
    ) -> None:
        count = sys.getrefcount(value)
        if self._alias_baseline is None:
            self._alias_baseline = alias_baseline()
        if count <= self._alias_baseline:
            return
        template = self.template(field, path)
        row = self.aliases.setdefault(template, {"sightings": 0, "held_by": []})
        row["sightings"] += 1
        if len(row["held_by"]) < 2:
            row["held_by"].append(_holders(value, walking))

    def report(self) -> dict[str, Any]:
        return {
            "types": {key: dict(value) for key, value in sorted(self.types.items())},
            "ops": {key: dict(value) for key, value in sorted(self.ops.items())},
            "writes_by_namespace": dict(sorted(self.writes_by_namespace.items())),
            "values_forwarded": dict(sorted(self.values_forwarded.items())),
            "writes_into_list_items": dict(sorted(self.writes_into_list_items.items())),
            "shared": dict(sorted(self.shared.items())),
            "aliases": dict(sorted(self.aliases.items())),
            "ticks": self.ticks,
        }


def _holders(value: Any, walking: tuple[Any, ...]) -> list[str]:
    """What refers to value, by type: for a person reading the census, not for logic.

    Empty means the holder is invisible to gc, such as Rust or a C cache, or a plain str-keyed
    container gc has stopped tracking.
    """
    rows = []
    for holder in gc.get_referrers(value):
        kind = type(holder).__name__
        if kind == "frame" or any(holder is item for item in walking):
            continue
        if kind == "tuple" and len(holder) == 1:
            continue
        if isinstance(holder, dict):
            owners = [
                type(owner).__name__
                for owner in gc.get_referrers(holder)
                if type(owner).__name__ not in {"frame", "list", "dict", "tuple"}
            ][:2]
            rows.append(f"{kind}({','.join(sorted(owners))})")
        else:
            rows.append(kind)
    return sorted(rows)[:6]


def alias_baseline() -> int:
    """The refcount _note_alias sees for a container only its parent holds."""
    seen: list[int] = []

    class _Spy(Census):
        def _note_alias(
            self, value: Any, field: str, path: tuple[Any, ...], walking: tuple[Any, ...]
        ) -> None:
            seen.append(sys.getrefcount(value))

    _Spy()._walk({"a": {"b": []}}, "probe", (), {})
    return min(seen)


# ---- Session -----------------------------------------------------------------------------------


class Session:
    """What one patched stretch of code saw: findings, census and optional digests.

    tracked False runs the plain containers and only observes. reverse_at_phase_end reverses the
    physical order of every dict at each phase end; reverse_reads makes every tracked dict iterate
    backwards all the time. record collects a digest per engine, minute, phase and namespace;
    expect compares with a stream record collected earlier. check_every is the minutes between
    minutes with all phases compared, tick_every those between ends of ticks compared; long runs
    need them, since each comparison reads the whole document.
    """

    def __init__(
        self,
        *,
        tracked: bool = True,
        reverse_reads: bool = False,
        reverse_at_phase_end: bool = False,
        record: bool = False,
        expect: dict[tuple[int, int, str, str], bytes] | None = None,
        check_every: int = 1,
        tick_every: int = 1,
        census_every: int = 0,
        raise_on_drift: bool = True,
    ) -> None:
        self.tracked = tracked
        self.reverse_reads = reverse_reads
        self.reverse_at_phase_end = reverse_at_phase_end
        self.record = record
        self.stream: dict[tuple[int, int, str, str], bytes] = {}
        self.expect = expect
        self.check_every = check_every
        self.tick_every = tick_every
        self.census_every = census_every
        self.raise_on_drift = raise_on_drift
        self.census = Census()
        self.findings: dict[str, Counter[str]] = {}
        self.drifts: list[str] = []
        self.compared_phases = 0
        self.compared_shadow = 0
        self.minute: int | None = None
        self.phase: str | None = None
        self.roots: dict[int, Any] = {}
        self._engines = 0
        self._retained: list[dict[str, Any]] = []
        self._restore: list[tuple[Any, ...]] = []

    # -- Findings

    def note(self, kind: str, detail: str) -> None:
        self.findings.setdefault(kind, Counter())[detail] += 1

    def note_plain_insert(self, plain: Any, node: Any) -> None:
        """Keep a plain container just converted on insert, to see if its alias changes later."""
        if len(self._retained) >= 5000:
            self.check_plain_aliases()
        self._retained.append(
            {
                "plain": plain,
                "snapshot": _plain_view(plain),
                "node": weakref.ref(node),
                "site": _caller_site(),
                "ids": id(plain),
            }
        )

    def check_plain_aliases(self) -> None:
        kept = []
        seen_ids: dict[int, str] = {}
        for entry in self._retained:
            plain = entry["plain"]
            if entry["ids"] in seen_ids and seen_ids[entry["ids"]] != entry["site"]:
                self.note(
                    "same_plain_object_inserted_twice",
                    f"{seen_ids[entry['ids']]} and {entry['site']}",
                )
            seen_ids[entry["ids"]] = entry["site"]
            current = _plain_view(plain)
            if current != entry["snapshot"]:
                self.note("plain_alias_changed_after_insert", entry["site"])
                entry["snapshot"] = current
            # Held by the entry, the local name and getrefcount's argument.
            if sys.getrefcount(plain) > 3:
                node = entry["node"]()
                sort = self._sorted_digests()
                if node is not None and _dumps(node, sort=sort) != _dumps(plain, sort=sort):
                    self.note("plain_alias_stale", entry["site"])
                kept.append(entry)
            del plain
        self._retained = kept

    # -- Patching

    def __enter__(self) -> Session:
        global _active
        from sheltersim.core.engine import TickEngine, no_op_phase_handler
        from sheltersim.core.world import WorldState

        session = self
        _active = self
        original_init = TickEngine.__init__
        original_setattr = WorldState.__dict__.get("__setattr__")

        def engine_init(
            engine: Any,
            world: Any,
            *,
            phase_handler: Any = no_op_phase_handler,
            **options: Any,
        ) -> None:
            number = session._engines
            session._engines += 1

            def observed(phase: Any, phase_world: Any, minute: int, rng: Any) -> None:
                session.before_phase(phase_world, minute, phase)
                phase_handler(phase, phase_world, minute, rng)
                session.after_phase(number, phase_world, minute, phase)

            original_init(engine, world, phase_handler=observed, **options)

        def world_setattr(world: Any, name: str, value: Any) -> None:
            if name in TRACKED_FIELDS and isinstance(value, (dict, list)):
                value = session.track(value, name)
            object.__setattr__(world, name, value)

        TickEngine.__init__ = engine_init  # type: ignore[method-assign]
        self._restore.append((TickEngine, "__init__", original_init))
        if self.tracked:
            WorldState.__setattr__ = world_setattr  # type: ignore[method-assign]
            self._restore.append((WorldState, "__setattr__", original_setattr))
            # This sink stands in for the production one, so nothing is owned and the Python rules
            # run on the shadowed documents; a plain session of the same scenario runs the kernels.
            for name in ("OWNED_NAMESPACES", "OWNED_FIELDS"):
                self._restore.append((state_mirror, name, getattr(state_mirror, name)))
                setattr(state_mirror, name, {})
        return self

    def __exit__(self, *_exception: Any) -> None:
        global _active
        _active = None
        for owner, name, value in self._restore:
            if value is None:
                delattr(owner, name)
            else:
                setattr(owner, name, value)
        self._restore.clear()

    def track(self, value: Any, field: str) -> Any:
        root = tracked_root(value, field, self)
        id_of = id(root)
        # By id: a dict subclass isn't hashable, so a WeakSet can't hold it.
        self.roots[id(root)] = weakref.ref(root, lambda _: self.roots.pop(id_of, None))
        return root

    # -- Phases

    def before_phase(self, world: Any, minute: int, phase: Any) -> None:
        self.minute, self.phase = minute, str(phase)
        if self.tracked:
            # A world made without __setattr__: deepcopy, pickle.
            for field in TRACKED_FIELDS:
                value = getattr(world, field, None)
                if type(value) is dict or type(value) is list:
                    self.note("plain_root_at_phase_start", field)
                    setattr(world, field, value)

    def after_phase(self, number: int, world: Any, minute: int, phase: Any) -> None:
        name = str(phase)
        self.minute, self.phase = minute, name
        last_phase = name == "collect_metrics"
        at_stride = self.check_every <= 1 or minute % self.check_every == 0
        at_tick_stride = last_phase and minute % self.tick_every == 0
        if self.tracked:
            self.check_plain_aliases()
            if at_stride or at_tick_stride:
                self._check_world_shadow(world, minute, name)
        if self.census_every and minute % self.census_every == 0:
            self.census.observe_world(world)
        if self.record or self.expect is not None:
            if at_stride or at_tick_stride:
                self._compare_stream(number, world, minute, name, last_phase)
        if self.reverse_at_phase_end:
            reverse_world_dicts(world)
            if self.tracked:
                for field in TRACKED_FIELDS:
                    value = getattr(world, field, None)
                    mirror = mirror_of(value)
                    if mirror is not None:
                        reverse_tree(mirror.shadow)
        if last_phase:
            self.census.ticks += 1

    def _sorted_digests(self) -> bool:
        return self.reverse_at_phase_end or self.reverse_reads

    def _check_world_shadow(self, world: Any, minute: int, phase: str) -> None:
        self.compared_shadow += 1
        sort = self._sorted_digests()
        for field in TRACKED_FIELDS:
            value = getattr(world, field, None)
            mirror = mirror_of(value)
            if mirror is None:
                continue
            found = mirror.difference(sort=sort, strict=False)
            if found is not None:
                self._drift(
                    f"minute {minute} phase {phase} {found[0]}: "
                    f"tracked document differs from its write stream: {found[1]}"
                )

    def digests(self, world: Any, whole_tick: bool) -> dict[str, bytes]:
        rows = {}
        for field in TRACKED_FIELDS:
            value = getattr(world, field, None)
            if field == "model_state" and isinstance(value, dict):
                for key, item in dict.items(value):
                    rows[f"model_state/{key}"] = _digest(_dumps(item, sort=True))
                rows["model_state"] = _digest(_dumps(sorted(dict.keys(value))))
            else:
                rows[field] = _digest(_dumps(value, sort=True))
        if whole_tick:
            # Key order as well, once a tick, except where a run reverses it on purpose.
            for key, item in dict.items(world.model_state):
                rows[f"key_order/{key}"] = _digest(_dumps(item))
            document = world._canonical_data(include_event_log=False)
            for key in (*TRACKED_FIELDS, "event_log"):
                document.pop(key, None)
            rows["entities"] = _digest(_dumps(document, sort=True))
            rows["event_log"] = _digest(str(len(world.event_log)))
        return rows

    def _compare_stream(
        self, number: int, world: Any, minute: int, phase: str, whole_tick: bool
    ) -> None:
        rows = self.digests(world, whole_tick)
        if self.record:
            for group, value in rows.items():
                self.stream[(number, minute, phase, group)] = value
        if self.expect is not None:
            self.compared_phases += 1
            for group, value in rows.items():
                expected = self.expect.get((number, minute, phase, group))
                if group.startswith("key_order/"):
                    # A kernel and the Python code it replaces may build a dict in different
                    # orders. Not state: the reversing runs show that no result depends on it.
                    if expected != value and not self._sorted_digests():
                        self.note("key_order_differs_from_plain", group)
                    continue
                if expected != value:
                    state = "absent from" if expected is None else "differs from"
                    self._drift(
                        f"engine {number} minute {minute} phase {phase} {group}: "
                        f"{state} the plain run"
                    )

    def _drift(self, message: str) -> None:
        self.drifts.append(message)
        if self.raise_on_drift:
            raise Drift(message)

    def check_all_roots(self) -> None:
        """Compare every live tracked document with its shadow, whatever owns it now."""
        for reference in list(self.roots.values()):
            root = reference()
            if root is None:
                continue
            found = mirror_of(root).difference(sort=self._sorted_digests())
            if found is not None:
                self._drift(
                    f"after minute {self.minute} phase {self.phase} {found[0]}: "
                    f"tracked document differs from its write stream: {found[1]}"
                )

    def report(self) -> dict[str, Any]:
        return {
            "drifts": self.drifts[:20],
            "drift_count": len(self.drifts),
            "findings": {key: dict(value) for key, value in sorted(self.findings.items())},
            "phases_compared_with_plain": self.compared_phases,
            "phases_compared_with_shadow": self.compared_shadow,
            "engines": self._engines,
            "census": self.census.report(),
        }


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=12).digest()


def reverse_tree(value: Any) -> None:
    """Reverse the physical order of every dict under value. No forwarding: order is not state."""
    if isinstance(value, dict):
        rows = list(dict.items(value))
        dict.clear(value)
        for key, item in reversed(rows):
            dict.__setitem__(value, key, item)
            reverse_tree(item)
    elif isinstance(value, list):
        for item in list.__iter__(value):
            reverse_tree(item)


def reverse_world_dicts(world: Any) -> None:
    """Reverse every tree dict and the entity containers. Lists keep their meaningful order."""
    for field in TRACKED_FIELDS:
        reverse_tree(getattr(world, field, None))
    for name in ("agents", "zones", "zone_edges", "facilities", "staff", "support_requests"):
        reverse_tree_top = getattr(world, name)
        rows = list(reverse_tree_top.items())
        reverse_tree_top.clear()
        reverse_tree_top.update(reversed(rows))


# ---- Worker processes ---------------------------------------------------------------------------


def _worker_entry(report: str, options: dict[str, Any], *arguments: Any) -> None:
    from sheltersim.web.worker_process import run_worker

    session = Session(**options)
    try:
        with session:
            run_worker(*arguments)
    finally:
        Path(report).write_text(json.dumps(session.report()), encoding="utf-8")


def patch_worker_processes(directory: Path, options: dict[str, Any]) -> Any:
    """Run ProjectAPI's spawned workers under a Session too. Returns the undo function."""
    from sheltersim.web import worker_process

    context = worker_process._CONTEXT
    counter = iter(range(1_000_000))

    class _Context:
        def __getattr__(self, name: str) -> Any:
            return getattr(context, name)

        def Process(self, *, target: Any, args: Any, name: str) -> Any:  # noqa: N802
            report = directory / f"worker-{os.getpid()}-{next(counter)}.json"
            return context.Process(
                target=_worker_entry,
                args=(str(report), options, *args),
                name=name,
            )

    worker_process._CONTEXT = _Context()  # type: ignore[assignment]

    def undo() -> None:
        worker_process._CONTEXT = context

    return undo
