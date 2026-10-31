"""staff_dynamics_v1 owned by Rust, with the rules of domain/staff_dynamics.py.

native/src/rules/staff_dynamics.rs moves the rules; the Python rules stay as the comparison base and
give every error text (docs/rust-port/lanes/s-staff-dynamics.md). The whole rule is compared on the
gate by benchmarks/rule_shadow; here, on small worlds and on values no scenario reaches.
"""

from __future__ import annotations

import copy
import json
import math
import random
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.state_mirror import detach, rust_validated
from sheltersim.core.world import WorldState
from sheltersim.domain.models import StaffState
from sheltersim.domain.staff_dynamics import (
    SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
    SHIFT_STAFF_DYNAMICS_SOURCE_ID,
    STAFF_DYNAMICS_POLICY_VERSION,
    STAFF_DYNAMICS_STATE_KEY,
    add_staff,
    canonical_staff_dynamics_config,
    create_staff_dynamics_state,
    staff_dynamics_is_available,
    staff_dynamics_on_shift,
    update_staff_dynamics,
    validate_staff_dynamics_state,
    validate_staff_shift_windows_override,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.registry import StaffFatigueFormula

needs_kernels = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

KEY = STAFF_DYNAMICS_STATE_KEY
IDS = ("S01", "S02", "S03")


def _kernels() -> Any:
    return native.rules("staff_dynamics")


def _text(value: Any) -> str:
    """Float bits, 1 against 1.0, and key order all show."""
    return json.dumps(value)


def _world(**raw_config: Any) -> WorldState:
    world = WorldState(
        "s",
        1,
        500,
        staff={staff_id: StaffState(staff_id, "z", ("support",)) for staff_id in IDS},
        model_configs={"staff_dynamics": raw_config},
    )
    world.model_state[KEY] = create_staff_dynamics_state(world)
    return world


def _state(world: WorldState) -> Any:
    return world.model_state[KEY]


def _mirror(world: WorldState) -> Any:
    return world._core.mirror(KEY)


def _plain_twin(world: WorldState) -> tuple[Any, SimpleNamespace]:
    """A plain copy of the namespace, and a world for the Python rule that holds it."""
    plain = json.loads(json.dumps(_state(world)))
    table = dict(world.model_state)
    table[KEY] = plain
    return plain, SimpleNamespace(
        model_state=table,
        staff=world.staff,
        model_configs=world.model_configs,
        duration_minutes=world.duration_minutes,
    )


# ---- The row ----


@needs_kernels
@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"max_continuous_work_minutes_by_staff": {"S01": 60}},
        {"shift_windows_by_staff": {"S01": [{"start_minute": 0, "end_minute": 10}]}},
        {
            "policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
            "off_shift_fatigue_recovery_per_minute": 0.01,
            "source_id": "é-source",
        },
    ],
)
def test_a_row_writes_the_bytes_of_the_tree(raw: dict[str, Any]) -> None:
    world = _world(**raw)
    assert _mirror(world).row_json() == native.canonical_json(_state(world))
    before = _text(_state(world))
    assert detach(world, KEY)
    assert _text(_state(world)) == before


@needs_kernels
def test_a_config_key_an_older_schema_lacks_is_absent_from_the_row_and_its_bytes() -> None:
    world = _world()
    assert "max_continuous_work_minutes_by_staff" not in _state(world)["config"]
    assert _mirror(world).has_row
    del _state(world)["config"]["off_shift_fatigue_recovery_per_minute"]
    del _state(world)["config"]["shift_windows_by_staff"]
    assert _mirror(world).has_row
    assert _mirror(world).row_json() == native.canonical_json(_state(world))
    _state(world)["config"]["shift_windows_by_staff"] = {}
    assert _mirror(world).row_json() == native.canonical_json(_state(world))
    assert _mirror(world).verify()


