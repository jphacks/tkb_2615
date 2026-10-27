"""Population changes that wait for busy residents are retried with a bounded count."""

from __future__ import annotations

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.population import _schedule_departure_retry
from sheltersim.events.models import EventKind, EventSuppressed, ScheduledEvent


def _departure(retry_count: int | None) -> ScheduledEvent:
    provenance: dict[str, object] = {"effect_program_v1": {"program_id": "not-in-state"}}
    if retry_count is not None:
        provenance["population_retry"] = {"count": retry_count}
    return ScheduledEvent(
        event_id="departure",
        kind=EventKind.INTERVENTION,
        due_minute=0,
        priority=0,
        sequence=0,
        event_type="effect_program_population",
        payload={"op": "depart_evacuees"},
        provenance=provenance,
    )


def _retries(world: WorldState) -> list[ScheduledEvent]:
    return [
        event
        for event in world.event_queue.pending()
        if event.event_id.startswith("departure-retry-")
    ]


@pytest.mark.parametrize(("retry_count", "expected"), [(None, 1), (1, 2), (1438, 1439)])
def test_each_retry_counts_one_more_than_the_departure_it_retries(
    generated_scenario, retry_count, expected
) -> None:
    world = WorldState.from_scenario(generated_scenario)

    _schedule_departure_retry(world, _departure(retry_count))

    (retry,) = _retries(world)
    assert retry.provenance["population_retry"]["count"] == expected
    assert retry.provenance["deferred_from"] == "departure"
    assert retry.provenance["effect_program_v1"]["program_id"] == "not-in-state"


def test_a_departure_stops_waiting_after_a_day_of_retries(generated_scenario) -> None:
    # Before #78 was fixed the count was read as 0 every time, so this never ended.
    world = WorldState.from_scenario(generated_scenario)

    with pytest.raises(EventSuppressed, match="bounded deferral limit"):
        _schedule_departure_retry(world, _departure(1440))

    assert _retries(world) == []
