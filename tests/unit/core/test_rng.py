"""CounterRNG draws depend on the seed and a stable key, never on call order."""

from __future__ import annotations

import hashlib
import json
from enum import IntEnum, StrEnum

import pytest

from sheltersim.core.rng import CounterRNG


def test_counter_rng_uses_stable_domain_keys_not_call_order() -> None:
    rng = CounterRNG(2026)
    first_a = rng.random("agent", "A001", "minute", 17)
    first_b = rng.random("agent", "A002", "minute", 17)

    assert first_a == CounterRNG(2026).random("agent", "A001", "minute", 17)
    assert first_b == CounterRNG(2026).random("agent", "A002", "minute", 17)
    assert first_a != first_b
    assert first_a != CounterRNG(2027).random("agent", "A001", "minute", 17)


def test_counter_rng_requires_a_stable_key() -> None:
    rng = CounterRNG(4)

    try:
        rng.random()
    except ValueError as error:
        assert "stable RNG key" in str(error)
    else:
        raise AssertionError("an unkeyed random request should fail")


def _reference_draw(seed: int, *key_parts: object) -> float:
    # The documented sha256-keyed-v1 draw, keyed by the JSON array of the key parts.
    encoded_key = json.dumps(
        list(key_parts), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    digest = hashlib.sha256(
        b"sheltersim:sha256-keyed-v1\0" + str(seed).encode("ascii") + b"\0" + encoded_key
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) / 2**64


def test_counter_rng_draws_match_the_json_keyed_definition_for_every_kind_of_part() -> None:
    """Keys are written part by part without json.dumps; the draws must not change."""

    class Phase(StrEnum):
        MOVE = "move"

    class Level(IntEnum):
        HIGH = 3

    parts = [
        "A001",
        "",
        "日本語の区域",
        'quote"d',
        "back\\slash",
        "line\nbreak",
        "\x00\x1f",
        "\x7f ",
        0,
        -17,
        2**70,
        Phase.MOVE,
        Level.HIGH,
    ]
    rng = CounterRNG(2026)
    for first in parts:
        for second in parts:
            assert rng.random(first, second, 5) == _reference_draw(2026, first, second, 5)
    for invalid in (True, 1.5, None, ["A001"]):
        with pytest.raises(TypeError, match="strings or integers"):
            rng.random("agent", invalid)
