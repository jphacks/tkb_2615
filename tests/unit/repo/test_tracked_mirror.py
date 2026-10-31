"""The test-only tracked mirror sees each kind of write, and reports what it can't forward.

The prelude of the Rust port runs simulations on it (benchmarks/tracked_mirror_gate.py), so a
mutator it forwards wrongly would hide the very drift it looks for.
"""

from __future__ import annotations

import copy
import heapq
import json
import pickle
import random
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from support.tracked_mirror import (
    Census,
    Drift,
    ReverseReadDict,
    Session,
    TrackedDict,
    TrackedList,
    check_shadow,
    plain_copy,
    reverse_tree,
    strict_difference,
    tracked_root,
)


def _document() -> dict[str, Any]:
    return {
        "a": 1,
        "b": {"c": [1, 2, 3], "d": {"e": 1.5}},
        "l": [3, 1, 2],
        "m": [{"x": 1}, {"x": 2}, {"x": 3}],
    }


def _same_as_plain(change: Callable[[dict[str, Any]], Any]) -> None:
    """The change does the same to a plain and a tracked document, and to the shadow."""
    plain = _document()
    tracked = tracked_root(_document())
    expected = change(plain)
    actual = change(tracked)
    assert strict_difference(tracked, plain) is None
    assert strict_difference(tracked._m_mirror.shadow, plain) is None
    check_shadow(tracked)
    assert actual == expected
    assert tracked._m_mirror.writes > 0


DICT_CHANGES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "setitem_new": lambda d: d.__setitem__("z", 1),
    "setitem_existing_keeps_position": lambda d: d.__setitem__("a", 2),
    "setitem_container": lambda d: d.__setitem__("z", {"y": [1, {"w": 2}]}),
    "setitem_nested": lambda d: d["b"]["d"].__setitem__("e", 2.5),
    "delitem": lambda d: d["b"].__delitem__("c"),
    "pop": lambda d: d["b"].pop("c"),
    "pop_default_writes_nothing_more": lambda d: (d["b"].pop("no", 7), d.__setitem__("a", 9)),
    "popitem": lambda d: d.popitem(),
    "setdefault_new": lambda d: d.setdefault("z", [1]),
    "setdefault_present": lambda d: (d.setdefault("a", 7), d.__setitem__("a", 8)),
    "setdefault_result_is_live": lambda d: d.setdefault("z", []).append(1),
    "update_mapping": lambda d: d.update({"z": 1, "a": 5}),
    "update_pairs_and_keywords": lambda d: d.update([("y", 1)], x={"w": 1}),
    "clear": lambda d: d["b"].clear(),
    "clear_root": lambda d: d.clear(),
    "ior": lambda d: d["b"].__ior__({"z": [1]}),
    "ior_operator": lambda d: _ior(d),
}


def _ior(document: dict[str, Any]) -> None:
    document |= {"z": {"y": 1}}
    document["z"]["y"] = 2


LIST_CHANGES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "setitem": lambda d: d["l"].__setitem__(1, 5),
    "setitem_negative": lambda d: d["l"].__setitem__(-1, {"a": 1}),
    "setitem_slice": lambda d: d["l"].__setitem__(slice(0, 2), [7, {"a": 1}, 9]),
    "setitem_extended_slice": lambda d: d["l"].__setitem__(slice(0, 3, 2), [7, 8]),
    "delitem": lambda d: d["l"].__delitem__(0),
    "delitem_slice": lambda d: d["l"].__delitem__(slice(0, 2)),
    "append": lambda d: d["l"].append({"a": 1}),
    "extend": lambda d: d["l"].extend([1, [2]]),
    "extend_iterator": lambda d: d["l"].extend(iter([1, 2])),
    "extend_self": lambda d: d["l"].extend(d["l"]),
    "insert": lambda d: d["l"].insert(1, 9),
    "insert_negative": lambda d: d["l"].insert(-1, {"a": 1}),
    "remove": lambda d: d["l"].remove(1),
    "pop_last": lambda d: d["l"].pop(),
    "pop_index": lambda d: d["l"].pop(0),
    "clear": lambda d: d["l"].clear(),
    "sort": lambda d: d["l"].sort(),
    "sort_key_reverse": lambda d: d["m"].sort(key=lambda item: item["x"], reverse=True),
    "reverse": lambda d: d["l"].reverse(),
    "iadd": lambda d: _iadd(d),
    "imul": lambda d: _imul(d),
    "imul_zero": lambda d: d["l"].__imul__(0),
    "write_after_items_shifted": lambda d: _shifted(d),
    "write_after_sort": lambda d: _after_sort(d),
    "write_after_reverse": lambda d: _after_reverse(d),
}


