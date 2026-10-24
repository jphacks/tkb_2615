"""Read-only queries over frozen run artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class InspectorQueryError(ValueError):
    """Raised when an artifact is absent or an inspection target does not exist."""


class RunInspector:
    """Query profiles, planned/realized events, and decision JSONL by stable IDs."""

    def __init__(self, run_directory: str | Path) -> None:
        self.run_directory = Path(run_directory)

    def profiles(self) -> list[dict[str, Any]]:
        """Return every frozen Agent profile in stable ID order."""
        document = self._read_json("agents_initial.json")
        profiles = document.get("profiles")
        if not isinstance(profiles, list):
            raise InspectorQueryError("agents_initial.json has no profiles list")
        return sorted(profiles, key=lambda item: str(item.get("id", "")))

    def planned_events(self) -> list[dict[str, Any]]:
        """Return all scheduled pre-run events in deterministic order."""
        document = self._read_json("events_planned.json")
        events = document.get("events")
        if not isinstance(events, list):
            raise InspectorQueryError("events_planned.json has no events list")
        return sorted(
            events,
            key=lambda item: (
                item.get("due_minute", 0),
                item.get("priority", 0),
                item.get("sequence", 0),
                str(item.get("id", "")),
            ),
        )

    def realized_events(self) -> list[dict[str, Any]]:
        """Return event outcomes in their recorded dispatch order."""
        return self._read_jsonl("events_realized.jsonl")

    def resource_ledger(self) -> dict[str, Any]:
        """Return the full versioned ledger for a run that enabled resources."""
        return self._read_json("resource_ledger.json")

    def agent_decisions(
        self,
        agent_id: str,
        *,
        start_minute: int | None = None,
        end_minute: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return one Agent's decisions inside an inclusive minute range."""
        known_agent_ids = {profile.get("id") for profile in self.profiles()}
        if agent_id not in known_agent_ids:
            raise InspectorQueryError(f"Agent {agent_id!r} does not exist in this run")
        _validate_range(start_minute, end_minute)
        decisions = self._read_jsonl("decisions.jsonl")
        return [
            decision
            for decision in decisions
            if decision.get("agent_id") == agent_id
            and (start_minute is None or decision.get("minute_index", -1) >= start_minute)
            and (end_minute is None or decision.get("minute_index", -1) <= end_minute)
        ]

    def event(self, event_id: str) -> dict[str, Any]:
        """Return planned and realized records sharing an event ID."""
        planned = [item for item in self.planned_events() if item.get("id") == event_id]
        realized = [
            item
            for item in self.realized_events()
            if item.get("event", {}).get("event_id") == event_id
        ]
        if not planned and not realized:
            raise InspectorQueryError(f"event {event_id!r} does not exist in this run")
        return {"event_id": event_id, "planned": planned, "realized": realized}

    def causal_trace(
        self,
        *,
        agent_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Join one Agent's decisions or event provenance into a bounded causal graph."""
        if agent_id is None and event_id is None:
            raise InspectorQueryError("causal trace requires an Agent ID or event ID")
        if agent_id is not None:
            known_agent_ids = {profile.get("id") for profile in self.profiles()}
            if agent_id not in known_agent_ids:
                raise InspectorQueryError(f"Agent {agent_id!r} does not exist in this run")
        if event_id is not None:
            realized_event_ids = {
                item.get("event", {}).get("event_id") for item in self.realized_events()
            }
            if event_id not in realized_event_ids:
                planned_ids = {item.get("id") for item in self.planned_events()}
                if event_id not in planned_ids:
                    raise InspectorQueryError(f"event {event_id!r} does not exist in this run")
                raise InspectorQueryError(f"event {event_id!r} is planned but not realized")
        from sheltersim.experiments.comparison import build_causal_trace

        return build_causal_trace(
            self._read_jsonl("decisions.jsonl"),
            self.realized_events(),
            focus_agent_id=agent_id,
            focus_event_id=event_id,
        )

    def _read_json(self, filename: str) -> dict[str, Any]:
        path = self.run_directory / filename
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise InspectorQueryError(f"run artifact {filename} is missing") from exc
        except json.JSONDecodeError as exc:
            raise InspectorQueryError(f"run artifact {filename} is invalid JSON") from exc
        if not isinstance(document, dict):
            raise InspectorQueryError(f"run artifact {filename} must contain a JSON object")
        return document

    def _read_jsonl(self, filename: str) -> list[dict[str, Any]]:
        path = self.run_directory / filename
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise InspectorQueryError(f"run artifact {filename} is missing") from exc
        records: list[dict[str, Any]] = []
        for number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InspectorQueryError(f"{filename} line {number} is invalid JSON") from exc
            if not isinstance(record, dict):
                raise InspectorQueryError(f"{filename} line {number} must be a JSON object")
            records.append(record)
        return records


def _validate_range(start_minute: int | None, end_minute: int | None) -> None:
    if start_minute is not None and start_minute < 0:
        raise InspectorQueryError("range start minute cannot be negative")
    if end_minute is not None and end_minute < 0:
        raise InspectorQueryError("range end minute cannot be negative")
    if start_minute is not None and end_minute is not None and start_minute > end_minute:
        raise InspectorQueryError("range start minute cannot be after its end minute")
