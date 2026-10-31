"""The tree unported Python reads and writes for a namespace Rust owns.

A world keeps its Rust state in `world.__dict__["_core"]`, made only with the Rust kernels on. A
namespace is owned when it is listed in OWNED_NAMESPACES: Rust holds it in a typed row, and
`world.model_state[name]` is a tree of TrackedDict and TrackedList. These are real dicts and lists,
so isinstance, json, == and every read work as before and run the inherited C code. Every mutator
also tells the tree's sink, which keeps the row equal to the tree. A plain container is converted
when it is inserted, so a write through a name it had before the insert reaches neither. See
docs/rust-port/lanes/p4-world-core.md for what a lane does.

The maps of entities are IdMap, which keep the Rust rows of the entities equal to the objects
(docs/rust-port/lanes/p6-entity-windows.md). With SHELTERSIM_NATIVE=0 a world has no core at all,
and these maps and containers are plain dicts and lists.

A sink is sheltersim_native.NamespaceMirror, or a test's object with the same methods
(tests/support/tracked_mirror.py). A node's _sink is None out of any tree: it then behaves as the
plain container, like the value `pop` returns, and an insert moves it in as itself.
"""

from __future__ import annotations

import copy
import operator
from collections.abc import Callable, Iterator
from typing import Any

from sheltersim import native

_MISSING = object()


class DetachedNodeError(RuntimeError):
    """A write to a node of a detached namespace, which could reach neither the row nor a tree."""


class _DeadSink:
    """The sink of a node whose namespace was detached. Reading such a node is reading the past."""

    def __getattr__(self, name: str) -> Any:
        raise DetachedNodeError(
            "a tree node was kept across a detach of its namespace and then written; "
            "read the node again from world.model_state"
        )


_DEAD_SINK = _DeadSink()


