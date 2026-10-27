"""Typed event and realized-event records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from json import dumps
from types import MappingProxyType
from typing import Any, Mapping

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class EventKind(StrEnum):
    """Origin category for a scheduled event."""

    EXOGENOUS = "exogenous"
    COMPLETION = "completion"
    ENDOGENOUS = "endogenous"
    INTERVENTION = "intervention"


class EventStatus(StrEnum):
    """Terminal result recorded after a due event is processed."""

    APPLIED = "applied"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


def _validate_id(name: str, value: str | None, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ValueError(f"{name} must be a stable ID")


def _freeze_json(value: Any) -> Any:
    # A Mapping may be a value frozen by another event; copy it like a dict.
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("event JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _json_default(value: Any) -> Any:
    """Let json.dumps check values copied from another event's frozen payload."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class ScheduledEvent:
    """Immutable, reproducibly ordered event input for the simulation."""

    event_id: str
    kind: EventKind
    due_minute: int
    priority: int
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    causation_id: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id("event_id", self.event_id)
        _validate_id("event_type", self.event_type)
        _validate_id("source_id", self.source_id, optional=True)
        _validate_id("causation_id", self.causation_id, optional=True)
        object.__setattr__(self, "kind", EventKind(self.kind))
        for name, value in (("due_minute", self.due_minute), ("sequence", self.sequence)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"event {name} must be a non-negative integer")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("event priority must be an integer")
        if not isinstance(self.payload, dict) or not isinstance(self.provenance, dict):
            raise ValueError("event payload and provenance must be objects")
        try:
            dumps(self.payload, allow_nan=False, default=_json_default)
            dumps(self.provenance, allow_nan=False, default=_json_default)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "event payload and provenance must contain finite JSON values"
            ) from exc
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        object.__setattr__(self, "provenance", _freeze_json(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "due_minute": self.due_minute,
            "priority": self.priority,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": _thaw_json(self.payload),
            "source_id": self.source_id,
            "causation_id": self.causation_id,
            "provenance": _thaw_json(self.provenance),
        }


@dataclass(frozen=True)
class EventLogRecord:
    """A due event's immutable outcome, appended in dispatch order."""

    event: ScheduledEvent
    realized_minute: int
    status: EventStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.realized_minute, int)
            or isinstance(self.realized_minute, bool)
            or self.realized_minute < 0
        ):
            raise ValueError("realized_minute must be a non-negative integer")
        object.__setattr__(self, "status", EventStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "realized_minute": self.realized_minute,
            "status": self.status.value,
            "detail": self.detail,
        }


class EventApplicationError(ValueError):
    """Expected event rejection; handlers must raise before mutating state."""


class EventSuppressed(ValueError):
    """Expected no-op event outcome; handlers must not mutate state."""
