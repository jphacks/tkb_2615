"""native/src/core gives CPython's results, bit for bit: floats (exp, expm1, **, hypot, sum,
fsum, max, min, round), CounterRNG draws, and the bytes and types of JSON values.

Each case is drawn from a fixed seed. SHELTERSIM_PARITY_CASES runs more of them. Skipped
without the sheltersim_native extension, except what native.py does without it.
"""

from __future__ import annotations

import json
import math
import os
import random
import struct
from enum import StrEnum

import pytest

from sheltersim import native
from sheltersim.core.rng import CounterRNG
from sheltersim.domain.models import Activity

CASES = int(os.environ.get("SHELTERSIM_PARITY_CASES", "20000"))

EDGES = [
    0.0,
    -0.0,
    1.0,
    -1.0,
    0.5,
    -0.5,
    2.0,
    5e-324,
    -5e-324,
    2.2250738585072014e-308,
    2.225073858507201e-308,
    1.7976931348623157e308,
    -1.7976931348623157e308,
    709.782712893384,
    709.7827128933841,
    -745.2,
    1e-300,
    1e300,
    math.inf,
    -math.inf,
    math.nan,
]


@pytest.fixture
def parity():
    if not native.available():
        pytest.skip("the sheltersim_native extension is not installed")
    return native.rules("parity")


def _floats(rng: random.Random, count: int) -> list[float]:
    """Random bits, the unit interval the models work in, every scale, and the edges."""
    values = list(EDGES)
    while len(values) < count:
        kind = rng.randrange(5)
        if kind == 0:
            values.append(struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0])
        elif kind == 1:
            values.append(rng.random())
        elif kind == 2:
            values.append(rng.uniform(-50.0, 50.0))
        elif kind == 3:
            values.append(rng.random() * 10.0 ** rng.randint(-320, 300))
        else:
            values.append(round(rng.uniform(-2.0, 2.0), rng.randint(0, 10)))
    return values


def _same(rust: object, python: object) -> bool:
    if type(rust) is not type(python):
        return False
    if isinstance(rust, float):
        if math.isnan(rust):
            return math.isnan(python)
        return struct.pack("<d", rust) == struct.pack("<d", python)
    return rust == python


def _assert_all_same(inputs: list, rust: list, python: list) -> None:
    wrong = [
        (given, ours, theirs)
        for given, ours, theirs in zip(inputs, rust, python, strict=True)
        if not _same(ours, theirs)
    ]
    assert not wrong, f"{len(wrong)} of {len(inputs)} differ, first: {wrong[:5]}"


def _raising_to_none(function, *arguments):
    try:
        return function(*arguments)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


@pytest.mark.parametrize("name", ["exp", "expm1"])
def test_exp_and_expm1_give_math_results_and_none_where_math_raises(parity, name) -> None:
    rng = random.Random(1)
    values = _floats(rng, CASES) + [rng.uniform(-1.0, 0.0) for _ in range(CASES)]
    python = [_raising_to_none(getattr(math, name), value) for value in values]
    _assert_all_same(values, getattr(parity, name)(values), python)


def _power(base: float, exponent: float) -> float | None:
    result = _raising_to_none(lambda: base**exponent)
    return None if isinstance(result, complex) else result


def _assert_powers_same(pairs: list[tuple[float, float]], rust: list) -> None:
    python = [_power(*pair) for pair in pairs]
    # A subnormal result is left to Python, which looks at errno.
    expected = [
        None if value is not None and abs(value) < 2.2250738585072014e-308 and value else value
        for value in python
    ]
    _assert_all_same(pairs, rust, expected)


def test_power_gives_float_pow_for_every_exponent_the_models_use(parity) -> None:
    rng = random.Random(2)
    values = _floats(rng, CASES)
    for exponent in (2.0, 0.5, 3.0, -1.0, 0.25, 1.5):
        pairs = [(value, exponent) for value in values]
        _assert_powers_same(pairs, parity.pow(pairs))


def test_power_of_two_is_libm_pow_and_not_the_product_llvm_would_write(parity) -> None:
    rng = random.Random(3)
    values = [rng.uniform(-20.0, 20.0) for _ in range(max(CASES, 100_000))]
    pairs = [(value, 2.0) for value in values]
    rust = parity.pow(pairs)
    # The int exponent of `x ** 2` becomes this same float.
    _assert_all_same(pairs, rust, [value**2 for value in values])


