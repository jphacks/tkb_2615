"""The production tracked containers do what plain ones do, and tell their sink.

Each mutator runs on a plain document and on a tree owned by a Rust row that holds any JSON: the two
must end the same, with the same errors, and the row must write the same canonical JSON as the tree.
"""

from __future__ import annotations

import copy
import gc
import json
import operator
import pickle
import random
import weakref
from collections.abc import Callable
from typing import Any

import pytest
from support.tracked_mirror import strict_difference

from sheltersim import native
from sheltersim.core.state_mirror import NamespaceTable, TrackedDict, TrackedList

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")


def _tree(document: Any) -> tuple[Any, Any]:
    mirror = native.world_core().own("x", native.state("sample_namespace").VALUE_ROW)
    return mirror.replace_root(document), mirror


def _document() -> dict[str, Any]:
    return {
        "a": 1,
        "b": {"c": [1, 2, 3], "d": {"e": 1.5}},
        "l": [3, 1, 2],
        "m": [{"x": 1}, {"x": 2}, {"x": 3}],
    }


def _outcome(change: Callable[[Any], Any], document: Any) -> Any:
    try:
        return ("returned", _plain(change(document)))
    except Exception as exc:  # noqa: BLE001 - each error is compared with the plain one
        return ("raised", type(exc), str(exc))


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in dict.items(value)}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _same_as_plain(change: Callable[[Any], Any]) -> None:
    plain = _document()
    tree, mirror = _tree(_document())
    assert _outcome(change, tree) == _outcome(change, plain)
    assert strict_difference(tree, plain) is None
    assert mirror.tainted is None
    assert mirror.row_json() == native.canonical_json(plain)
    assert mirror.verify()


def _sets(d: Any) -> None:
    d["z"] = {"y": [1]}


def _replaces(d: Any) -> None:
    d["b"] = 2


def _sets_nested(d: Any) -> None:
    d["b"]["d"]["e"] = 2.5


def _extends_nested(d: Any) -> None:
    d["b"]["c"].extend([{"k": 1}, 2])


def _inserts_and_writes(d: Any) -> None:
    d["l"].insert(1, {"k": 1})
    d["l"][1]["k"] = 2


def _writes_a_list_item(d: Any) -> None:
    d["m"][1]["x"] = 9
    d["m"].insert(0, {"x": 0})
    d["m"][2]["x"] = 10
    del d["m"][0]
    d["m"][1]["x"] = 11


MUTATORS: dict[str, Callable[[Any], Any]] = {
    "set": _sets,
    "replace": _replaces,
    "set nested": _sets_nested,
    "extend nested": _extends_nested,
    "insert then write": _inserts_and_writes,
    "write list items": _writes_a_list_item,
    "same value again": lambda d: d.__setitem__("b", d["b"]),
    "del": lambda d: d.__delitem__("a"),
    "del missing": lambda d: d.__delitem__("nope"),
    "unhashable key": lambda d: d.__setitem__([], 1),
    "unhashable pop": lambda d: d.pop([]),
    "pop": lambda d: d.pop("b"),
    "pop missing": lambda d: d.pop("nope"),
    "pop default": lambda d: d.pop("nope", 7),
    "popitem": lambda d: d.popitem(),
    "popitem empty": lambda d: (d.clear(), d.popitem()),
    "setdefault new": lambda d: d.setdefault("z", {"y": 1}),
    "setdefault old": lambda d: d.setdefault("a", 9),
    "setdefault write": lambda d: d.setdefault("z", {"y": 1}).__setitem__("y", 2),
    "update": lambda d: d.update({"a": 2, "n": [1]}, o=2),
    "or": lambda d: d.__ior__({"a": 2}),
    "clear": lambda d: d.clear(),
    "append": lambda d: d["l"].append({"k": 1}),
    "extend": lambda d: d["l"].extend(iter([4, 5])),
    "iadd": lambda d: operator.iadd(d["l"], [4]),
    "insert negative": lambda d: d["l"].insert(-1, 9),
    "insert past end": lambda d: d["l"].insert(99, 9),
    "insert before start": lambda d: d["l"].insert(-99, 9),
    "list set": lambda d: d["l"].__setitem__(-1, 9),
    "list set past end": lambda d: d["l"].__setitem__(9, 9),
    "list set by str": lambda d: d["l"].__setitem__("a", 9),
    "slice set": lambda d: d["l"].__setitem__(slice(0, 2), [{"k": 1}]),
    "slice set items again": lambda d: d["m"].__setitem__(slice(0, 2), list(reversed(d["m"][:2]))),
    "slice set self": lambda d: d["l"].__setitem__(slice(1, 1), d["l"]),
    "slice set none": lambda d: d["l"].__setitem__(slice(0, 1), None),
    "step slice set": lambda d: d["l"].__setitem__(slice(0, 3, 2), [8, 9]),
    "step slice wrong size": lambda d: d["l"].__setitem__(slice(0, 3, 2), [8]),
    "list del": lambda d: d["l"].__delitem__(-1),
    "list del past end": lambda d: d["l"].__delitem__(9),
    "slice del": lambda d: d["l"].__delitem__(slice(None, -1)),
    "slice del nothing": lambda d: d["l"].__delitem__(slice(None, -9)),
    "step slice del": lambda d: d["m"].__delitem__(slice(None, None, 2)),
    "remove": lambda d: d["l"].remove(1),
    "remove missing": lambda d: d["l"].remove(99),
    "list pop": lambda d: d["l"].pop(),
    "list pop index": lambda d: d["m"].pop(0),
    "list pop past end": lambda d: d["l"].pop(9),
    "list clear": lambda d: d["m"].clear(),
    "sort": lambda d: d["l"].sort(),
    "sort key reverse": lambda d: d["m"].sort(key=lambda item: item["x"], reverse=True),
    "reverse": lambda d: d["m"].reverse(),
    "imul": lambda d: operator.imul(d["m"], 2),
    "imul then write": lambda d: (operator.imul(d["l"], 2), d["l"].__setitem__(4, 7)),
    "imul zero": lambda d: operator.imul(d["m"], 0),
    "imul by str": lambda d: operator.imul(d["l"], "a"),
    "move": lambda d: d.__setitem__("moved", d.pop("b")),
    "move then write": lambda d: (d.__setitem__("n", d.pop("b")), d["n"]["c"].append(1)),
}