def _iadd(document: dict[str, Any]) -> None:
    document["l"] += [1, {"a": 1}]
    document["l"][-1]["a"] = 2


def _imul(document: dict[str, Any]) -> None:
    document["l"] *= 2


def _shifted(document: dict[str, Any]) -> None:
    item = document["m"][2]
    document["m"].insert(0, {"x": 0})
    document["m"].pop(1)
    item["x"] = 30


def _after_sort(document: dict[str, Any]) -> None:
    item = document["m"][0]
    document["m"].sort(key=lambda value: -value["x"])
    item["x"] = 10


def _after_reverse(document: dict[str, Any]) -> None:
    item = document["m"][0]
    document["m"].reverse()
    item["y"] = 10


@pytest.mark.parametrize("name", DICT_CHANGES)
def test_a_dict_mutator_forwards_what_it_does(name: str) -> None:
    _same_as_plain(DICT_CHANGES[name])


@pytest.mark.parametrize("name", LIST_CHANGES)
def test_a_list_mutator_forwards_what_it_does(name: str) -> None:
    _same_as_plain(LIST_CHANGES[name])


def test_a_random_stream_of_writes_keeps_tracked_plain_and_shadow_equal() -> None:
    rng = random.Random(2615)
    plain = _document()
    tracked = tracked_root(_document())

    def targets(document: Any) -> list[Any]:
        found = [document]
        for item in document.values() if isinstance(document, dict) else document:
            if isinstance(item, (dict, list)):
                found.extend(targets(item))
        return found

    for step in range(3000):
        choice = rng.random()
        index = rng.randrange(len(targets(plain)))
        value = rng.choice([step, step + 0.5, None, {"n": step}, [step], "s"])
        for document in (plain, tracked):
            target = targets(document)[index]
            local = rng.__class__(step)
            fresh = copy.deepcopy(value)
            if isinstance(target, dict):
                keys = sorted(target, key=str)
                if choice < 0.5 or not keys:
                    target[f"k{local.randrange(6)}"] = fresh
                elif choice < 0.8:
                    target.pop(keys[local.randrange(len(keys))])
                else:
                    target.setdefault(f"k{local.randrange(6)}", fresh)
            elif choice < 0.4 or not target:
                target.append(fresh)
            elif choice < 0.6:
                target.insert(local.randrange(len(target) + 1), fresh)
            elif choice < 0.8:
                target.pop(local.randrange(len(target)))
            else:
                target[local.randrange(len(target))] = fresh
        assert strict_difference(tracked, plain) is None
    check_shadow(tracked)
    assert strict_difference(tracked._m_mirror.shadow, plain) is None


def test_copies_are_plain_and_detached() -> None:
    tracked = tracked_root(_document())

    deep = copy.deepcopy(tracked)
    assert type(deep) is dict and type(deep["b"]) is dict and type(deep["b"]["c"]) is list
    deep["b"]["c"].append(9)
    # A shallow copy is a plain container holding live nodes, as with plain dicts.
    assert type(copy.copy(tracked)) is dict and type(tracked.copy()) is dict
    assert type(copy.copy(tracked["l"])) is list and type(tracked["l"].copy()) is list
    assert tracked.copy()["b"] is tracked["b"]
    assert type(tracked["l"][:2]) is list and type(tracked["l"] + [1]) is list
    assert type(tracked | {"z": 1}) is dict and type({"z": 1} | tracked) is dict
    check_shadow(tracked)
    assert tracked == _document()


def test_deepcopy_keeps_a_memo_for_shared_objects() -> None:
    tracked = tracked_root({"a": [1]})
    shared = [1]

    copied = copy.deepcopy(tracked, {id(tracked["a"]): shared})

    assert copied["a"] is shared


def test_pickling_gives_plain_containers() -> None:
    tracked = tracked_root(_document())

    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        loaded = pickle.loads(pickle.dumps(tracked, protocol=protocol))
        assert strict_difference(loaded, _document()) is None
        assert type(loaded) is dict and type(loaded["m"][0]) is dict


def test_json_writes_a_tracked_document_like_a_plain_one() -> None:
    tracked = tracked_root(_document())

    assert json.dumps(tracked) == json.dumps(_document())
    assert json.dumps(tracked, sort_keys=True, indent=1) == json.dumps(
        _document(), sort_keys=True, indent=1
    )
    assert repr(tracked) == repr(_document())


def test_a_plain_container_is_converted_on_insert_with_its_children() -> None:
    tracked = tracked_root(_document())

    tracked["z"] = {"y": [{"w": 1}]}

    assert isinstance(tracked["z"], TrackedDict)
    assert isinstance(tracked["z"]["y"], TrackedList)
    assert isinstance(tracked["z"]["y"][0], TrackedDict)
    tracked["z"]["y"][0]["w"] = 2
    check_shadow(tracked)