def test_power_of_any_two_floats_matches_or_is_left_to_python(parity) -> None:
    rng = random.Random(4)
    bases = _floats(rng, CASES)
    exponents = _floats(rng, CASES)
    rng.shuffle(exponents)
    pairs = list(zip(bases, exponents, strict=True))
    pairs += [(base, exponent) for base in EDGES for exponent in EDGES]
    _assert_powers_same(pairs, parity.pow(pairs))


def test_hypot_is_cpythons_algorithm(parity) -> None:
    rng = random.Random(5)
    values = _floats(rng, CASES)
    rows = [[values[i], values[(i * 7 + 1) % len(values)]] for i in range(len(values))]
    rows += [[rng.uniform(-30.0, 30.0), rng.uniform(-30.0, 30.0)] for _ in range(CASES)]
    rows += [[rng.random() * 2e-310, rng.random() * 2e-310] for _ in range(1000)]
    rows += [[rng.random() * 1e-320, rng.random() * 1e-320] for _ in range(1000)]
    rows += [[rng.random() * 1e308, rng.random() * 1e308] for _ in range(1000)]
    rows += [[rng.random(), rng.random() * 1e-200] for _ in range(1000)]
    rows += [[x, y] for x in EDGES for y in EDGES]
    rows += [[value] for value in EDGES] + [[]]
    rows += [[rng.uniform(-5.0, 5.0) for _ in range(rng.randint(3, 6))] for _ in range(1000)]
    _assert_all_same(rows, parity.hypot(rows), [math.hypot(*row) for row in rows])


def test_round_to_eight_digits_is_half_even_on_the_exact_value(parity) -> None:
    rng = random.Random(6)
    values = _floats(rng, CASES)
    values += [(2 * rng.randrange(1, 100_000) + 1) / 512 for _ in range(2000)]  # exact ties
    values += [-(2 * rng.randrange(1, 100_000) + 1) / 512 for _ in range(2000)]
    values += [-1e-10, 1e-10, 5e-9, -5e-9, 0.000000005, 1.5e-8, 2.5e-8]
    python = [round(value, 8) for value in values]
    _assert_all_same(values, parity.round_digits(values, 8), python)