class TrackedDict(dict):
    """A dict whose mutators tell its sink. Reads are the inherited C code."""

    __slots__ = ("_sink", "_node", "__weakref__")

    def __new__(cls, *_arguments: Any, **_keywords: Any) -> TrackedDict:
        self = super().__new__(cls)
        self._sink = None
        self._node = None
        return self

    def __init__(self, *arguments: Any, **keywords: Any) -> None:
        if self._sink is None:
            dict.__init__(self, *arguments, **keywords)
        else:
            self.update(*arguments, **keywords)

    def __setitem__(self, key: Any, value: Any) -> None:
        sink = self._sink
        if sink is None:
            dict.__setitem__(self, key, value)
        else:
            sink.dict_set(self, self._node, key, value)

    def __delitem__(self, key: Any) -> None:
        sink = self._sink
        if sink is None:
            dict.__delitem__(self, key)
            return
        forward = sink.forward
        # pop raises the same KeyError(key) as del.
        old = dict.pop(self, key)
        sink.release(old)
        forward(self, self._node, "del", key)

    def pop(self, key: Any, *default: Any) -> Any:
        sink = self._sink
        if sink is None:
            return dict.pop(self, key, *default)
        forward = sink.forward
        value = dict.pop(self, key, _MISSING)
        if value is _MISSING:
            return dict.pop(self, key, *default)
        sink.release(value)
        forward(self, self._node, "del", key)
        return value

    def popitem(self) -> tuple[Any, Any]:
        sink = self._sink
        if sink is None:
            return dict.popitem(self)
        forward = sink.forward
        key, value = dict.popitem(self)
        sink.release(value)
        forward(self, self._node, "del", key)
        return key, value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if self._sink is None or key in self:
            return dict.setdefault(self, key, default)
        self[key] = default
        # The stored node, not the plain default: this is what makes setdefault's result safe.
        return dict.__getitem__(self, key)

    def update(self, *arguments: Any, **keywords: Any) -> None:
        if self._sink is None:
            dict.update(self, *arguments, **keywords)
            return
        for key, value in dict(*arguments, **keywords).items():
            self[key] = value

    def clear(self) -> None:
        sink = self._sink
        if sink is None:
            dict.clear(self)
            return
        forward = sink.forward
        old = list(dict.values(self))
        dict.clear(self)
        for value in old:
            sink.release(value)
        forward(self, self._node, "clear")

    def __ior__(self, other: Any) -> TrackedDict:
        self.update(other)
        return self

    def copy(self) -> dict[Any, Any]:
        return dict(self)

    def __copy__(self) -> dict[Any, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        memo[id(self)] = result
        for key, value in dict.items(self):
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result

    def __reduce_ex__(self, _protocol: Any) -> tuple[Any, ...]:
        return (dict, (dict(self),))


class TrackedList(list):
    """A list whose mutators tell its sink. Reads are the inherited C code."""

    __slots__ = ("_sink", "_node", "__weakref__")

    def __new__(cls, *_arguments: Any, **_keywords: Any) -> TrackedList:
        self = super().__new__(cls)
        self._sink = None
        self._node = None
        return self

    def __init__(self, *arguments: Any) -> None:
        if self._sink is None:
            list.__init__(self, *arguments)
        else:
            self[:] = list(*arguments)

    def __setitem__(self, index: Any, value: Any) -> None:
        sink = self._sink
        if sink is None:
            list.__setitem__(self, index, value)
        elif isinstance(index, slice):
            self._set_slice(sink, index, value)
        else:
            self._set_index(sink, index, value)

    def _set_index(self, sink: Any, index: Any, value: Any) -> None:
        size = list.__len__(self)
        if not hasattr(index, "__index__") or not -size <= operator.index(index) < size:
            list.__setitem__(self, index, value)  # raises list's own error
        position = operator.index(index)
        position = position + size if position < 0 else position
        old = list.__getitem__(self, position)
        if old is value:
            return
        forward = sink.forward
        stored = sink.adopt(self._node, None, value)
        list.__setitem__(self, position, stored)
        sink.release(old)
        forward(self, self._node, "set", position, stored)

    def _set_slice(self, sink: Any, index: slice, value: Any) -> None:
        forward = sink.forward
        if not hasattr(value, "__iter__") and not hasattr(value, "__getitem__"):
            list.__setitem__(self, index, value)  # raises list's own error
        items = list(value)
        size = list.__len__(self)
        old = list.__getitem__(self, index)
        # Stored as they are first, so a wrong size raises list's own error before any change.
        list.__setitem__(self, index, items)
        for item in old:
            sink.release(item)
        start, stop, step = index.indices(size)
        positions = range(start, start + len(items)) if step == 1 else range(start, stop, step)
        stored = []
        for position in positions:
            node = sink.adopt(self._node, None, list.__getitem__(self, position))
            list.__setitem__(self, position, node)
            stored.append(node)
        forward(self, self._node, "slice_set", index, stored)

    def __delitem__(self, index: Any) -> None:
        sink = self._sink
        if sink is None:
            list.__delitem__(self, index)
            return
        forward = sink.forward
        size = list.__len__(self)
        if isinstance(index, slice):
            start, stop, step = index.indices(size)
            old = list.__getitem__(self, index)
            list.__delitem__(self, index)
            if not old:
                return  # the usual `del history[:-N]` of a list within its limit
            for item in old:
                sink.release(item)
            if step == 1:
                forward(self, self._node, "del_range", start, stop)
            else:
                forward(self, self._node, "replace_all")
            return
        if not hasattr(index, "__index__") or not -size <= operator.index(index) < size:
            list.__delitem__(self, index)  # raises list's own error
        position = operator.index(index)
        position = position + size if position < 0 else position
        old = list.__getitem__(self, position)
        list.__delitem__(self, position)
        sink.release(old)
        forward(self, self._node, "del", position)

    def append(self, value: Any) -> None:
        sink = self._sink
        if sink is None:
            list.append(self, value)
        else:
            sink.list_append(self, self._node, value)

    def extend(self, values: Any) -> None:
        sink = self._sink
        if sink is None:
            list.extend(self, values)
            return
        forward = sink.forward
        stored = [sink.adopt(self._node, None, item) for item in list(values)]
        list.extend(self, stored)
        forward(self, self._node, "extend", stored)

    def insert(self, index: Any, value: Any) -> None:
        sink = self._sink
        if sink is None:
            list.insert(self, index, value)
            return
        size = list.__len__(self)
        position = operator.index(index)
        position = max(position + size, 0) if position < 0 else min(position, size)
        forward = sink.forward
        stored = sink.adopt(self._node, None, value)
        list.insert(self, position, stored)
        forward(self, self._node, "insert", position, stored)

    def remove(self, value: Any) -> None:
        if self._sink is None or value not in self:
            list.remove(self, value)
        else:
            del self[list.index(self, value)]

    def pop(self, index: Any = -1) -> Any:
        sink = self._sink
        if sink is None:
            return list.pop(self, index)
        forward = sink.forward
        position = operator.index(index)
        if position < 0:
            position += list.__len__(self)
        value = list.pop(self, index)
        sink.release(value)
        forward(self, self._node, "del", position)
        return value

    def clear(self) -> None:
        sink = self._sink
        if sink is None:
            list.clear(self)
            return
        forward = sink.forward
        old = list(list.__iter__(self))
        list.clear(self)
        for item in old:
            sink.release(item)
        forward(self, self._node, "clear")

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        sink = self._sink
        if sink is None:
            list.sort(self, key=key, reverse=reverse)
            return
        forward = sink.forward
        list.sort(self, key=key, reverse=reverse)
        forward(self, self._node, "replace_all")

    def reverse(self) -> None:
        sink = self._sink
        if sink is None:
            list.reverse(self)
            return
        forward = sink.forward
        list.reverse(self)
        forward(self, self._node, "reverse")

    def __iadd__(self, values: Any) -> TrackedList:
        self.extend(values)
        return self

    def __imul__(self, count: Any) -> TrackedList:
        if not hasattr(count, "__index__"):
            # The operator's own text; list.__imul__ called by name words it differently.
            kind = type(count).__name__
            raise TypeError(f"can't multiply sequence by non-int of type '{kind}'")
        sink = self._sink
        if sink is None:
            return list.__imul__(self, count)
        forward = sink.forward
        size = list.__len__(self)
        if operator.index(count) <= 0:
            self.clear()
            return self
        list.__imul__(self, count)
        # Each repeat is an item of its own: a node has one place.
        for position in range(size, list.__len__(self)):
            list.__setitem__(
                self, position, sink.adopt(self._node, None, list.__getitem__(self, position))
            )
        forward(self, self._node, "replace_all")
        return self

    def copy(self) -> list[Any]:
        return list(self)

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        result: list[Any] = []
        memo[id(self)] = result
        for value in list.__iter__(self):
            result.append(copy.deepcopy(value, memo))
        return result

    def __reduce_ex__(self, _protocol: Any) -> tuple[Any, ...]:
        return (list, (list(self),))


class NamespaceTable(dict):
    """world.model_state while a namespace is owned. Reads are the inherited C code.

    The namespaces no lane owns are stored as they are; each owned one is a tree with a mirror.
    Writing a whole owned namespace rebuilds its tree and row, so it costs O(namespace): the code
    doing it per tick moves with the namespace's port.
    """

    __slots__ = ("_mirrors", "__weakref__")

    def __new__(cls, *_arguments: Any, **_keywords: Any) -> NamespaceTable:
        self = super().__new__(cls)
        self._mirrors = {}
        return self

    def __setitem__(self, key: Any, value: Any) -> None:
        mirror = self._mirrors.get(key)
        if mirror is None:
            dict.__setitem__(self, key, value)
        elif dict.get(self, key, _MISSING) is not value:
            dict.__setitem__(self, key, mirror.replace_root(value))

    def __delitem__(self, key: Any) -> None:
        dict.__delitem__(self, key)
        mirror = self._mirrors.get(key)
        if mirror is not None:
            mirror.clear_root()

    def pop(self, key: Any, *default: Any) -> Any:
        mirror = self._mirrors.get(key)
        if mirror is None:
            return dict.pop(self, key, *default)
        value = dict.pop(self, key, _MISSING)
        if value is _MISSING:
            return dict.pop(self, key, *default)
        mirror.clear_root()
        return value

    def popitem(self) -> tuple[Any, Any]:
        key, value = dict.popitem(self)
        mirror = self._mirrors.get(key)
        if mirror is not None:
            mirror.clear_root()
        return key, value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return dict.__getitem__(self, key)

    def update(self, *arguments: Any, **keywords: Any) -> None:
        for key, value in dict(*arguments, **keywords).items():
            self[key] = value

    def clear(self) -> None:
        names = list(dict.keys(self))
        dict.clear(self)
        for name in names:
            mirror = self._mirrors.get(name)
            if mirror is not None:
                mirror.clear_root()

    def __ior__(self, other: Any) -> NamespaceTable:
        self.update(other)
        return self

    def copy(self) -> dict[Any, Any]:
        return dict(self)

    def __copy__(self) -> dict[Any, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        memo[id(self)] = result
        for key, value in dict(self).items():
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result

    def __reduce_ex__(self, _protocol: Any) -> tuple[Any, ...]:
        return (dict, (dict(self),))


class _DetachingTable(NamespaceTable):
    """model_state while a namespace is detached: its key stays, holding a placeholder.

    Keys, len and `in` read the right thing as they are. Whatever reads a value builds it first, so
    the placeholder never gets out. Kernels turn this class down, so the C API can't see it either
    (core/tracked.rs), and json.dumps calls items(). The class goes back to NamespaceTable, with
    C-speed reads, when nothing is detached any more.
    """

    __slots__ = ()

    def _build(self, key: Any) -> None:
        if dict.get(self, key, None) is _DETACHED:
            _rebuild(self, key)

    def _build_all(self) -> None:
        for key, value in list(dict.items(self)):
            if value is _DETACHED:
                _rebuild(self, key)

    def __getitem__(self, key: Any) -> Any:
        self._build(key)
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._build(key)
        return dict.get(self, key, default)

    # Without this, dict(table) takes its C path and reads the placeholder. keys() stays right.
    def __iter__(self) -> Iterator[Any]:
        return dict.__iter__(self)

    def values(self) -> Any:
        self._build_all()
        return dict.values(self)

    def items(self) -> Any:
        self._build_all()
        return dict.items(self)

    def pop(self, key: Any, *default: Any) -> Any:
        self._build(key)
        return NamespaceTable.pop(self, key, *default)

    def popitem(self) -> tuple[Any, Any]:
        self._build_all()
        return dict.popitem(self)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._build(key)
        return NamespaceTable.setdefault(self, key, default)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._build(key)
        NamespaceTable.__setitem__(self, key, value)

    def __delitem__(self, key: Any) -> None:
        self._build(key)
        NamespaceTable.__delitem__(self, key)

    def __eq__(self, other: object) -> bool:
        self._build_all()
        return dict.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        self._build_all()
        return dict.__ne__(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __or__(self, other: Any) -> Any:
        self._build_all()
        return dict.__or__(self, other)

    def __ror__(self, other: Any) -> Any:
        self._build_all()
        return dict.__ror__(self, other)

    def __repr__(self) -> str:
        self._build_all()
        return dict.__repr__(self)

    def copy(self) -> dict[Any, Any]:
        self._build_all()
        return dict(dict.items(self))

    __copy__ = copy

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        self._build_all()
        return NamespaceTable.__deepcopy__(self, memo)

    def __reduce_ex__(self, _protocol: Any) -> tuple[Any, ...]:
        self._build_all()
        return (dict, (dict(dict.items(self)),))


_DETACHED = object()


def _rebuild(table: NamespaceTable, key: Any) -> None:
    dict.__setitem__(table, key, table._mirrors[key].rebuild())
    if not any(value is _DETACHED for value in dict.values(table)):
        table.__class__ = NamespaceTable


def detach(world: Any, name: str) -> bool:
    """Drop the tree of an owned namespace with no per-tick Python reader left.

    The row alone then holds it, and the next Python read rebuilds the tree. A node kept from
    before raises DetachedNodeError on a write, and its reads are of the past. Returns False for a
    tainted or absent namespace, which stays as it is.
    """
    table = world.model_state
    mirror = table._mirrors[name]
    if dict.get(table, name, _MISSING) is _MISSING or not mirror.detach(_DEAD_SINK):
        return False
    dict.__setitem__(table, name, _DETACHED)
    table.__class__ = _DetachingTable
    return True


# ---- The entities of a world (native/src/state/entities.rs) --------------------------------------


class IdMap(dict):
    """world.agents and the other maps of entities. Reads are the inherited C code.

    An entity stored here is attached: its row is in the world's EntityStore and its own writes go
    there (domain/models.py). An entity that leaves works on as a plain object, and a map that
    leaves its world (`_store` None) is a plain dict of plain objects.
    """

    __slots__ = ("_store", "_table", "__weakref__")

    def __new__(cls, *_arguments: Any, **_keywords: Any) -> IdMap:
        self = super().__new__(cls)
        self._store = None
        self._table = 0
        return self

    def __init__(self, *arguments: Any, **keywords: Any) -> None:
        if self._store is None:
            dict.__init__(self, *arguments, **keywords)
        else:
            self.update(*arguments, **keywords)

    def __setitem__(self, key: Any, value: Any) -> None:
        store = self._store
        if store is None:
            dict.__setitem__(self, key, value)
        else:
            store.map_set(self, self._table, key, value)

    def __delitem__(self, key: Any) -> None:
        store = self._store
        if store is None:
            dict.__delitem__(self, key)
            return
        # pop raises the same KeyError(key) as del.
        store.map_removed(self._table, key, dict.pop(self, key))

    def pop(self, key: Any, *default: Any) -> Any:
        store = self._store
        if store is None:
            return dict.pop(self, key, *default)
        value = dict.pop(self, key, _MISSING)
        if value is _MISSING:
            return dict.pop(self, key, *default)
        store.map_removed(self._table, key, value)
        return value

    def popitem(self) -> tuple[Any, Any]:
        key, value = dict.popitem(self)
        store = self._store
        if store is not None:
            store.map_removed(self._table, key, value)
        return key, value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return dict.__getitem__(self, key)

    def update(self, *arguments: Any, **keywords: Any) -> None:
        if self._store is None:
            dict.update(self, *arguments, **keywords)
            return
        for key, value in dict(*arguments, **keywords).items():
            self[key] = value

    def clear(self) -> None:
        store = self._store
        if store is None:
            dict.clear(self)
            return
        items = list(dict.items(self))
        dict.clear(self)
        for key, value in items:
            store.map_removed(self._table, key, value)

    def __ior__(self, other: Any) -> IdMap:
        self.update(other)
        return self

    def copy(self) -> dict[Any, Any]:
        return dict(self)

    def __copy__(self) -> dict[Any, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        memo[id(self)] = result
        for key, value in dict.items(self):
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result

    def __reduce_ex__(self, _protocol: Any) -> tuple[Any, ...]:
        return (dict, (dict(self),))


# world.<name> -> the table of the EntityStore: the numbers of native/src/state/entities.rs.
ENTITY_FIELDS = {
    "agents": 0,
    "facilities": 2,
    "staff": 3,
    "support_requests": 4,
    "active_tasks": 5,
}


def _attach_entities(world: Any, core: Any) -> None:
    store = core.entities()
    state = world.__dict__
    for name, table in ENTITY_FIELDS.items():
        state[name] = store.assign(table, None, state[name])


def entities_assigned(world: Any, name: str, value: Any) -> Any:
    """What world.<name> stores when a caller assigns a whole map of entities to a world."""
    current = world.__dict__.get(name)
    if value is current:
        return value
    return world.__dict__["_core"].entities().assign(ENTITY_FIELDS[name], current, value)


# ---- What is owned, and what Rust does instead of Python ----------------------------------------

# name of a namespace -> (the module of native/src/state/, the name of its row type). A lane adds
# its line here and nothing to core/world.py. Empty, a world's model_state stays a plain dict.
OWNED_NAMESPACES: dict[str, tuple[str, str]] = {
    "psychological_distress_v1": ("psychological_distress", "ROW"),
    "information_spread_v1": ("information", "ROW"),
    "social_propagation_v1": ("social_propagation", "ROW"),
    "movement_v1": ("movement", "ROW"),
}

# The same for a field of WorldState that is not in model_state. A whole assignment to such a field
# is not caught: the field then holds a plain dict, the kernels say Unsupported (field_mirror_of in
# world_core.rs) and the Python rules run on it.
OWNED_FIELDS: dict[str, tuple[str, str]] = {
    "metric_state": ("metric_state", "ROW"),
}

# name of a namespace -> the kernel that accepts it: kernel(world) is True only if the namespace
# passes every rule its Python validation checks; anything else, or Unsupported, runs the rules.
# Filled by register_rust_validator from the lane's module, read at each validation site through
# rust_validated.
_RUST_VALIDATORS: dict[str, Callable[[Any], bool]] = {}


def register_rust_validator(name: str, kernel: Callable[[Any], bool] | None) -> None:
    """Say a kernel can accept this namespace; None takes that back. Without kernels, a no-op."""
    if kernel is None:
        _RUST_VALIDATORS.pop(name, None)
    elif native.available():
        _RUST_VALIDATORS[name] = kernel


def rust_validated(world: Any, name: str) -> bool:
    """Whether Rust accepts this namespace now, so its Python validation can be skipped.

    Called where that validation runs, so the errors of the others keep their order. A false says
    nothing about the state. A kernel reads its row through with_row, which raises Unsupported for
    a tainted namespace, so that one is always false.
    """
    if not _RUST_VALIDATORS:
        return False
    kernel = _RUST_VALIDATORS.get(name)
    if kernel is None:
        return False
    try:
        return kernel(world) is True
    except native.Unsupported:
        return False


def _attach_owned(world: Any, core: Any) -> None:
    table = NamespaceTable()
    for name, (module, row) in OWNED_NAMESPACES.items():
        table._mirrors[name] = core.own(name, getattr(native.state(module), row))
    current = world.__dict__["model_state"]
    # Anything but a mapping stays as it is, for validation to say what is wrong with it.
    if isinstance(current, dict):
        _fill_table(table, current)
        world.__dict__["model_state"] = table


def _fill_table(table: NamespaceTable, current: Any) -> None:
    # In the order of the document, so a world's order does not depend on what is owned.
    for key, value in current.items():
        mirror = table._mirrors.get(key)
        dict.__setitem__(table, key, value if mirror is None else mirror.replace_root(value))


def _attach_fields(world: Any, core: Any) -> None:
    for name, (module, row) in OWNED_FIELDS.items():
        mirror = core.own(name, getattr(native.state(module), row))
        current = world.__dict__[name]
        # Anything but a mapping stays as it is; the kernels then say Unsupported.
        if isinstance(current, dict):
            world.__dict__[name] = mirror.replace_root(current)


def start_core(world: Any) -> None:
    """Give a new world its core, and own what is listed. Only with the kernels on."""
    core = native.world_core()
    world.__dict__["_core"] = core
    _attach_entities(world, core)
    if OWNED_NAMESPACES:
        _attach_owned(world, core)
    _attach_fields(world, core)


def model_state_assigned(world: Any, value: Any) -> Any:
    """What world.model_state stores when a caller assigns value to a world with owned namespaces.

    The table it replaces becomes a plain holder of plain containers, as a name of it would see.
    """
    table = world.__dict__.get("model_state")
    if value is table or not isinstance(table, NamespaceTable):
        return value
    table.items()  # builds whatever is detached
    mirrors, table._mirrors = table._mirrors, {}
    for mirror in mirrors.values():
        mirror.clear_root()
    new_table = NamespaceTable()
    new_table._mirrors = mirrors
    if isinstance(value, dict):
        _fill_table(new_table, value)
        return new_table
    # Not a mapping at all: validation will say so.
    return value


def deepcopy_world(world: Any, memo: dict[int, Any]) -> Any:
    """deepcopy(world): the state is copied as plain containers, and owned again by a new core."""
    result = object.__new__(type(world))
    memo[id(world)] = result
    state = result.__dict__
    for key, value in world.__dict__.items():
        if key != "_core":
            state[key] = copy.deepcopy(value, memo)
    if "_core" in world.__dict__:
        start_core(result)
    return result


native.install_tracked_types(TrackedDict, TrackedList, NamespaceTable)
native.install_id_map_type(IdMap)