@pytest.mark.parametrize(
    "plain_value",
    [
        pytest.param(lambda d: json.loads(json.dumps(d["b"])), id="json_loads"),
        pytest.param(lambda d: copy.deepcopy(d["b"]), id="deepcopy"),
        pytest.param(lambda d: sorted(d["m"], key=lambda item: -item["x"]), id="sorted"),
        pytest.param(lambda d: d["l"][:2], id="slice"),
        pytest.param(lambda d: d["l"] + [1], id="concatenation"),
        pytest.param(lambda d: [item for item in d["m"] if item["x"] > 1], id="comprehension"),
        pytest.param(lambda d: dict(d["b"]), id="dict_call"),
        pytest.param(lambda d: list(d["m"]), id="list_call"),
    ],
)
def test_what_c_level_code_returns_is_converted_when_inserted(
    plain_value: Callable[[Any], Any],
) -> None:
    plain = _document()
    tracked = tracked_root(_document())

    plain["new"] = plain_value(plain)
    tracked["new"] = plain_value(tracked)

    assert isinstance(tracked["new"], (TrackedDict, TrackedList))
    assert strict_difference(tracked, plain) is None
    check_shadow(tracked)


def test_a_comprehension_moving_nodes_keeps_them_forwarding() -> None:
    tracked = tracked_root(_document())
    item = tracked["m"][2]

    tracked["m"] = [value for value in tracked["m"] if value["x"] > 1]
    item["x"] = 30

    assert tracked["m"][1]["x"] == 30
    check_shadow(tracked)


def test_a_node_popped_from_the_tree_stops_forwarding() -> None:
    session = Session()
    with session:
        tracked = session.track(_document(), "model_state")
        item = tracked["m"].pop(0)
        item["x"] = 100
        check_shadow(tracked)

    assert "write_to_detached_node" in session.findings


def test_a_constructed_container_reads_like_a_plain_one_until_inserted() -> None:
    # dataclasses.asdict builds type(obj)(...) for a dict subclass.
    tracked = tracked_root(_document())
    built = TrackedDict([("a", {"b": 1})], c=[1])

    tracked["z"] = built

    assert tracked["z"] is built
    assert isinstance(built["a"], TrackedDict) and isinstance(built["c"], TrackedList)
    built["a"]["b"] = 2
    check_shadow(tracked)
    assert tracked._m_mirror.shadow["z"]["a"]["b"] == 2


@pytest.mark.parametrize(
    "bypass",
    [
        pytest.param(lambda d: dict.__setitem__(d, "z", 1), id="dict_setitem"),
        pytest.param(lambda d: dict.update(d["b"], {"z": 1}), id="dict_update"),
        pytest.param(lambda d: dict.pop(d, "a"), id="dict_pop"),
        pytest.param(lambda d: dict.clear(d["b"]), id="dict_clear"),
        pytest.param(lambda d: dict.setdefault(d, "z", 1), id="dict_setdefault"),
        pytest.param(lambda d: list.append(d["l"], 1), id="list_append"),
        pytest.param(lambda d: list.__setitem__(d["l"], 0, 9), id="list_setitem"),
        pytest.param(lambda d: list.sort(d["l"]), id="list_sort"),
        pytest.param(lambda d: heapq.heappush(d["l"], 0), id="heappush"),
        pytest.param(lambda d: heapq.heappop(d["l"]), id="heappop"),
    ],
)
def test_a_write_past_the_mutators_is_reported_as_drift(bypass: Callable[[Any], Any]) -> None:
    tracked = tracked_root(_document())

    bypass(tracked)

    with pytest.raises(Drift):
        check_shadow(tracked)


def test_the_comparison_tells_int_float_bool_tuple_and_key_order_apart() -> None:
    assert strict_difference({"a": 1}, {"a": 1.0}) is not None
    assert strict_difference({"a": 1}, {"a": True}) is not None
    assert strict_difference({"a": [1]}, {"a": (1,)}) is not None
    assert strict_difference({"a": 0.0}, {"a": -0.0}) is not None
    assert strict_difference({"a": 1, "b": 2}, {"b": 2, "a": 1}) is not None
    assert strict_difference(tracked_root({"a": [1]}), {"a": [1]}) is None


def test_a_shared_node_written_through_one_path_is_drift() -> None:
    # Rust's tree has no aliases: the second place would not see the write.
    session = Session()
    with session:
        tracked = session.track({"first": {"x": 1}, "second": None}, "model_state")
        tracked["second"] = tracked["first"]
        tracked["second"]["x"] = 2
        with pytest.raises(Drift):
            check_shadow(tracked)

    assert "write_to_shared_node" in session.findings