@needs_kernels
@pytest.mark.parametrize(
    ("write", "reason"),
    [
        (lambda state: state["staff"]["S01"].__setitem__("fatigue", 1), "not a float"),
        (lambda state: state["staff"]["S01"].__setitem__("fatigue", True), "not a float"),
        (lambda state: state["staff"]["S01"].__setitem__("total_work_minutes", 1.0), "not an int"),
        (
            lambda state: state["staff"]["S01"].__setitem__("last_activity", "asleep"),
            "LastActivity",
        ),
        (lambda state: state["staff"]["S01"].__setitem__("last_activity", 1), "not a str"),
        (lambda state: state["staff"]["S01"].__setitem__("other", 1), "no field"),
        (lambda state: state["staff"]["S01"].__delitem__("fatigue"), "which fields"),
        (lambda state: state["config"].__setitem__("rest_minutes", 30.0), "not an int"),
        (
            lambda state: state["config"].__setitem__("off_shift_fatigue_recovery_per_minute", 0),
            "not a float",
        ),
        (lambda state: state["config"].__delitem__("rest_minutes"), "which fields"),
        (lambda state: state["config"].__setitem__("other", 1), "no field"),
        (lambda state: state["config"].__setitem__("shift_windows_by_staff", None), "not a dict"),
    ],
)
def test_a_value_the_row_cant_hold_taints_it_and_the_python_rules_still_run(
    write: Any, reason: str
) -> None:
    world = _world()
    write(_state(world))
    assert reason in _mirror(world).tainted
    with pytest.raises(native.Unsupported):
        _kernels().update(world, 0)
    assert not rust_validated(world, KEY)


@needs_kernels
def test_forwarded_writes_reach_the_row() -> None:
    world = _world()
    record = _state(world)["staff"]["S01"]
    record["fatigue"] = 0.5
    record["last_activity"] = "working"
    record["rest_until_minute"] = 9
    _state(world)["config"]["rest_minutes"] = 45
    assert _mirror(world).verify()
    assert _mirror(world).row_json() == native.canonical_json(_state(world))


# ---- update ----


@needs_kernels
def test_a_tick_writes_no_whole_record_and_nothing_is_forwarded() -> None:
    world = _world()
    world.staff["S01"].active_task_id = "t"
    before = dict(_mirror(world).stats())
    for minute in range(20):
        _kernels().update(world, minute)
    after = _mirror(world).stats()
    assert after["whole_writes"] == before["whole_writes"]
    assert after["forwards"] == before["forwards"]
    assert _state(world)["staff"]["S01"]["consecutive_work_minutes"] == 20
    assert _mirror(world).verify()


@needs_kernels
def test_the_rule_the_callers_use_runs_the_kernel() -> None:
    world = _world()
    update_staff_dynamics(world, 0)
    assert _state(world)["staff"]["S01"]["total_rest_minutes"] == 0
    assert update_staff_dynamics.__wrapped__ is not update_staff_dynamics


def _random_world(rng: random.Random) -> WorldState:
    roster = {
        staff_id: [{"start_minute": rng.randrange(0, 60), "end_minute": rng.randrange(60, 200)}]
        for staff_id in IDS
        if rng.random() < 0.5
    }
    raw: dict[str, Any] = {
        "max_continuous_work_minutes": rng.choice((1, 3, 7, 240)),
        "rest_minutes": rng.choice((1, 2, 30, 480)),
        "off_shift_fatigue_recovery_per_minute": rng.choice((0.0, 0.005, 0.1, 1.0, 1 / 3)),
        "shift_windows_by_staff": roster,
    }
    limits = {staff_id: rng.randrange(1, 9) for staff_id in IDS if rng.random() < 0.3}
    if limits:
        raw["max_continuous_work_minutes_by_staff"] = limits
    world = _world(**raw)
    world.model_configs["policy_registry"] = {
        "staff_fatigue": rng.choice(("staff-fatigue-linear-v1", "staff-fatigue-saturating-v1"))
    }
    if rng.random() < 0.5:
        world.model_state["operational_events_v1"] = {
            "staff_status": {
                staff_id: {"available": rng.random() < 0.5}
                for staff_id in IDS
                if rng.random() < 0.7
            }
        }
    for record in _state(world)["staff"].values():
        record["fatigue"] = rng.random()
        record["consecutive_work_minutes"] = rng.randrange(0, 12)
        record["rest_until_minute"] = rng.choice((0, 0, rng.randrange(0, 400)))
    return world