@pytest.mark.parametrize("name", MUTATORS)
def test_each_mutator_does_what_a_plain_container_does_and_reaches_the_row(name: str) -> None:
    _same_as_plain(MUTATORS[name])


def _random_change(document: Any, rng: random.Random) -> None:
    containers = [document]
    stack = [document]
    while stack:
        for item in (lambda n: list(dict.values(n)) if isinstance(n, dict) else list(n))(
            stack.pop()
        ):
            if isinstance(item, (dict, list)):
                containers.append(item)
                stack.append(item)
    node = rng.choice(containers)
    value: Any = rng.choice(
        [1, 2.5, None, True, "t", {"a": 1}, [1, {"b": 2}], {"c": {"d": []}}, 10**30]
    )
    key = rng.choice("abcde")
    if isinstance(node, dict):
        operations: list[Callable[[], Any]] = [
            lambda: node.__setitem__(key, value),
            lambda: node.pop(key, None),
            lambda: node.setdefault(key, value),
            lambda: node.update({key: value}),
        ]
        rng.choice(operations)()
        return
    size = len(node)
    index = rng.randrange(size) if size else 0
    operations = [
        lambda: node.append(value),
        lambda: node.extend([value, 1]),
        lambda: node.insert(index, value),
        lambda: size and node.__setitem__(index, value),
        lambda: size and node.__delitem__(index),
        lambda: size and node.pop(index),
        lambda: node.__setitem__(slice(index, index + 1), [value, 2]),
        lambda: node.__delitem__(slice(index, index + 2)),
        lambda: node.reverse(),
    ]
    rng.choice(operations)()


@pytest.mark.parametrize("seed", range(8))
def test_random_writes_keep_the_row_equal_to_the_tree_and_a_plain_document(seed: int) -> None:
    plain = _document()
    tree, mirror = _tree(_document())
    plain_rng, tree_rng = random.Random(seed), random.Random(seed)
    for step in range(600):
        _random_change(plain, plain_rng)
        _random_change(tree, tree_rng)
        if step % 50 == 0:
            assert strict_difference(tree, plain) is None
            assert mirror.row_json() == native.canonical_json(plain)
    assert mirror.tainted is None
    assert mirror.verify()


def test_a_plain_container_is_converted_on_insert_and_its_old_name_reaches_nothing() -> None:
    tree, mirror = _tree(_document())
    plain = {"y": [{"k": 1}]}
    tree["z"] = plain
    assert type(tree["z"]) is TrackedDict and type(tree["z"]["y"]) is TrackedList
    assert type(tree["z"]["y"][0]) is TrackedDict
    plain["y"][0]["k"] = 2
    assert tree["z"]["y"][0]["k"] == 1
    assert mirror.verify()


def test_a_node_out_of_the_tree_is_an_ordinary_container() -> None:
    tree, mirror = _tree(_document())
    kept = tree.pop("b")
    forwards = mirror.stats()["forwards"]
    kept["d"]["e"] = 9.0
    kept["c"].append(1)
    assert mirror.stats()["forwards"] == forwards
    assert kept["d"]["e"] == 9.0
    assert mirror.verify()


