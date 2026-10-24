from sheltersim_foundation.core import (
    CounterRNG,
    SimulationClock,
    TickEngine,
    TickPhase,
    WorldState,
)


def test_seed_and_stable_key_determine_random_draw() -> None:
    first = CounterRNG(17).random("agent-1", "minute", 3)
    rng = CounterRNG(17)
    rng.random("unrelated")
    assert rng.random("agent-1", "minute", 3) == first
    assert CounterRNG(18).random("agent-1", "minute", 3) != first


def test_tick_engine_uses_canonical_order_and_advances_once() -> None:
    world = WorldState("scenario", 5, 2)
    observed: list[tuple[TickPhase, int]] = []
    engine = TickEngine(world, lambda phase, state, minute, rng: observed.append((phase, minute)))

    engine.step()

    assert observed == [(phase, 0) for phase in TickPhase]
    assert world.minute_index == 1


def test_clock_rejects_invalid_bounds() -> None:
    clock = SimulationClock(1)
    clock.advance()
    assert clock.complete
