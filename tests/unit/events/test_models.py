"""ScheduledEvent keeps its payload and provenance as detached, frozen JSON."""

from __future__ import annotations

import pytest

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


def test_scheduled_event_payload_is_detached_immutable_and_json_compatible() -> None:
    caller_payload = {"labels": ["initial", {"source": "scenario"}]}
    event = _event("immutable", payload=caller_payload)
    caller_payload["labels"][0] = "changed"
    caller_payload["labels"][1]["source"] = "changed"

    assert event.to_dict()["payload"] == {"labels": ["initial", {"source": "scenario"}]}
    with pytest.raises(TypeError):
        event.payload["new"] = "value"  # type: ignore[index]
    with pytest.raises(ValueError, match="finite JSON values"):
        _event("not-json", payload={"value": float("nan")})


def test_an_event_read_back_from_saved_json_equals_the_event_built_from_it() -> None:
    """ScheduledEvent.from_saved skips only encoding the payload again to check it is
    JSON; it freezes it and runs every other check, and other documents go to the
    constructor."""
    from types import MappingProxyType

    event = ScheduledEvent(
        event_id="saved-1",
        kind=EventKind.EXOGENOUS,
        due_minute=3,
        priority=1,
        sequence=7,
        event_type="record",
        payload={"rows": [{"value": 1.5}], "label": "避難所"},
        source_id="source",
        causation_id=None,
        provenance={"marker": {"phase": "start"}},
    )
    document = event.to_dict()
    restored = ScheduledEvent.from_saved(document)
    assert restored == event and restored.to_dict() == document
    assert isinstance(restored.payload, MappingProxyType)
    assert isinstance(restored.payload["rows"], tuple)
    assert restored.kind is EventKind.EXOGENOUS
    with pytest.raises(ValueError, match="stable ID"):
        ScheduledEvent.from_saved({**document, "event_id": "not stable!"})
    with pytest.raises(TypeError):
        ScheduledEvent.from_saved({**document, "extra": 1})