@needs_kernels
@pytest.mark.parametrize("seed", range(40))
def test_update_writes_what_the_python_rule_writes(seed: int) -> None:
    rng = random.Random(seed)
    world = _random_world(rng)
    minutes = sorted(rng.randrange(0, 12_000) for _ in range(30))
    for minute in minutes:
        for staff_id in IDS:
            world.staff[staff_id].active_task_id = "t" if rng.random() < 0.4 else None
        plain, twin = _plain_twin(world)
        update_staff_dynamics.__wrapped__(twin, minute)
        _kernels().update(world, minute)
        assert _text(_state(world)) == _text(plain)
        assert _mirror(world).verify()


@needs_kernels
def test_the_comparison_sees_a_one_percent_change() -> None:
    world = _world()
    world.staff["S01"].active_task_id = "t"
    plain, twin = _plain_twin(world)
    update_staff_dynamics.__wrapped__(twin, 0)
    _kernels().update(world, 0)
    assert _text(_state(world)) == _text(plain)
    plain["staff"]["S01"]["fatigue"] *= 1.01
    assert _text(_state(world)) != _text(plain)


@needs_kernels
@pytest.mark.parametrize("model_id", ["staff-fatigue-linear-v1", "staff-fatigue-saturating-v1"])
def test_a_fatigue_value_has_the_bits_of_the_python_formula(model_id: str) -> None:
    formula = StaffFatigueFormula(model_id, "x", {})
    rng = random.Random(1)
    for _ in range(20_000):
        consecutive = rng.randrange(-5, 3_000)
        maximum = rng.randrange(1, 1_441)
        expected = formula.fatigue(consecutive, maximum)
        actual = _kernels().fatigue_value(
            model_id == "staff-fatigue-saturating-v1", consecutive, maximum
        )
        assert math.copysign(1.0, actual) == math.copysign(1.0, expected)
        assert actual.hex() == expected.hex()


@needs_kernels
@pytest.mark.parametrize(
    ("corrupt", "error"),
    [
        (lambda world: _state(world)["staff"].__delitem__("S02"), KeyError),
        (
            lambda world: (
                _state(world)["staff"]["S01"].__setitem__("consecutive_work_minutes", 240),
                _state(world)["config"].pop("rest_minutes"),
            ),
            KeyError,
        ),
        (
            lambda world: world.model_state.__setitem__(
                "operational_events_v1", {"staff_status": {"S01": {}}}
            ),
            KeyError,
        ),
        (lambda world: world.model_state.__setitem__("operational_events_v1", {}), KeyError),
        (lambda world: world.model_configs.__setitem__("policy_registry", 1), AttributeError),
    ],
)
def test_a_bad_value_changes_nothing_and_the_python_rule_gives_its_error(
    corrupt: Any, error: type[Exception]
) -> None:
    world = _world()
    corrupt(world)
    expected = None
    try:
        _, twin = _plain_twin(world)
        update_staff_dynamics.__wrapped__(twin, 0)
    except Exception as raised:  # noqa: BLE001 - the text is what is compared
        expected = raised
    assert isinstance(expected, error)
    before = _text(_state(world))
    with pytest.raises(native.Unsupported):
        _kernels().update(world, 0)
    assert _text(_state(world)) == before
    with pytest.raises(error) as raised_here:
        update_staff_dynamics(world, 0)
    assert str(raised_here.value) == str(expected)


