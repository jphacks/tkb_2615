"""ScheduledEvent keeps its payload and provenance as detached, frozen JSON."""

from __future__ import annotations

from sheltersim.events.models import EventKind, ScheduledEvent


def _event(event_id: str, **fields: object) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=event_id,
        kind=EventKind.INTERVENTION,
        due_minute=0,
        priority=0,
        sequence=0,
        event_type="record",
        **fields,
    )


def test_an_event_can_be_built_from_another_events_frozen_values() -> None:
    # Handlers rebuild events from due ones; nested values arrive frozen (#76, #78).
    original = _event(
        "original",
        payload={"parameters": {"point_m": {"x": 1.0, "y": 2.0}}, "ids": ["a", "b"]},
        provenance={"effect_program_v1": {"program_id": "p", "assumption_ids": ["ASM-X"]}},
    )

    rebuilt = _event(
        "rebuilt",
        payload=dict(original.payload),
        provenance={**dict(original.provenance), "deferred_from": original.event_id},
    )

    assert rebuilt.to_dict()["payload"] == original.to_dict()["payload"]
    assert rebuilt.to_dict()["provenance"] == {
        **original.to_dict()["provenance"],
        "deferred_from": "original",
    }
    assert rebuilt.payload["parameters"] is not original.payload["parameters"]