def test_a_plain_alias_changed_after_insert_is_reported() -> None:
    session = Session()
    with session:
        tracked = session.track({"records": {}}, "model_state")
        record = tracked["records"]["r"] = {"count": 0}
        record["count"] = 1
        session.check_plain_aliases()
        check_shadow(tracked)

    assert tracked["records"]["r"]["count"] == 0
    assert any("test_tracked_mirror" in site for site in session.findings[_changed()])


def _changed() -> str:
    return "plain_alias_changed_after_insert"


def test_a_plain_alias_read_again_from_the_tree_is_not_reported() -> None:
    session = Session()
    with session:
        tracked = session.track({"records": {}}, "model_state")
        tracked["records"]["r"] = {"count": 0}
        record = tracked["records"]["r"]
        record["count"] = 1
        session.check_plain_aliases()
        check_shadow(tracked)

    assert _changed() not in session.findings


def test_a_node_changing_inside_an_inserted_plain_list_is_not_an_alias_change() -> None:
    session = Session()
    with session:
        tracked = session.track({"items": [{"x": 1}]}, "model_state")
        moved = [item for item in tracked["items"]]
        tracked["items"] = moved
        tracked["items"][0]["x"] = 2
        session.check_plain_aliases()
        check_shadow(tracked)

    assert _changed() not in session.findings


def test_the_same_plain_object_inserted_twice_is_reported() -> None:
    session = Session()
    with session:
        tracked = session.track({}, "model_state")
        shared: list[int] = []
        tracked["a"] = shared
        tracked["b"] = shared
        session.check_plain_aliases()

    assert "same_plain_object_inserted_twice" in session.findings


def test_a_reversing_dict_reads_backwards_and_stays_equal() -> None:
    session = Session(reverse_reads=True)
    with session:
        tracked = session.track({"a": 1, "b": 2, "c": {"d": 1, "e": 2}}, "model_state")
        assert isinstance(tracked, ReverseReadDict)
        assert list(tracked) == ["c", "b", "a"]
        assert list(tracked.items())[0][0] == "c"
        assert list(tracked.values())[-1] == 1
        assert list(dict(tracked)) == ["c", "b", "a"]
        assert "a" in tracked.keys() and tracked.keys() & {"a"} == {"a"}
        assert tracked == {"a": 1, "b": 2, "c": {"d": 1, "e": 2}}
        assert tracked.popitem()[0] == "c"
        assert tracked._m_mirror.difference(sort=True) is None


def test_reversing_a_tree_changes_only_the_order_of_dicts() -> None:
    document = {"a": 1, "b": {"c": 1, "d": [{"e": 1, "f": 2}]}, "g": [1, 2]}
    original = plain_copy(document)

    reverse_tree(document)

    assert document == original
    assert list(document) == ["g", "b", "a"]
    assert list(document["b"]["d"][0]) == ["f", "e"]
    assert document["g"] == [1, 2]


@dataclass
class _Entity:
    stress: float = 0.5


def _census_world(**model_state: Any) -> Any:
    return SimpleNamespace(
        agents={"A1": _Entity()},
        zones={},
        zone_edges={},
        facilities={},
        staff={},
        support_requests={},
        active_tasks={},
        event_queue=SimpleNamespace(pending=list),
        model_state=model_state,
    )


def test_the_census_counts_types_per_path_with_ids_collapsed() -> None:
    census = Census()

    census.observe_world(_census_world(ns_v1={"agents": {"A1": {"x": 1}}}))
    census.observe_world(_census_world(ns_v1={"agents": {"A1": {"x": 1.5}}}))

    assert census.types["model_state/ns_v1/agents/*/x"] == {"int": 1, "float": 1}


def test_the_census_counts_the_fields_of_entities() -> None:
    census = Census()

    census.observe_world(_census_world())

    assert census.types["entities/agents/stress"] == {"float": 1}


def test_the_census_finds_tuples_and_keys_that_are_not_strings() -> None:
    census = Census()

    census.observe_world(_census_world(ns_v1={"pair": (1, 2), "by_int": {3: "a"}}))

    assert census.types["model_state/ns_v1/pair"] == {"tuple": 2}
    assert census.types["model_state/ns_v1/by_int/<key>"] == {"int": 1}


def test_the_census_finds_an_object_at_two_paths() -> None:
    census = Census()
    shared = {"x": 1}

    census.observe_world(_census_world(first_v1={"a": shared}, second_v1={"b": shared}))

    assert list(census.shared) == ["model_state/first_v1/a and model_state/second_v1/b"]


def test_the_census_finds_an_alias_held_outside_the_tree() -> None:
    census = Census()
    held = {"x": 1}
    world = _census_world(ns_v1={"held": held, "free": {"x": 1}})

    census.observe_world(world)

    assert list(census.aliases) == ["model_state/ns_v1/held"]