@needs_kernels
@pytest.mark.parametrize(
    "prepare",
    [
        lambda world: world.model_configs.__setitem__(
            "policy_registry", {"staff_fatigue": "another-formula"}
        ),
        lambda world: world.model_configs["staff_dynamics"].__setitem__(
            "max_continuous_work_minutes_by_staff", {"S01": 1.5}
        ),
        lambda world: _state(world)["staff"]["S01"].__setitem__(
            "consecutive_work_minutes", 2**63 - 1
        ),
    ],
)
def test_what_the_kernel_does_not_handle_is_unsupported_before_any_change(prepare: Any) -> None:
    world = _world()
    world.staff["S01"].active_task_id = "t"
    prepare(world)
    before = _text(_state(world))
    with pytest.raises(native.Unsupported):
        _kernels().update(world, 0)
    assert _text(_state(world)) == before


@needs_kernels
@pytest.mark.parametrize("minute", [True, 1.0, 2**70, "1"])
def test_a_minute_of_another_type_goes_to_python(minute: Any) -> None:
    world = _world()
    for kernel in (_kernels().update, lambda w, m: _kernels().on_shift(w, "S01", m)):
        with pytest.raises(native.Unsupported):
            kernel(world, minute)


@needs_kernels
def test_a_world_without_the_namespace_answers_as_the_python_rule_does() -> None:
    world = WorldState("s", 1, 100, staff={"S01": StaffState("S01", "z", ("support",))})
    update_staff_dynamics(world, 0)
    add_staff(world, "S02")
    assert staff_dynamics_on_shift(world, "S01", 0) is True
    assert staff_dynamics_is_available(world, "S01", 0) is True
    assert _kernels().on_shift(world, "S01", 0) is True
    assert _kernels().is_available(world, "S01", 0) is True
    assert rust_validated(world, KEY)
    assert KEY not in world.model_state


def test_a_world_of_another_class_reaches_the_python_rule() -> None:
    other = SimpleNamespace(model_state={})
    update_staff_dynamics(other, 0)
    assert staff_dynamics_on_shift(other, "S01", 0) is True


# ---- The readers ----


@needs_kernels
@pytest.mark.parametrize("seed", range(40))
def test_the_readers_answer_what_the_python_rules_answer(seed: int) -> None:
    rng = random.Random(seed)
    world = _random_world(rng)
    if rng.random() < 0.7:
        world.model_state["staff_operations_v1"] = {
            "staff": {
                staff_id: {"phase": rng.choice(("idle", "travel", "work"))}
                for staff_id in IDS
                if rng.random() < 0.7
            }
        }
    answers = set()
    for _ in range(300):
        staff_id = rng.choice((*IDS, "unknown"))
        minute = rng.choice((rng.randrange(0, 300), rng.randrange(0, 40_000)))
        for kernel, rule in (
            (_kernels().on_shift, staff_dynamics_on_shift),
            (_kernels().is_available, staff_dynamics_is_available),
        ):
            answer = kernel(world, staff_id, minute)
            assert answer is rule.__wrapped__(world, staff_id, minute)
            answers.add(answer)
    assert answers == {True, False}


@needs_kernels
@pytest.mark.parametrize(
    "operations",
    [
        {"staff": {"S01": {}}},
        {"staff": {"S01": None}},
        {"staff": {}},
        {},
        None,
    ],
)
def test_the_availability_reader_takes_each_shape_of_the_operations_state(operations: Any) -> None:
    world = _world()
    world.model_state["staff_operations_v1"] = operations
    assert _kernels().is_available(world, "S01", 0) is (
        staff_dynamics_is_available.__wrapped__(world, "S01", 0)
    )


@needs_kernels
def test_a_config_without_windows_leaves_the_readers_to_the_python_rule() -> None:
    world = _world()
    del _state(world)["config"]["shift_windows_by_staff"]
    with pytest.raises(native.Unsupported):
        _kernels().on_shift(world, "S01", 0)
    with pytest.raises(KeyError):
        staff_dynamics_on_shift(world, "S01", 0)


# ---- add_staff, create ----


@needs_kernels
@pytest.mark.parametrize("staff_id", ["S09", "S01"])
def test_add_staff_writes_what_the_python_rule_writes_and_the_tree_stays_tracked(
    staff_id: str,
) -> None:
    world = _world()
    _state(world)["staff"]["S01"]["fatigue"] = 0.5
    plain, twin = _plain_twin(world)
    add_staff.__wrapped__(twin, staff_id)
    add_staff(world, staff_id)
    assert _text(_state(world)) == _text(plain)
    assert _mirror(world).verify()
    _state(world)["staff"][staff_id]["fatigue"] = 0.25
    assert _mirror(world).verify()


