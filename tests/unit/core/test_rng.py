"""CounterRNG draws depend on the seed and a stable key, never on call order."""

from __future__ import annotations

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