def test_a_replaced_node_is_out_of_the_tree_too() -> None:
    tree, mirror = _tree(_document())
    kept = tree["b"]
    tree["b"] = {"new": 1}
    kept["d"] = 1
    assert tree["b"] == {"new": 1}
    assert mirror.verify()
    assert mirror.stats()["live_nodes"] == 7


def test_a_node_in_the_tree_inserted_elsewhere_is_a_copy() -> None:
    tree, mirror = _tree(_document())
    tree["copy"] = tree["b"]
    assert tree["copy"] == tree["b"] and tree["copy"] is not tree["b"]
    tree["copy"]["d"]["e"] = 3.0
    assert tree["b"]["d"]["e"] == 1.5
    assert mirror.verify()


def test_reads_look_like_plain_containers() -> None:
    tree, _ = _tree(_document())
    assert isinstance(tree, dict) and isinstance(tree["l"], list)
    assert tree == _document() and _document() == tree
    assert json.dumps(tree, sort_keys=True) == json.dumps(_document(), sort_keys=True)
    assert repr(tree) == repr(_document())
    assert sorted(tree["l"]) == [1, 2, 3]
    assert list(tree) == list(_document())


@pytest.mark.parametrize(
    "copied",
    [
        lambda tree: tree.copy(),
        lambda tree: copy.copy(tree),
        lambda tree: copy.deepcopy(tree),
        lambda tree: pickle.loads(pickle.dumps(tree)),
        lambda tree: dict(tree),
    ],
)
def test_copies_of_a_node_are_plain(copied: Callable[[Any], Any]) -> None:
    tree, mirror = _tree(_document())
    result = copied(tree)
    assert type(result) is dict and result == _document()
    forwards = mirror.stats()["forwards"]
    result["new"] = 1
    assert "new" not in tree
    assert mirror.stats()["forwards"] == forwards


def test_a_deep_copy_has_no_tracked_node_left() -> None:
    tree, _ = _tree(_document())
    result = copy.deepcopy(tree)
    assert strict_difference(result, _document()) is None
    assert type(result["m"]) is list and type(result["m"][0]) is dict


def test_a_container_built_by_its_constructor_is_free() -> None:
    built = TrackedDict([("a", {"b": 1})], c=[1])
    built["d"] = {"e": 1}
    assert built == {"a": {"b": 1}, "c": [1], "d": {"e": 1}}
    assert type(built["d"]) is dict
    assert TrackedList([1, 2]) == [1, 2]
    assert NamespaceTable(a=1) == {"a": 1}


def test_a_free_node_moves_in_as_itself_with_its_plain_children_converted() -> None:
    tree, mirror = _tree(_document())
    free = TrackedDict(y=[{"k": 1}])
    tree["free"] = free
    assert tree["free"] is free
    assert type(free["y"]) is TrackedList and type(free["y"][0]) is TrackedDict
    free["y"][0]["k"] = 2
    assert mirror.verify() and mirror.tainted is None


def test_a_tree_nobody_names_is_collected_with_its_mirror() -> None:
    tree, mirror = _tree(_document())
    reference = weakref.ref(tree["m"])
    del tree, mirror
    gc.collect()
    assert reference() is None


class _Overriding(dict):
    def items(self) -> Any:
        return [("hidden", 1)]


class _OverridingList(list):
    pass


def test_a_user_subclass_goes_to_the_python_code_while_tracked_ones_do_not() -> None:
    tree, _ = _tree(_document())
    assert native.canonical_json(tree) == native.canonical_json(_document())
    assert native.canonical_json(_Overriding(a=1)) is None
    assert native.canonical_json({"a": _Overriding(a=1)}) is None
    assert native.canonical_json(_OverridingList([1])) is None
    assert native.body_agents_valid(TrackedDict())
    assert not native.body_agents_valid(_Overriding())
    assert native.same_json_items(TrackedList([{"a": 1}]), [{"a": 1}], 0, 1)
    assert not native.same_json_items(_OverridingList([{"a": 1}]), [{"a": 1}], 0, 1)
    assert not native.same_json_items([_Overriding(a=1)], [{"a": 1}], 0, 1)


def test_tracked_containers_still_differ_from_other_types_for_the_history_kernel() -> None:
    tree, _ = _tree({"h": [{"a": [1, 2.0]}]})
    assert native.same_json_items(tree["h"], [{"a": [1, 2.0]}], 0, 1)
    assert not native.same_json_items(tree["h"], [{"a": [1, 2]}], 0, 1)
    assert not native.same_json_items(tree["h"], [{"a": (1, 2.0)}], 0, 1)