@needs_kernels
@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"max_continuous_work_minutes_by_staff": {"S02": 5}},
        {"shift_windows_by_staff": {"S02": [{"start_minute": 1, "end_minute": 2}]}},
    ],
)
def test_create_state_builds_what_the_python_rule_builds(raw: Any) -> None:
    world = WorldState(
        "s",
        1,
        100,
        staff={staff_id: StaffState(staff_id, "z", ()) for staff_id in ("é", "S02", "S01")},
    )
    if raw is not None:
        world.model_configs["staff_dynamics"] = raw
    assert _text(_kernels().create_state(world)) == _text(
        create_staff_dynamics_state.__wrapped__(world)
    )


# ---- The validators ----


def _shift(start: Any = 0, end: Any = 10) -> dict[str, Any]:
    return {"start_minute": start, "end_minute": end}


CONFIGS: list[Any] = [
    None,
    {},
    {"policy_version": STAFF_DYNAMICS_POLICY_VERSION},
    {"policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION},
    {"policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION, "source_id": "x"},
    {"policy_version": "other"},
    {"policy_version": 1},
    {"policy_version": None},
    {"schema_version": 1},
    {"schema_version": 1.0},
    {"schema_version": True},
    {"schema_version": 2},
    {"source_id": ""},
    {"source_id": 3},
    {"other": 1},
    {1: 1},
    [],
    "x",
    {"max_continuous_work_minutes": 1},
    {"max_continuous_work_minutes": 1440},
    {"max_continuous_work_minutes": 0},
    {"max_continuous_work_minutes": 1441},
    {"max_continuous_work_minutes": 240.0},
    {"max_continuous_work_minutes": True},
    {"rest_minutes": 480},
    {"rest_minutes": 481},
    {"rest_minutes": "30"},
    {"max_continuous_work_minutes_by_staff": {"S01": 1}},
    {"max_continuous_work_minutes_by_staff": {"S01": 1441}},
    {"max_continuous_work_minutes_by_staff": {"S01": True}},
    {"max_continuous_work_minutes_by_staff": {"": 1}},
    {"max_continuous_work_minutes_by_staff": []},
    {"shift_windows_by_staff": {}},
    {"shift_windows_by_staff": {"S01": []}},
    {"shift_windows_by_staff": {"S01": [_shift()]}},
    {"shift_windows_by_staff": {"S01": [_shift(10, 20), _shift(0, 10)]}},
    {"shift_windows_by_staff": {"S01": [_shift(0, 11), _shift(10, 20)]}},
    {"shift_windows_by_staff": {"S01": [_shift(5, 5)]}},
    {"shift_windows_by_staff": {"S01": [_shift(-1, 5)]}},
    {"shift_windows_by_staff": {"S01": [_shift(0, 10_081)]}},
    {"shift_windows_by_staff": {"S01": [_shift(0.0, 5)]}},
    {"shift_windows_by_staff": {"S01": [_shift(True, 5)]}},
    {"shift_windows_by_staff": {"S01": [{"start_minute": 0}]}},
    {"shift_windows_by_staff": {"S01": [{**_shift(), "x": 1}]}},
    {"shift_windows_by_staff": {"S01": [_shift(i * 2, i * 2 + 1) for i in range(21)]}},
    {"shift_windows_by_staff": {"S01": [_shift(i * 2, i * 2 + 1) for i in range(22)]}},
    {"shift_windows_by_staff": {"": []}},
    {"shift_windows_by_staff": []},
    {"off_shift_fatigue_recovery_per_minute": 0.0},
    {"off_shift_fatigue_recovery_per_minute": -0.0},
    {"off_shift_fatigue_recovery_per_minute": 0.5},
    {"off_shift_fatigue_recovery_per_minute": 1.0},
    {"off_shift_fatigue_recovery_per_minute": 1.5},
    {"off_shift_fatigue_recovery_per_minute": 0},
    {"off_shift_fatigue_recovery_per_minute": 1},
    {"off_shift_fatigue_recovery_per_minute": True},
    {"off_shift_fatigue_recovery_per_minute": float("nan")},
    {"off_shift_fatigue_recovery_per_minute": float("inf")},
    {"policy_version": STAFF_DYNAMICS_POLICY_VERSION, "shift_windows_by_staff": {"S01": []}},
    {"policy_version": STAFF_DYNAMICS_POLICY_VERSION, "shift_windows_by_staff": {}},
    {"policy_version": STAFF_DYNAMICS_POLICY_VERSION, "off_shift_fatigue_recovery_per_minute": 0.1},
    {"policy_version": STAFF_DYNAMICS_POLICY_VERSION, "off_shift_fatigue_recovery_per_minute": 0.0},
]


def _outcome(rule: Any, *arguments: Any, **options: Any) -> tuple[str, str]:
    try:
        return "value", _text(rule(*arguments, **options))
    except ValueError as error:
        return "error", str(error)


@needs_kernels
@pytest.mark.parametrize("raw", CONFIGS, ids=[f"config{i}" for i in range(len(CONFIGS))])
def test_the_config_kernel_gives_the_python_result_or_leaves_it_to_python(raw: Any) -> None:
    python = canonical_staff_dynamics_config.__wrapped__
    expected = _outcome(python, copy.deepcopy(raw))
    try:
        actual = _text(_kernels().canonical_config(copy.deepcopy(raw)))
    except native.Unsupported:
        actual = None
    if actual is not None:
        assert ("value", actual) == expected
    # What every caller sees is the same whichever of the two ran.
    assert _outcome(canonical_staff_dynamics_config, copy.deepcopy(raw)) == expected


@needs_kernels
def test_the_config_kernel_runs_on_the_shapes_of_a_real_config() -> None:
    for raw in (
        None,
        {},
        {"policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION},
        {"shift_windows_by_staff": {"S01": [_shift()]}},
        {"max_continuous_work_minutes_by_staff": {"S01": 1}},
        {"off_shift_fatigue_recovery_per_minute": 0.01, "rest_minutes": 45},
        canonical_staff_dynamics_config.__wrapped__({"rest_minutes": 45}),
    ):
        assert _text(_kernels().canonical_config(raw)) == _text(
            canonical_staff_dynamics_config.__wrapped__(raw)
        )


@needs_kernels
def test_the_config_kernel_shares_what_the_python_rule_shares() -> None:
    windows = {"S01": [_shift()]}
    assert _kernels().canonical_config({"shift_windows_by_staff": windows})[
        "shift_windows_by_staff"
    ] is (windows)


OVERRIDES: list[Any] = [
    {"S01": [_shift()]},
    {"b": [_shift(10, 20), _shift(0, 10)], "a": []},
    {"a.b_c:d-e": [_shift()]},
    {"-a": [_shift()]},
    {"é": [_shift()]},
    {"a" * 64: []},
    {"a" * 65: []},
    {},
    [],
    {f"s{i}": [] for i in range(100)},
    {f"s{i}": [] for i in range(101)},
    {"S01": "x"},
    {"S01": [_shift(i * 2, i * 2 + 1) for i in range(21)]},
    {"S01": [_shift(i * 2, i * 2 + 1) for i in range(22)]},
    {"S01": [_shift(0, 11), _shift(10, 20)]},
    {"S01": [_shift(0, True)]},
    {"S01": [{"end_minute": 10, "start_minute": 0}]},
    {1: []},
    {f"staff{i:03d}": [_shift(j * 2, j * 2 + 1) for j in range(5)] for i in range(70)},
    {f"staff{i:03d}": [_shift(j * 2, j * 2 + 1) for j in range(5)] for i in range(99)},
    {f"staff{i:03d}": [_shift(j * 2, j * 2 + 1) for j in range(6)] for i in range(99)},
    {f"staff{i:03d}": [_shift(j * 2, j * 2 + 1) for j in range(7)] for i in range(99)},
]


@needs_kernels
@pytest.mark.parametrize("known", [None, {"S01"}, {"S01", "a", "b"}])
@pytest.mark.parametrize("value", OVERRIDES, ids=[f"override{i}" for i in range(len(OVERRIDES))])
def test_the_override_kernel_gives_the_python_result_or_leaves_it_to_python(
    value: Any, known: Any
) -> None:
    python = validate_staff_shift_windows_override.__wrapped__
    try:
        expected: tuple[str, str] = "value", _text(python(value, known_staff_ids=known))
    except (ValueError, TypeError) as error:
        expected = "error", str(error)
    try:
        actual = _text(_kernels().validate_override(value, known_staff_ids=known))
    except native.Unsupported:
        actual = None
    if actual is not None:
        assert ("value", actual) == expected


def _override_of_size(size: int) -> dict[str, Any]:
    value: dict[str, Any] = {
        f"staff{i:03d}": [_shift(j * 2, j * 2 + 1) for j in range(5)] for i in range(85)
    }

    def length() -> int:
        return len(json.dumps(value, separators=(",", ":")))

    number = 0
    while (left := size - length()) > 0:
        # A comma, two quotes, a colon and two brackets, beside the ID.
        pad = 64 if left > 140 else (left - 6 if left <= 70 else 10)
        value[f"p{number:02d}".ljust(pad, "x")] = []
        number += 1
    assert length() == size
    return value


@needs_kernels
@pytest.mark.parametrize(("size", "accepted"), [(16_383, True), (16_384, True), (16_385, False)])
def test_the_override_kernel_counts_the_json_limit_to_the_byte(size: int, accepted: bool) -> None:
    """The limit is on the length of json.dumps, so the kernel adds that length up without it."""
    value = _override_of_size(size)
    python = validate_staff_shift_windows_override.__wrapped__
    if accepted:
        assert _text(_kernels().validate_override(value)) == _text(python(value))
        return
    with pytest.raises(native.Unsupported):
        _kernels().validate_override(value)
    with pytest.raises(ValueError, match="16 KiB"):
        validate_staff_shift_windows_override(value)


def _valid_for_python(world: WorldState) -> bool:
    try:
        validate_staff_dynamics_state(world)
    except ValueError:
        return False
    return True


CORRUPTIONS: list[Any] = [
    lambda state: state.__setitem__("schema_version", 2),
    lambda state: state.__setitem__("policy_version", "other"),
    lambda state: state.__setitem__("source_id", "other"),
    lambda state: state["config"].__setitem__("schema_version", 2),
    lambda state: state["config"].__setitem__("policy_version", "other"),
    lambda state: state["config"].__setitem__("source_id", ""),
    lambda state: state["config"].__setitem__("max_continuous_work_minutes", 0),
    lambda state: state["config"].__setitem__("max_continuous_work_minutes", 1441),
    lambda state: state["config"].__setitem__("rest_minutes", 0),
    lambda state: state["config"].__setitem__("rest_minutes", 481),
    lambda state: state["config"].__setitem__("off_shift_fatigue_recovery_per_minute", 1.5),
    lambda state: state["config"].__setitem__(
        "off_shift_fatigue_recovery_per_minute", float("nan")
    ),
    lambda state: state["config"].__setitem__("shift_windows_by_staff", {"S09": []}),
    lambda state: state["config"].__setitem__("shift_windows_by_staff", {"": []}),
    lambda state: state["config"].__setitem__(
        "shift_windows_by_staff", {"S01": [_shift(0, 11), _shift(10, 20)]}
    ),
    lambda state: state["config"].__setitem__("max_continuous_work_minutes_by_staff", {"S09": 5}),
    lambda state: state["config"].__setitem__("max_continuous_work_minutes_by_staff", {"S01": 0}),
    lambda state: state["config"].__delitem__("shift_windows_by_staff"),
    lambda state: state["config"].__delitem__("off_shift_fatigue_recovery_per_minute"),
    lambda state: state["staff"].__delitem__("S01"),
    lambda state: state["staff"].__setitem__("S09", state["staff"]["S01"].copy()),
    lambda state: state["staff"]["S01"].__setitem__("fatigue", 1.5),
    lambda state: state["staff"]["S01"].__setitem__("fatigue", -0.1),
    lambda state: state["staff"]["S01"].__setitem__("fatigue", float("nan")),
    lambda state: state["staff"]["S01"].__setitem__("consecutive_work_minutes", -1),
    lambda state: state["staff"]["S01"].__setitem__("total_work_minutes", -1),
    lambda state: state["staff"]["S01"].__setitem__("total_rest_minutes", -1),
    lambda state: state["staff"]["S01"].__setitem__("rest_until_minute", -1),
    lambda state: state["staff"]["S01"].__setitem__("last_activity", "asleep"),
]


@needs_kernels
@pytest.mark.parametrize("corrupt", CORRUPTIONS)
def test_the_validator_never_takes_what_the_python_rule_turns_down(corrupt: Any) -> None:
    world = _world()
    assert rust_validated(world, KEY)
    corrupt(_state(world))
    assert not _valid_for_python(world)
    assert not rust_validated(world, KEY)


@needs_kernels
@pytest.mark.parametrize(
    "change",
    [
        lambda state: None,
        lambda state: state["config"].__setitem__("max_continuous_work_minutes_by_staff", {}),
        lambda state: state["config"].__setitem__(
            "max_continuous_work_minutes_by_staff", {"S01": 5}
        ),
        lambda state: state["config"].__setitem__(
            "shift_windows_by_staff", {"S01": [_shift()], "S02": []}
        ),
        lambda state: state["staff"]["S01"].__setitem__("fatigue", 1.0),
        lambda state: state["staff"]["S01"].__setitem__("last_activity", "resting"),
    ],
)
def test_the_validator_takes_what_the_python_rule_passes(change: Any) -> None:
    world = _world(policy_version=SHIFT_STAFF_DYNAMICS_POLICY_VERSION)
    change(_state(world))
    assert _valid_for_python(world)
    assert rust_validated(world, KEY)


@needs_kernels
def test_the_validator_takes_the_config_of_policy_v1_without_its_later_keys() -> None:
    world = _world(policy_version=STAFF_DYNAMICS_POLICY_VERSION)
    config = _state(world)["config"]
    del config["shift_windows_by_staff"]
    del config["off_shift_fatigue_recovery_per_minute"]
    assert _valid_for_python(world)
    assert rust_validated(world, KEY)
    config["max_continuous_work_minutes_by_staff"] = {}
    assert not _valid_for_python(world)
    assert not rust_validated(world, KEY)


@needs_kernels
def test_a_person_added_to_the_world_alone_fails_the_validators() -> None:
    world = _world()
    world.staff["S09"] = StaffState("S09", "z", ())
    assert not _valid_for_python(world)
    assert not rust_validated(world, KEY)
    add_staff(world, "S09")
    assert _valid_for_python(world)
    assert rust_validated(world, KEY)


# ---- A real run ----


@needs_kernels
def test_a_run_never_forwards_a_write_of_the_tick_and_the_kernel_runs(
    generated_scenario: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    engine.step()
    before = dict(_mirror(world).stats())
    worked = 0
    for _ in range(60):
        engine.step()
        assert _mirror(world).verify()
        worked += sum(
            record["last_activity"] == "working" for record in _state(world)["staff"].values()
        )
    after = _mirror(world).stats()
    assert after["whole_writes"] == before["whole_writes"]
    assert after["forwards"] == before["forwards"]
    assert _mirror(world).tainted is None
    assert worked > 0


def test_the_source_of_a_shift_policy_is_its_own() -> None:
    config = canonical_staff_dynamics_config({"shift_windows_by_staff": {}})
    assert config["source_id"] == SHIFT_STAFF_DYNAMICS_SOURCE_ID