@pytest.mark.parametrize("ndigits", [0, 1, 2, 3, 6, 12, 17, 300, 400])
def test_round_to_other_digits(parity, ndigits) -> None:
    rng = random.Random(7 + ndigits)
    values = _floats(rng, CASES // 4)
    values += [(2 * rng.randrange(1, 100_000) + 1) / 2 ** (ndigits + 1) for _ in range(500)]
    python = [round(value, ndigits) for value in values]
    _assert_all_same(values, parity.round_digits(values, ndigits), python)


def test_round_to_an_int_is_half_even(parity) -> None:
    rng = random.Random(8)
    values = _floats(rng, CASES) + [rng.randrange(-1000, 1000) + 0.5 for _ in range(2000)]
    python = [_raising_to_none(round, value) for value in values]
    python = [None if value is not None and abs(value) >= 2**63 else value for value in python]
    rust = parity.round_int(values)
    # Python raises for an infinity with OverflowError and a NaN with ValueError: both None.
    _assert_all_same(values, rust, python)


def test_max_and_min_of_floats_keep_the_first_argument_on_a_tie(parity) -> None:
    rng = random.Random(9)
    values = _floats(rng, CASES)
    pairs = [(values[i], values[(i * 3 + 1) % len(values)]) for i in range(len(values))]
    pairs += [(x, y) for x in EDGES for y in EDGES]
    _assert_all_same(pairs, parity.max_floats(pairs), [max(x, y) for x, y in pairs])
    _assert_all_same(pairs, parity.min_floats(pairs), [min(x, y) for x, y in pairs])


def test_max_of_an_int_and_a_float_returns_the_type_python_returns(parity) -> None:
    assert type(parity.max_num(0, -0.5)) is int
    assert type(parity.max_num(0, 0.0)) is int
    assert type(parity.max_num(0, -0.0)) is int
    assert type(parity.max_num(0, math.nan)) is int
    assert type(parity.max_num(0, 0.5)) is float
    rng = random.Random(10)
    numbers: list[int | float] = [0, 1, -1, 2**53, 2**53 + 1, -(2**53) - 1, 2**62, -(2**63)]
    numbers += [2**63 - 1, 9007199254740992.0, 9007199254740994.0, 9.223372036854775808e18]
    numbers += [-9.223372036854775808e18, 1e300, -1e300, 0.5, -0.5] + EDGES
    numbers += _floats(rng, 100) + [rng.randint(-10, 10) for _ in range(100)]
    pairs = [(x, y) for x in numbers for y in numbers]
    pairs += [(0, value) for value in _floats(rng, CASES)]
    for name, function in (("max_num", max), ("min_num", min)):
        kernel = getattr(parity, name)
        _assert_all_same(
            pairs, [kernel(x, y) for x, y in pairs], [function(x, y) for x, y in pairs]
        )


def test_sum_of_floats_is_the_compensated_sum_of_312(parity) -> None:
    rng = random.Random(11)
    values = _floats(rng, CASES)
    rows = [rng.sample(values, rng.randint(1, 8)) for _ in range(CASES // 2)]
    rows += [[rng.random() * 10.0 ** rng.randint(-20, 20) for _ in range(6)] for _ in range(CASES)]
    rows += [[1.0, 1e100, 1.0, -1e100], [-0.0], [-0.0, -0.0], [1e308, 1e308, -1e308], [0.1] * 10]
    _assert_all_same(rows, parity.sum_floats(rows), [sum(row) for row in rows])


def test_sum_of_ints_and_floats_keeps_python_types(parity) -> None:
    rng = random.Random(12)
    rows: list[list[int | float]] = [[], [0], [1, 2], [1, 0.5], [0.5, 1], [-0.0], [0, -0.0]]
    rows += [[2**53, 1, 1.0], [1.0, 2**53, 1], [1e100, 1, -1e100], [1.0, 1e100, 1, -1e100]]
    for _ in range(CASES // 2):
        rows.append(
            [
                rng.randint(-(2**60), 2**60)
                if rng.random() < 0.4
                else rng.choice(EDGES[:18])
                if rng.random() < 0.1
                else rng.uniform(-1e6, 1e6)
                for _ in range(rng.randint(0, 7))
            ]
        )
    _assert_all_same(rows, [parity.sum_nums(row) for row in rows], [sum(row) for row in rows])


def test_sum_leaving_i64_is_left_to_python(parity) -> None:
    with pytest.raises(native.Unsupported):
        parity.sum_nums([2**63 - 1, 1])


def test_fsum(parity) -> None:
    rng = random.Random(13)
    rows = [[rng.random() * 10.0 ** rng.randint(-20, 20) for _ in range(8)] for _ in range(CASES)]
    rows += [[1e308, 1e308, -1e308], [1e16, 1.0, 1e-16, -1e16], []]
    python = [_raising_to_none(math.fsum, row) for row in rows]
    # A partial sum that leaves the finite range is left to Python.
    rust = parity.fsum(rows)
    kept = [(row, r, p) for row, r, p in zip(rows, rust, python, strict=True) if r is not None]
    _assert_all_same([k[0] for k in kept], [k[1] for k in kept], [k[2] for k in kept])
    assert len(kept) >= len(rows) - 1


TEXTS = [
    "a",
    "resident",
    "A001",
    "",
    'q"uo\\te',
    "\n\t\r\b\f",
    "\x00\x1f\x7f",
    "避難所",
    "😀 é",
    "é\u2028",
]


def _key_parts(rng: random.Random) -> list[str | int]:
    parts: list[str | int] = []
    for _ in range(rng.randint(1, 6)):
        kind = rng.randrange(3)
        if kind == 0:
            parts.append(rng.choice(TEXTS))
        elif kind == 1:
            parts.append(rng.randint(-(2**62), 2**62))
        else:
            parts.append(f"agent-{rng.randrange(1000)}")
    return parts


@pytest.mark.parametrize("seed", [0, 1, 42, 20261031, 2**63 - 1, 2**64 + 7, 10**40])
def test_keyed_draws_are_counter_rng_draws(parity, seed) -> None:
    rng = random.Random(seed % 1000)
    keys = [_key_parts(rng) for _ in range(CASES // 4)]
    reference = CounterRNG(seed)
    python = [reference.random(*key) for key in keys]
    _assert_all_same(keys, parity.draws(str(seed), keys), python)


def test_keys_written_from_a_mark_are_the_same_draws(parity) -> None:
    reference = CounterRNG(7)
    python = [reference.random("social-contact-bucket-v1", 9600, slot) for slot in range(100)]
    rust = parity.draws_from_mark("7", "social-contact-bucket-v1", 9600, 100)
    _assert_all_same(list(range(100)), rust, python)


def _json_value(rng: random.Random, depth: int = 0) -> object:
    kind = rng.randrange(10 if depth < 5 else 7)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, 1, -1, 2**53, 2**63 - 1, -(2**63), 2**63, -(2**80), 10**30])
    if kind in (3, 4):
        value = _floats(rng, len(EDGES) + 1)[-1] if rng.random() < 0.5 else rng.choice(EDGES[:18])
        return value if math.isfinite(value) else 0.25
    if kind in (5, 6):
        return rng.choice(TEXTS)
    if kind == 7:
        return [_json_value(rng, depth + 1) for _ in range(rng.randrange(5))]
    keys = rng.sample(TEXTS + ["z", "b", "aa", "é", "￿", "😀"], rng.randrange(6))
    return {key: _json_value(rng, depth + 1) for key in keys}


def _same_value(x: object, y: object) -> bool:
    if type(x) is not type(y):
        return False
    if isinstance(x, dict):
        return list(x) == list(y) and all(_same_value(x[key], y[key]) for key in x)
    if isinstance(x, list):
        return len(x) == len(y) and all(_same_value(a, b) for a, b in zip(x, y, strict=True))
    return _same(x, y)


def test_values_write_the_canonical_bytes_and_come_back_the_same(parity) -> None:
    rng = random.Random(14)
    for _ in range(CASES // 10):
        value = _json_value(rng)
        expected = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        assert parity.value_json(value) == expected
        assert native.canonical_json(value) == expected
        assert _same_value(parity.value_round_trip(value), value)


class _Tracked(dict):
    pass


class _TrackedList(list):
    pass


class _Text(StrEnum):
    A = "a"


@pytest.mark.parametrize(
    "value", [(1,), {1: 2}, _Text.A, {"a": (1,)}, object(), b"x", {"a": {"b": [(1,)]}}]
)
def test_values_that_would_not_come_back_the_same_are_unsupported(parity, value) -> None:
    with pytest.raises(native.Unsupported):
        parity.value_round_trip(value)


def test_nan_and_infinity_are_left_to_json(parity) -> None:
    for value in (math.nan, math.inf, [{"a": -math.inf}]):
        with pytest.raises(native.Unsupported):
            parity.value_json(value)


def test_values_read_through_tracked_containers(parity) -> None:
    value = _Tracked(b=_TrackedList([1, 2.0]), a=None)
    assert parity.value_json(value) == b'{"a":null,"b":[1,2.0]}'
    back = parity.value_round_trip(value)
    assert type(back) is dict and type(back["b"]) is list and list(back) == ["b", "a"]


def test_an_id_has_one_python_object(parity) -> None:
    first, second, third = parity.interned(["A001", "A002", "".join(["A0", "01"])])
    assert first == "A001" and second == "A002"
    assert third is first


def test_enum_members_come_back_as_the_singletons(parity) -> None:
    members = list(Activity)
    found = parity.enum_members(Activity, [*members, str(members[0].value)])
    for index, member in enumerate(members):
        assert found[index][0] == index
        assert found[index][1] is member
    assert found[-1] is None


def test_lane_modules_are_found_by_name(parity) -> None:
    assert callable(parity.exp)
    with pytest.raises(AttributeError):
        native.rules("no_such_rule_module")
    with pytest.raises(AttributeError):
        native.state("no_such_state_module")


def test_without_the_extension_lane_modules_are_none(monkeypatch) -> None:
    monkeypatch.setattr(native, "_native", None)
    assert native.rules("parity") is None
    assert native.state("anything") is None
