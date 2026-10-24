"""Bounded read-only queries over saved ShelterSim run artifacts."""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
import re
from collections import deque
from pathlib import Path
from typing import Any

from sheltersim.domain.layouts import LayoutSpec
from sheltersim.experiments.profiles import select_representative_profiles
from sheltersim.experiments.scenario import ProfileSpec

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENTITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
_PRIVATE_FIELD = re.compile(
    r"(?:api[_-]?(?:key|token)|access[_-]?token|credential|password|secret|prompt|raw[_-]?text|"
    r"email|phone|address|person[_-]?name|full[_-]?name)",
    re.IGNORECASE,
)
_MAX_PROFILE_PAGE = 100
_MAX_EVENT_PAGE = 500
_MAX_TIMELINE_POINTS = 2000
_GYM_LAYOUT_PATH = (
    Path(__file__).resolve().parents[3] / "fixtures" / "layouts" / "gym_synthetic.json"
)
_METRIC_COLUMNS = frozenset(
    {
        "minute_index",
        "toilet_queue_length",
        "toilet_active_users",
        "toilet_working_units",
        "toilet_wait_count",
        "toilet_wait_total_minutes",
        "toilet_wait_mean_minutes",
        "toilet_wait_max_minutes",
        "stress_mean",
        "stress_high_count",
        "staff_assigned_task_count",
        "staff_busy_count",
        "staff_load_ratio",
        "unresolved_support_request_count",
        "support_no_qualified_worker_count",
        "support_no_qualified_worker_request_minutes",
        "support_fairness_promoted_assignment_count",
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
    }
)
_OPERATIONAL_METRIC_COLUMNS = frozenset(
    {
        "toilet_urgency_mean",
        "hunger_need_mean",
        "hydration_need_mean",
        "water_queue_length",
        "meal_queue_length",
        "water_active_service_count",
        "meal_active_service_count",
        "water_service_wait_count",
        "water_service_wait_total_minutes",
        "water_service_wait_mean_minutes",
        "water_service_wait_max_minutes",
        "meal_service_wait_count",
        "meal_service_wait_total_minutes",
        "meal_service_wait_mean_minutes",
        "meal_service_wait_max_minutes",
        "water_outage_stall_count",
        "resource_stock_stall_count",
        "water_ml_remaining",
        "food_meal_units_remaining",
        "water_ml_reserved",
        "food_meal_units_reserved",
        "support_no_qualified_worker_count",
        "support_no_qualified_worker_request_minutes",
        "support_fairness_promoted_assignment_count",
        "support_wait_staff_busy_request_minutes",
        "support_wait_staff_off_shift_request_minutes",
    }
)


class WebQueryError(ValueError):
    """Invalid or unavailable saved-run request for the local web inspector."""


class InspectorService:
    """Read-only artifact facade with path, payload and response-size allowlists."""

    def __init__(self, runs_directory: str | Path) -> None:
        self.runs_directory = Path(runs_directory).resolve()

    def list_runs(self) -> dict[str, Any]:
        runs = []
        if not self.runs_directory.is_dir():
            return {"runs": []}
        for directory in sorted(self.runs_directory.iterdir(), key=lambda item: item.name):
            if not directory.is_dir() or not _RUN_ID.fullmatch(directory.name):
                continue
            if directory.resolve().parent != self.runs_directory:
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = _read_object(manifest_path)
            except WebQueryError:
                continue
            runs.append(
                {
                    "id": directory.name,
                    "scenario_id": manifest.get("scenario_id"),
                    "seed": manifest.get("seed"),
                    "final_minute": manifest.get("final_minute"),
                    "status": manifest.get("run_status"),
                    "branch_id": manifest.get("branch_id", "main"),
                }
            )
        return {"runs": runs}

    def overview(self, run_id: str) -> dict[str, Any]:
        directory = self._run_directory(run_id)
        manifest = _read_object(directory / "manifest.json")
        profiles = _read_object(directory / "agents_initial.json").get("profiles", [])
        planned = _read_object(directory / "events_planned.json").get("events", [])
        final_metrics = _last_metrics_row(directory / "metrics.csv")
        return {
            "run_id": directory.name,
            "manifest": _safe_manifest_fields(
                manifest,
                (
                    "scenario_id",
                    "scenario_schema_version",
                    "world_state_schema_version",
                    "scenario_hash",
                    "scenario_definition_hash",
                    "seed",
                    "duration_minutes",
                    "final_minute",
                    "run_status",
                    "execution_mode",
                    "trace_level",
                    "branch_id",
                    "parent_branch_id",
                    "application_version",
                    "rng_algorithm",
                    "scenario_provenance",
                    "scenario_input_source_ids",
                    "scheduled_event_source_ids",
                    "model_configs",
                    "model_assumptions",
                    "llm_usage",
                    "initial_stock",
                    "resource_ledger_summary",
                ),
            ),
            "profile_count": len(profiles) if isinstance(profiles, list) else 0,
            "planned_event_count": len(planned) if isinstance(planned, list) else 0,
            "realized_event_count": manifest.get("realized_event_count", 0),
            "final_metrics": final_metrics,
        }

    def resource_ledger(self, run_id: str) -> dict[str, Any]:
        """Return the versioned resource ledger when the run opted in."""
        directory = self._run_directory(run_id)
        manifest = _read_object(directory / "manifest.json")
        if "resource_ledger_summary" not in manifest:
            return {"enabled": False}
        ledger = _read_object(directory / "resource_ledger.json")
        if set(ledger) != {
            "schema_version",
            "warehouse_id",
            "zone_id",
            "accounts",
            "used_reservation_ids",
            "audit",
        }:
            raise WebQueryError("resource_ledger.json has an unsupported shape")
        return {"enabled": True, "ledger": ledger}

    def agent_stories(
        self,
        run_id: str,
        *,
        at_minute: int | None = None,
    ) -> dict[str, Any]:
        """Return small evidence-backed synthetic Agent examples from saved run records."""
        directory = self._run_directory(run_id)
        manifest = _read_object(directory / "manifest.json")
        final_minute = manifest.get("final_minute", 0)
        if at_minute is None:
            at_minute = final_minute if isinstance(final_minute, int) else 0
        duration = manifest.get("duration_minutes")
        if (
            not isinstance(at_minute, int)
            or isinstance(at_minute, bool)
            or at_minute < 0
            or (isinstance(duration, int) and at_minute > duration)
        ):
            raise WebQueryError("at_minute is outside this run's duration")

        profile_records = _read_object(directory / "agents_initial.json").get("profiles", [])
        if not isinstance(profile_records, list):
            raise WebQueryError("agents_initial.json has no profiles list")
        try:
            profiles = [ProfileSpec.model_validate(record) for record in profile_records]
        except (TypeError, ValueError) as exc:
            raise WebQueryError("agents_initial.json contains an invalid profile") from exc
        selections = select_representative_profiles(profiles)
        decisions_path = directory / "decisions.jsonl"
        decisions_available = decisions_path.is_file()
        decisions_by_agent: dict[str, list[dict[str, Any]]] = {}
        if decisions_available:
            for record in _iter_jsonl(decisions_path):
                agent_id = record.get("agent_id")
                minute = record.get("minute_index")
                if (
                    isinstance(agent_id, str)
                    and isinstance(minute, int)
                    and not isinstance(minute, bool)
                    and minute <= at_minute
                    and isinstance(record.get("observed_state"), dict)
                ):
                    decisions_by_agent.setdefault(agent_id, []).append(record)

        events_by_agent: dict[str, list[dict[str, Any]]] = {}
        events_path = directory / "events_realized.jsonl"
        if events_path.is_file():
            for record in _iter_jsonl(events_path):
                event = record.get("event")
                payload = event.get("payload") if isinstance(event, dict) else None
                agent_id = payload.get("agent_id") if isinstance(payload, dict) else None
                if not isinstance(agent_id, str):
                    continue
                projected = _realized_event(record)
                if projected["minute"] <= at_minute:
                    events_by_agent.setdefault(agent_id, []).append(projected)

        stories = []
        for selection in selections:
            profile = selection.profile
            history = sorted(
                decisions_by_agent.get(profile.id, []),
                key=lambda item: (item.get("minute_index", -1), item.get("policy_version", "")),
            )
            current = history[-1] if history else None
            samples = _bounded_agent_story_history(history, maximum=8)
            events = sorted(
                events_by_agent.get(profile.id, []),
                key=lambda item: (item["minute"], item["event_id"]),
            )[-8:]
            stories.append(
                {
                    "criterion_id": selection.criterion_id,
                    "label": selection.label,
                    "selection_reason": selection.selection_reason,
                    "profile": _profile_projection(profile.model_dump(mode="json")),
                    "state_at_minute": (
                        _state_projection(current["observed_state"], current["minute_index"])
                        if current is not None
                        else {
                            "minute_index": 0,
                            "needs": profile.initial_needs.model_dump(mode="json"),
                            "source": "initial profile only; per-decision state was not recorded",
                        }
                    ),
                    "history_status": "recorded" if history else "not_recorded",
                    "history": samples,
                    "event_references": [
                        {
                            "event_id": event["event_id"],
                            "minute": event["minute"],
                            "event_type": event["event_type"],
                            "status": event["status"],
                            "reason": event.get("reason"),
                        }
                        for event in events
                    ],
                    "dialogue": None,
                    "dialogue_status": "not_generated",
                }
            )
        return {
            "run_id": run_id,
            "at_minute": at_minute,
            "selection_method": "synthetic profile fields; stable criteria and Agent-ID tie break",
            "trace_level": manifest.get("trace_level"),
            "decision_trace_available": decisions_available,
            "stories": stories,
        }

    def profiles(
        self,
        run_id: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        _validate_page(offset, limit, maximum=_MAX_PROFILE_PAGE)
        document = _read_object(self._run_directory(run_id) / "agents_initial.json")
        profiles = document.get("profiles")
        if not isinstance(profiles, list):
            raise WebQueryError("agents_initial.json has no profiles list")
        needle = query.casefold().strip()
        selected = [
            _profile_projection(item)
            for item in sorted(profiles, key=lambda item: str(item.get("id", "")))
            if not needle
            or needle
            in " ".join(
                str(item.get(key, "")) for key in ("id", "group_id", "initial_zone_id", "mobility")
            ).casefold()
        ]
        return {
            "items": selected[offset : offset + limit],
            "total": len(selected),
            "offset": offset,
            "limit": limit,
        }

    def events(
        self,
        run_id: str,
        *,
        start_minute: int | None = None,
        end_minute: int | None = None,
        query: str = "",
        branch_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
        status: str = "all",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        _validate_page(offset, limit, maximum=_MAX_EVENT_PAGE)
        _validate_range(start_minute, end_minute)
        _validate_entity_filter("agent_id", agent_id)
        _validate_entity_filter("zone_id", zone_id)
        if status not in {"all", "planned", "realized"}:
            raise WebQueryError("status must be all, planned, or realized")
        directory = self._run_directory(run_id)
        manifest = _read_object(directory / "manifest.json")
        planned_rows = []
        if status in ("all", "planned"):
            planned_doc = _read_object(directory / "events_planned.json")
            rows = planned_doc.get("events", [])
            if not isinstance(rows, list):
                raise WebQueryError("events_planned.json has no events list")
            planned_rows = sorted(
                rows,
                key=lambda item: (
                    item.get("due_minute", 0),
                    item.get("priority", 0),
                    item.get("sequence", 0),
                    str(item.get("id", "")),
                ),
            )
        realized_path = directory / "events_realized.jsonl"
        include_realized = status in ("all", "realized") and realized_path.exists()
        planned_events = (_planned_event(item) for item in planned_rows)
        realized_events = (
            (_realized_event(item) for item in _iter_jsonl(realized_path))
            if include_realized
            else iter(())
        )
        combined = heapq.merge(
            planned_events,
            realized_events,
            key=lambda item: (
                item["minute"],
                0 if item["status"] == "planned" else 1,
                item["event_id"],
            ),
        )
        needle = query.casefold().strip()
        run_branch = manifest.get("branch_id", "main")
        selected = []
        total = 0
        status_counts: dict[str, int] = {}
        for item in combined:
            matches = (
                (start_minute is None or item["minute"] >= start_minute)
                and (end_minute is None or item["minute"] <= end_minute)
                and (
                    not needle
                    or needle
                    in f"{item['event_id']} {item['event_type']} {item['status']}".casefold()
                )
                and (agent_id is None or item.get("related_agent_id") == agent_id)
                and (zone_id is None or item.get("related_zone_id") == zone_id)
                and (
                    branch_id is None
                    or item.get("branch_id") in (None, branch_id)
                    and (item.get("branch_id") is not None or branch_id == run_branch)
                )
            )
            if not matches:
                continue
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
            if offset <= total < offset + limit:
                selected.append(item)
            total += 1
        return {
            "items": selected,
            "total": total,
            "offset": offset,
            "limit": limit,
            "status_counts": status_counts,
        }

    def event(self, run_id: str, event_id: str) -> dict[str, Any]:
        events = self.events(run_id, query=event_id, limit=_MAX_EVENT_PAGE)
        matching = [item for item in events["items"] if item["event_id"] == event_id]
        if not matching:
            raise WebQueryError(f"event {event_id!r} does not exist in this run")
        return {"event_id": event_id, "records": matching}

    def agent(
        self,
        run_id: str,
        agent_id: str,
        *,
        at_minute: int | None = None,
        start_minute: int | None = None,
        end_minute: int | None = None,
    ) -> dict[str, Any]:
        _validate_range(start_minute, end_minute)
        directory = self._run_directory(run_id)
        overview = self.overview(run_id)
        if at_minute is None:
            at_minute = overview["manifest"].get("final_minute", 0)
        if not isinstance(at_minute, int) or isinstance(at_minute, bool) or at_minute < 0:
            raise WebQueryError("at_minute must be a non-negative integer")
        duration = overview["manifest"].get("duration_minutes")
        if isinstance(duration, int) and at_minute > duration:
            raise WebQueryError("at_minute cannot exceed this run's duration")
        profile = next(
            (
                _profile_projection(item)
                for item in _read_object(directory / "agents_initial.json").get("profiles", [])
                if item.get("id") == agent_id
            ),
            None,
        )
        if profile is None:
            raise WebQueryError(f"Agent {agent_id!r} does not exist in this run")
        decisions_path = directory / "decisions.jsonl"
        history = []
        current = {
            "minute_index": 0,
            "zone_id": profile.get("initial_zone_id"),
            "activity": None,
            "target_id": None,
            "needs": profile.get("initial_needs", {}),
            "stress": None,
            "trust": None,
            "source": "initial profile; per-decision snapshots were not recorded",
        }
        if decisions_path.exists():
            for record in _iter_jsonl(decisions_path):
                minute = record.get("minute_index", -1)
                if record.get("agent_id") != agent_id or not isinstance(minute, int):
                    continue
                if minute > at_minute:
                    continue
                if start_minute is not None and minute < start_minute:
                    continue
                if end_minute is not None and minute > end_minute:
                    continue
                history.append(_decision_projection(record))
                state = record.get("observed_state")
                if (
                    minute <= at_minute
                    and isinstance(state, dict)
                    and minute >= current["minute_index"]
                ):
                    current = _state_projection(state, minute)
        history.sort(key=lambda item: item["minute_index"])
        return {
            "profile": profile,
            "at_minute": at_minute,
            "state": current,
            "decisions": history[-100:],
        }

    def timeline(
        self,
        run_id: str,
        *,
        start_minute: int | None = None,
        end_minute: int | None = None,
        at_minute: int | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_TIMELINE_POINTS
        ):
            raise WebQueryError(f"limit must be between 1 and {_MAX_TIMELINE_POINTS}")
        _validate_range(start_minute, end_minute)
        if at_minute is not None and (
            not isinstance(at_minute, int) or isinstance(at_minute, bool) or at_minute < 0
        ):
            raise WebQueryError("at_minute must be a non-negative integer")
        directory = self._run_directory(run_id)
        path = directory / "metrics.csv"
        try:
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except FileNotFoundError as exc:
            raise WebQueryError("metrics.csv is missing") from exc
        rows = [
            row
            for row in rows
            if (start_minute is None or _int(row.get("minute_index"), -1) >= start_minute)
            and (end_minute is None or _int(row.get("minute_index"), -1) <= end_minute)
        ]
        operational_by_minute: dict[int, dict[str, Any]] = {}
        operational_path = directory / "operational_kpis.csv"
        if operational_path.is_file():
            with operational_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    minute = _int(row.get("minute_index"), -1)
                    if (
                        minute >= 0
                        and (start_minute is None or minute >= start_minute)
                        and (end_minute is None or minute <= end_minute)
                    ):
                        operational_by_minute[minute] = _operational_metric_row(row)

        def project(row: dict[str, Any]) -> dict[str, Any]:
            minute = _int(row.get("minute_index"), -1)
            return _metric_row(row) | operational_by_minute.get(minute, {})

        stride = max(1, math.ceil(len(rows) / limit))
        selected = [project(row) for row in rows[::stride]][:limit]
        if (
            rows
            and selected
            and selected[-1].get("minute_index") != _int(rows[-1].get("minute_index"), -1)
        ):
            selected[-1] = project(rows[-1])
        snapshot = None
        if at_minute is not None:
            prior_rows = [row for row in rows if _int(row.get("minute_index"), -1) <= at_minute]
            if prior_rows:
                snapshot = project(prior_rows[-1])
        return {
            "items": selected,
            "total": len(rows),
            "stride": stride,
            "selected": snapshot,
        }

    def zone_map(
        self,
        run_id: str,
        *,
        at_minute: int | None = None,
        selected_agent_id: str | None = None,
    ) -> dict[str, Any]:
        directory = self._run_directory(run_id)
        overview = self.overview(run_id)
        manifest = overview["manifest"]
        if at_minute is None:
            at_minute = manifest.get("final_minute", 0)
        if not isinstance(at_minute, int) or isinstance(at_minute, bool) or at_minute < 0:
            raise WebQueryError("at_minute must be a non-negative integer")
        duration = manifest.get("duration_minutes")
        if isinstance(duration, int) and at_minute > duration:
            raise WebQueryError("at_minute cannot exceed this run's duration")
        zones_path = directory / "zones.json"
        if zones_path.exists():
            document = _read_object(zones_path)
            zones = document.get("zones", [])
            edges = document.get("edges", [])
        else:
            profiles = _read_object(directory / "agents_initial.json").get("profiles", [])
            zone_ids = sorted(
                {item.get("initial_zone_id") for item in profiles if item.get("initial_zone_id")}
            )
            zones = [{"id": zone_id, "name": zone_id, "capacity": None} for zone_id in zone_ids]
            edges = []
        if not isinstance(zones, list) or not isinstance(edges, list):
            raise WebQueryError("zones.json has an invalid schema")
        projected_zones = []
        states: dict[str, dict[str, Any]] = {}
        profiles = _read_object(directory / "agents_initial.json").get("profiles", [])
        for profile in profiles:
            agent_id = profile.get("id")
            if isinstance(agent_id, str):
                states[agent_id] = {
                    "zone_id": profile.get("initial_zone_id"),
                    "activity": None,
                    "stress": None,
                    "queue_kind": None,
                    "queue_wait_minutes": None,
                    "support_status": None,
                    "support_wait_minutes": None,
                }
        decisions_path = directory / "decisions.jsonl"
        if decisions_path.exists():
            for record in _iter_jsonl(decisions_path):
                minute = record.get("minute_index")
                state = record.get("observed_state")
                if (
                    isinstance(minute, int)
                    and minute <= at_minute
                    and isinstance(state, dict)
                    and isinstance(record.get("agent_id"), str)
                ):
                    states[record["agent_id"]] = {
                        "zone_id": state.get("zone_id"),
                        "activity": state.get("activity"),
                        "stress": _unit_interval(state.get("stress")),
                        "queue_kind": _nested_string(state, "queue", "kind"),
                        "queue_wait_minutes": _optional_non_negative_number(
                            _nested_value(state, "queue", "wait_minutes")
                        ),
                        "support_status": _nested_string(state, "support", "status"),
                        "support_wait_minutes": _optional_non_negative_number(
                            _nested_value(state, "support", "wait_minutes")
                        ),
                    }
        occupancy: dict[str, int] = {}
        for state in states.values():
            zone_id = state.get("zone_id")
            if isinstance(zone_id, str):
                occupancy[zone_id] = occupancy.get(zone_id, 0) + 1
        coordinates_available = all(
            isinstance(item.get("display_x"), (int, float))
            and isinstance(item.get("display_y"), (int, float))
            for item in zones
        )
        ordered_zones = sorted(zones, key=lambda item: str(item.get("id", "")))
        layout = _read_gym_layout()
        layout_areas = layout.get("zones", []) if layout else []
        area_by_zone = {
            item.get("zone_id"): item
            for item in layout_areas
            if isinstance(item, dict) and isinstance(item.get("zone_id"), str)
        }
        x_values = (
            [float(item["display_x"]) for item in ordered_zones] if coordinates_available else []
        )
        y_values = (
            [float(item["display_y"]) for item in ordered_zones] if coordinates_available else []
        )
        x_min, x_max = (min(x_values), max(x_values)) if x_values else (0.0, 1.0)
        y_min, y_max = (min(y_values), max(y_values)) if y_values else (0.0, 1.0)
        for index, zone in enumerate(ordered_zones):
            x = (
                80 + 560 * (float(zone["display_x"]) - x_min) / (x_max - x_min)
                if coordinates_available and x_max != x_min
                else (360 if coordinates_available else 80 + (index % 4) * 140)
            )
            y = (
                55 + 220 * (float(zone["display_y"]) - y_min) / (y_max - y_min)
                if coordinates_available and y_max != y_min
                else (165 if coordinates_available else 80 + (index // 4) * 110)
            )
            area = area_by_zone.get(zone.get("id"))
            polygon = area.get("polygon") if isinstance(area, dict) else None
            if _valid_polygon(polygon):
                x, y = _polygon_center(polygon)
            projected_zones.append(
                {
                    "id": zone.get("id"),
                    "name": (
                        area.get("label")
                        if isinstance(area, dict)
                        else zone.get("name", zone.get("id"))
                    ),
                    "capacity": zone.get("capacity"),
                    "x": x,
                    "y": y,
                    "polygon": polygon,
                    "occupancy": occupancy.get(zone.get("id"), 0),
                }
            )
        if layout:
            for area in layout_areas:
                if area.get("zone_id") in {item.get("id") for item in ordered_zones}:
                    continue
                polygon = area.get("polygon")
                if not _valid_polygon(polygon):
                    continue
                x, y = _polygon_center(polygon)
                projected_zones.append(
                    {
                        "id": area.get("zone_id"),
                        "name": area.get("label", area.get("zone_id")),
                        "capacity": None,
                        "x": x,
                        "y": y,
                        "polygon": polygon,
                        "occupancy": 0,
                        "modelled": False,
                    }
                )
        if selected_agent_id is not None and selected_agent_id not in states:
            raise WebQueryError(f"Agent {selected_agent_id!r} does not exist in this run")
        selected_state = states.get(selected_agent_id) if selected_agent_id else None
        zone_shapes = {
            item["id"]: item.get("polygon")
            for item in projected_zones
            if item.get("id") is not None
        }
        agent_points = []
        for agent_id in sorted(states):
            state = states[agent_id]
            zone_id = state.get("zone_id")
            polygon = zone_shapes.get(zone_id)
            if not _valid_polygon(polygon):
                continue
            x, y = _deterministic_display_point(agent_id, str(zone_id), at_minute, polygon)
            agent_points.append(
                {
                    "id": agent_id,
                    "zone_id": zone_id,
                    "activity": state.get("activity"),
                    "stress": state.get("stress"),
                    "queue_kind": state.get("queue_kind"),
                    "queue_wait_minutes": state.get("queue_wait_minutes"),
                    "support_status": state.get("support_status"),
                    "support_wait_minutes": state.get("support_wait_minutes"),
                    "x": x,
                    "y": y,
                }
            )
        facility_markers = _facility_markers(
            directory, _read_object(directory / "manifest.json"), layout, at_minute
        )
        queue_summary = _queue_summary(directory, at_minute)
        return {
            "at_minute": at_minute,
            "synthetic_layout": True if layout else not coordinates_available,
            "layout": (
                {
                    "id": layout.get("id"),
                    "title": layout.get("title"),
                    "source": layout.get("source"),
                    "width": layout.get("width"),
                    "height": layout.get("height"),
                    "note": layout.get("note"),
                }
                if layout
                else None
            ),
            "zones": projected_zones,
            "agents": agent_points,
            "facilities": facility_markers,
            "queue_summary": queue_summary,
            "edges": [
                {
                    "id": edge.get("id"),
                    "from_zone_id": edge.get("from_zone_id"),
                    "to_zone_id": edge.get("to_zone_id"),
                    "closed": edge.get("closed", False),
                }
                for edge in edges
            ],
            "selected_agent": (
                {"id": selected_agent_id, **selected_state} if selected_state else None
            ),
        }

    def social_contacts(
        self,
        run_id: str,
        *,
        start_minute: int | None = None,
        end_minute: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _validate_range(start_minute, end_minute)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_EVENT_PAGE
        ):
            raise WebQueryError(f"limit must be between 1 and {_MAX_EVENT_PAGE}")
        path = self._run_directory(run_id) / "social_contacts.jsonl"
        if not path.exists():
            return {"items": [], "total": 0}
        rows = []
        total = 0
        for item in _iter_jsonl(path):
            if (start_minute is not None and item.get("minute_index", -1) < start_minute) or (
                end_minute is not None and item.get("minute_index", -1) > end_minute
            ):
                continue
            total += 1
            if len(rows) < limit:
                rows.append(item)
        return {"items": rows, "total": total}

    def audit(self, run_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Return bounded model/fallback records without prompts or generated text."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise WebQueryError("limit must be between 1 and 500")
        directory = self._run_directory(run_id)
        llm_path = directory / "llm_calls.jsonl"
        social_path = directory / "social_decisions.jsonl"
        llm_calls = _tail_jsonl(llm_path, limit) if llm_path.exists() else []
        social = _tail_jsonl(social_path, limit) if social_path.exists() else []
        llm_fields = (
            "call_index",
            "request_id",
            "stage",
            "outcome",
            "model_id",
            "input_hash",
            "state_hash",
            "prompt_version",
            "schema_version",
            "api_request_count",
            "input_tokens",
            "output_tokens",
            "estimated_cost_usd",
            "elapsed_ms",
            "cache_hit",
            "fallback_used",
            "error_kind",
        )
        social_fields = (
            "minute",
            "agent_id",
            "request_id",
            "trigger",
            "interaction_type",
            "target_agent_id",
            "trigger_id",
            "action",
            "status",
            "accepted",
            "reason",
            "reason_short",
            "effect_code",
            "event_id",
            "model_id",
            "input_hash",
            "state_hash",
            "api_request_count",
            "input_tokens",
            "output_tokens",
            "estimated_cost_usd",
            "source_id",
            "cache_hit",
            "fallback_used",
        )
        return {
            "llm_calls": [_safe_fields(item, llm_fields) for item in llm_calls],
            "social_decisions": [_safe_fields(item, social_fields) for item in social],
        }

    def decisions(
        self,
        run_id: str,
        *,
        agent_id: str | None = None,
        start_minute: int | None = None,
        end_minute: int | None = None,
        at_minute: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return recent typed policy decisions, omitting unapproved artifact fields."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise WebQueryError("limit must be between 1 and 200")
        _validate_entity_filter("agent_id", agent_id)
        _validate_range(start_minute, end_minute)
        if at_minute is not None and (
            not isinstance(at_minute, int) or isinstance(at_minute, bool) or at_minute < 0
        ):
            raise WebQueryError("at_minute must be a non-negative integer")
        directory = self._run_directory(run_id)
        path = directory / "decisions.jsonl"
        selected: deque[dict[str, Any]] = deque(maxlen=limit)
        total = 0
        available = path.is_file()
        if available:
            for record in _iter_jsonl(path):
                minute = record.get("minute_index")
                if not isinstance(minute, int) or isinstance(minute, bool):
                    continue
                if agent_id is not None and record.get("agent_id") != agent_id:
                    continue
                if start_minute is not None and minute < start_minute:
                    continue
                if end_minute is not None and minute > end_minute:
                    continue
                if at_minute is not None and minute > at_minute:
                    continue
                projected_agent_id = _safe_identifier(record.get("agent_id"))
                if projected_agent_id is None:
                    continue
                total += 1
                selected.append({"agent_id": projected_agent_id, **_decision_projection(record)})
        return {
            "items": list(selected),
            "total": total,
            "limit": limit,
            "available": available,
            "trace_level": _read_object(directory / "manifest.json").get("trace_level"),
        }

    def branch_lineage(self, run_id: str) -> dict[str, Any]:
        """Project recorded branch identity without inventing missing command history."""
        directory = self._run_directory(run_id)
        manifest = _read_object(directory / "manifest.json")
        fields = (
            "scenario_id",
            "seed",
            "scenario_hash",
            "scenario_definition_hash",
            "branch_id",
            "parent_branch_id",
            "run_status",
            "final_minute",
            "final_state_hash",
        )
        lineage_path = directory / "branch_lineage.json"
        lineage = _read_object(lineage_path) if lineage_path.is_file() else None
        return {
            "run_id": directory.name,
            "recorded": lineage is not None,
            "manifest": _safe_fields(manifest, fields),
            "lineage": _branch_lineage_projection(lineage) if lineage is not None else None,
            "note": None
            if lineage is not None
            else (
                "This saved Run records branch IDs and final hash only; "
                "branch command history was not exported."
            ),
        }

    def _run_directory(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise WebQueryError("run ID contains unsupported characters")
        directory = (self.runs_directory / run_id).resolve()
        if directory.parent != self.runs_directory or not directory.is_dir():
            raise WebQueryError(f"run {run_id!r} does not exist")
        if not (directory / "manifest.json").is_file():
            raise WebQueryError(f"run {run_id!r} has no manifest")
        return directory


def _profile_projection(record: dict[str, Any]) -> dict[str, Any]:
    provenance = record.get("provenance")
    safe_provenance = _safe_fields(
        provenance if isinstance(provenance, dict) else {},
        ("source_kind", "generator", "seed"),
    )
    needs = record.get("initial_needs")
    safe_needs = {
        key: _unit_interval(needs[key])
        for key in ("toilet_urgency", "hunger", "hydration_need", "fatigue")
        if isinstance(needs, dict) and _unit_interval(needs.get(key)) is not None
    }
    projected = {
        key: _safe_identifier(record.get(key))
        for key in ("id", "group_id", "initial_zone_id", "mobility")
    }
    projected["support_need"] = _unit_interval(record.get("support_need"))
    projected["stress_sensitivity"] = _unit_interval(record.get("stress_sensitivity"))
    projected["initial_needs"] = safe_needs
    projected["provenance"] = safe_provenance
    return projected


def _planned_event(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    scheduled_minute = _optional_non_negative_integer(record.get("due_minute"))
    return {
        "event_id": _safe_identifier(record.get("id")) or "unknown",
        "minute": scheduled_minute,
        "scheduled_minute": scheduled_minute,
        "realized_minute": None,
        "created_minute": _optional_non_negative_integer(record.get("created_minute")),
        "event_type": _safe_identifier(record.get("event_type")) or "unknown",
        "kind": _safe_identifier(record.get("kind")),
        "status": "planned",
        **_event_relations(payload),
        "source_id": _safe_identifier(record.get("source_id")),
        "causation_id": _safe_identifier(record.get("causation_id")),
        "branch_id": _safe_identifier(record.get("provenance", {}).get("branch_id"))
        if isinstance(record.get("provenance"), dict)
        else None,
    }


def _read_gym_layout() -> dict[str, Any] | None:
    spec = load_gym_layout_spec()
    return spec.model_dump(mode="json") if spec is not None else None


def load_gym_layout_spec() -> LayoutSpec | None:
    """Load and structurally validate the synthetic display-only gym layout."""
    if not _GYM_LAYOUT_PATH.is_file():
        return None
    layout = _read_object(_GYM_LAYOUT_PATH)
    try:
        return LayoutSpec.from_mapping(layout)
    except ValueError as exc:
        raise WebQueryError(f"gym_synthetic.json is invalid: {exc}") from exc


def _valid_polygon(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 3
        and all(
            isinstance(point, list)
            and len(point) == 2
            and all(
                isinstance(coordinate, (int, float))
                and not isinstance(coordinate, bool)
                and math.isfinite(coordinate)
                for coordinate in point
            )
            for point in value
        )
    )


def _valid_point(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(coordinate, (int, float))
            and not isinstance(coordinate, bool)
            and math.isfinite(coordinate)
            for coordinate in value
        )
    )


def _polygon_center(polygon: list[list[float]]) -> tuple[float, float]:
    return (
        round(sum(point[0] for point in polygon) / len(polygon), 2),
        round(sum(point[1] for point in polygon) / len(polygon), 2),
    )


def _deterministic_display_point(
    stable_id: str,
    zone_id: str,
    minute: int,
    polygon: list[list[float]],
) -> tuple[float, float]:
    """Place a symbol reproducibly without reading or consuming simulation RNG state."""
    x_min = min(point[0] for point in polygon)
    x_max = max(point[0] for point in polygon)
    y_min = min(point[1] for point in polygon)
    y_max = max(point[1] for point in polygon)
    if x_min == x_max or y_min == y_max:
        return _polygon_center(polygon)
    for attempt in range(32):
        digest = hashlib.blake2b(
            f"map-v1\0{stable_id}\0{zone_id}\0{minute}\0{attempt}".encode("utf-8"),
            digest_size=16,
        ).digest()
        x_ratio = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        y_ratio = int.from_bytes(digest[8:], "big") / (2**64 - 1)
        x = x_min + (x_max - x_min) * x_ratio
        y = y_min + (y_max - y_min) * y_ratio
        if _point_in_polygon(x, y, polygon):
            return round(x, 2), round(y, 2)
    return _polygon_center(polygon)


def _point_in_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = current
        x2, y2 = previous
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _facility_markers(
    directory: Path,
    manifest: dict[str, Any],
    layout: dict[str, Any] | None,
    at_minute: int,
) -> list[dict[str, Any]]:
    facilities = manifest.get("facilities", [])
    if not isinstance(facilities, list):
        return []
    layout_rows = layout.get("facilities", []) if layout else []
    layout_by_id = {
        item.get("facility_id"): item
        for item in layout_rows
        if isinstance(item, dict) and isinstance(item.get("facility_id"), str)
    }
    zones = {item.get("zone_id"): item for item in layout.get("zones", [])} if layout else {}
    facilities = [
        item for item in facilities if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    working = {
        item.get("id"): _non_negative_number(item.get("working_units", item.get("capacity")))
        for item in facilities
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    event_path = directory / "events_realized.jsonl"
    if event_path.is_file():
        for record in _iter_jsonl(event_path):
            minute = record.get("realized_minute")
            event = record.get("event")
            if (
                record.get("status") != "applied"
                or not isinstance(minute, int)
                or minute > at_minute
                or not isinstance(event, dict)
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            facility_id = payload.get("facility_id")
            if facility_id not in working:
                continue
            if event.get("event_type") == "facility_outage":
                working[facility_id] = max(
                    0, working[facility_id] - _non_negative_number(payload.get("units_out"))
                )
            elif event.get("event_type") == "facility_recovery":
                facility = next(item for item in facilities if item.get("id") == facility_id)
                installed = _non_negative_number(
                    facility.get("capacity", facility.get("working_units"))
                )
                working[facility_id] = min(
                    installed,
                    working[facility_id] + _non_negative_number(payload.get("units_restored")),
                )
    markers = []
    for facility in sorted(facilities, key=lambda item: item["id"]):
        facility_id = facility["id"]
        zone_id = facility.get("zone_id")
        area = zones.get(zone_id)
        polygon = area.get("polygon") if isinstance(area, dict) else None
        configured = layout_by_id.get(facility_id, {})
        if _valid_point(configured.get("point")):
            x, y = configured["point"]
        elif _valid_polygon(polygon):
            x, y = _deterministic_display_point(facility_id, str(zone_id), 0, polygon)
        else:
            continue
        installed = _non_negative_number(facility.get("capacity", facility.get("working_units")))
        available = working.get(facility_id, 0)
        markers.append(
            {
                "id": facility_id,
                "kind": facility.get("kind"),
                "zone_id": zone_id,
                "display_zone_id": configured.get("display_zone_id", zone_id),
                "x": x,
                "y": y,
                "capacity": installed,
                "working_units": available,
                "status": "stopped"
                if available <= 0
                else "outage"
                if available < installed
                else "active",
            }
        )
    return markers


def _queue_summary(directory: Path, at_minute: int) -> dict[str, Any]:
    path = directory / "metrics.csv"
    if not path.is_file():
        return {
            "toilet_queue_length": None,
            "toilet_active_users": None,
            "toilet_working_units": None,
        }
    selected = None
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            minute = _int(row.get("minute_index"), -1)
            if 0 <= minute <= at_minute:
                selected = row
    if selected is None:
        return {
            "toilet_queue_length": None,
            "toilet_active_users": None,
            "toilet_working_units": None,
        }
    return {
        key: _non_negative_number(selected.get(key))
        for key in ("toilet_queue_length", "toilet_active_users", "toilet_working_units")
    }


def _nested_value(document: dict[str, Any], parent: str, key: str) -> Any:
    value = document.get(parent)
    return value.get(key) if isinstance(value, dict) else None


def _nested_string(document: dict[str, Any], parent: str, key: str) -> str | None:
    value = _nested_value(document, parent, key)
    return _safe_public_text(value, limit=128)


def _unit_interval(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    ):
        return float(value)
    return None


def _non_negative_number(value: Any) -> int | float:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        number = max(0.0, float(value))
        return int(number) if number.is_integer() else number
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    number = max(0.0, number)
    return int(number) if number.is_integer() else number


def _realized_event(record: dict[str, Any]) -> dict[str, Any]:
    event = record.get("event") if isinstance(record.get("event"), dict) else {}
    provenance = event.get("provenance") if isinstance(event.get("provenance"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    realized_minute = _optional_non_negative_integer(record.get("realized_minute"))
    return {
        "event_id": _safe_identifier(event.get("event_id")) or "unknown",
        "minute": realized_minute,
        "scheduled_minute": _optional_non_negative_integer(event.get("due_minute")),
        "realized_minute": realized_minute,
        "created_minute": _optional_non_negative_integer(
            record.get("created_minute", event.get("created_minute"))
        ),
        "event_type": _safe_identifier(event.get("event_type")) or "unknown",
        "kind": _safe_identifier(event.get("kind")),
        "status": _safe_identifier(record.get("status")) or "unknown",
        "reason": _safe_public_text(record.get("reason"), limit=128),
        **_event_relations(payload),
        "source_id": _safe_identifier(event.get("source_id")),
        "causation_id": _safe_identifier(event.get("causation_id")),
        "branch_id": _safe_identifier(provenance.get("branch_id")),
    }


def _event_relations(payload: dict[str, Any]) -> dict[str, str | None]:
    def stable_id(*keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and _ENTITY_ID.fullmatch(value):
                return value
        return None

    return {
        "related_agent_id": stable_id("agent_id", "related_agent_id"),
        "related_zone_id": stable_id("zone_id", "related_zone_id"),
        "related_facility_id": stable_id("facility_id", "related_facility_id"),
        "causation_id": stable_id("causation_id", "parent_event_id"),
    }


def _safe_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _ENTITY_ID.fullmatch(value) else None


def _safe_public_text(value: Any, *, limit: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value[:limit]
    lowered = bounded.casefold()
    if _EMAIL.search(bounded) or _PHONE.search(bounded):
        return "[REDACTED]"
    if any(marker in lowered for marker in ("api_key", "authorization", "password", "secret=")):
        return "[REDACTED]"
    return bounded


def _safe_fields(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in fields:
        value = record.get(key)
        if isinstance(value, str):
            text = _safe_public_text(value, limit=256)
            if text is not None:
                projected[key] = text
        elif isinstance(value, bool):
            projected[key] = value
        elif isinstance(value, int) and value >= 0:
            projected[key] = value
        elif isinstance(value, float) and math.isfinite(value) and value >= 0:
            projected[key] = value
    return projected


def _safe_manifest_fields(manifest: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    projected = {}
    for key in fields:
        if key in manifest:
            value = _safe_public_value(manifest[key], depth=0)
            if value is not None:
                projected[key] = value
    return projected


def _safe_public_value(value: Any, *, depth: int) -> Any:
    if depth > 5:
        return None
    if isinstance(value, dict):
        projected = {}
        for key, nested in list(value.items())[:100]:
            if not isinstance(key, str) or _PRIVATE_FIELD.search(key):
                continue
            safe_value = _safe_public_value(nested, depth=depth + 1)
            if safe_value is not None:
                projected[key] = safe_value
        return projected
    if isinstance(value, list):
        return [
            safe_value
            for item in value[:256]
            if (safe_value := _safe_public_value(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, str):
        return _safe_public_text(value, limit=256)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _validate_entity_filter(name: str, value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not _ENTITY_ID.fullmatch(value)):
        raise WebQueryError(f"{name} contains unsupported characters")


def _branch_lineage_projection(record: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "schema_version",
        "branch_id",
        "parent_branch_id",
        "source_branch_id",
        "source_checkpoint_id",
        "fork_at_minute",
        "scenario_id",
        "seed",
        "first_divergence_minute",
        "state_hash",
    )
    projected = {}
    for key in fields:
        value = record.get(key)
        if key in {
            "branch_id",
            "parent_branch_id",
            "source_branch_id",
            "source_checkpoint_id",
            "scenario_id",
        }:
            safe_value = _safe_identifier(value)
            if safe_value is not None:
                projected[key] = safe_value
        elif key == "state_hash":
            if isinstance(value, str) and re.fullmatch(r"[A-Fa-f0-9]{64}", value):
                projected[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            projected[key] = value
    return projected


def _decision_projection(record: dict[str, Any]) -> dict[str, Any]:
    projection = {}
    for key in ("action", "selected_action", "policy_version"):
        value = _safe_identifier(record.get(key))
        if value is not None:
            projection[key] = value
    trigger = _safe_public_text(record.get("trigger"), limit=256)
    if trigger is not None:
        projection["trigger"] = trigger
    minute = _optional_non_negative_integer(record.get("minute_index"))
    if minute is not None:
        projection["minute_index"] = minute
    next_decision = _optional_non_negative_integer(record.get("next_decision_at"))
    if next_decision is not None:
        projection["next_decision_at"] = next_decision
    probability = _unit_interval(record.get("selected_probability"))
    if probability is not None:
        projection["selected_probability"] = probability
    state_hash = record.get("observed_state_hash")
    if isinstance(state_hash, str) and re.fullmatch(r"[A-Fa-f0-9]{64}", state_hash):
        projection["observed_state_hash"] = state_hash
    state = record.get("observed_state")
    if isinstance(state, dict) and minute is not None:
        projection["observed_state"] = _state_projection(state, minute)
    candidates = record.get("candidates")
    if isinstance(candidates, list):
        projection["candidates"] = [
            candidate
            for item in candidates[:32]
            if isinstance(item, dict)
            if (candidate := _candidate_projection(item))
        ]
    target = _safe_identifier(record.get("target_id"))
    if target is not None:
        projection["target_id"] = target
    return projection


def _bounded_agent_story_history(
    records: list[dict[str, Any]], *, maximum: int
) -> list[dict[str, Any]]:
    """Sample recorded pre-decision states evenly without inventing missing minutes."""
    if len(records) <= maximum:
        selected = records
    else:
        indices = sorted(
            {round(index * (len(records) - 1) / (maximum - 1)) for index in range(maximum)}
        )
        selected = [records[index] for index in indices]
    samples = []
    for record in selected:
        minute = record.get("minute_index")
        state = record.get("observed_state")
        if not isinstance(minute, int) or isinstance(minute, bool) or not isinstance(state, dict):
            continue
        samples.append(
            {
                "minute": minute,
                "selected_action": _safe_identifier(
                    record.get("selected_action", record.get("action"))
                ),
                "policy_version": _safe_identifier(record.get("policy_version")),
                "trigger": _safe_public_text(record.get("trigger"), limit=256),
                "state": _state_projection(state, minute),
            }
        )
    return samples


def _candidate_projection(record: dict[str, Any]) -> dict[str, Any]:
    projection = {}
    action = _safe_identifier(record.get("action"))
    if action is not None:
        projection["action"] = action
    if isinstance(record.get("eligible"), bool):
        projection["eligible"] = record["eligible"]
    trigger = _safe_public_text(record.get("trigger"), limit=256)
    if trigger is not None:
        projection["trigger"] = trigger
    for key in ("utility", "urgency_priority", "probability"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            projection[key] = value
    return projection


def _state_projection(record: dict[str, Any], minute: int) -> dict[str, Any]:
    needs = record.get("needs")
    safe_needs = {
        key: _unit_interval(needs[key])
        for key in ("toilet_urgency", "hunger", "hydration_need", "fatigue")
        if isinstance(needs, dict) and _unit_interval(needs.get(key)) is not None
    }
    return {
        "minute_index": minute,
        "zone_id": _safe_identifier(record.get("zone_id")),
        "activity": _safe_identifier(record.get("activity")),
        "target_id": _safe_identifier(record.get("target_id")),
        "needs": safe_needs,
        "stress": _unit_interval(record.get("stress")),
        "trust": _unit_interval(record.get("trust")),
        "queue_kind": _nested_string(record, "queue", "kind"),
        "queue_wait_minutes": _optional_non_negative_number(
            _nested_value(record, "queue", "wait_minutes")
        ),
        "support_status": _nested_string(record, "support", "status"),
        "support_wait_minutes": _optional_non_negative_number(
            _nested_value(record, "support", "wait_minutes")
        ),
        "source": "pre-decision snapshot",
    }


def _metric_row(record: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, raw in record.items():
        if key not in _METRIC_COLUMNS or raw == "":
            continue
        try:
            values[key] = int(raw) if key == "minute_index" else float(raw)
        except (TypeError, ValueError):
            continue
    return values


def _operational_metric_row(record: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, raw in record.items():
        if key not in _OPERATIONAL_METRIC_COLUMNS or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values[key] = value
    return values


def _optional_non_negative_integer(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _optional_non_negative_number(value: Any) -> int | float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return int(value) if float(value).is_integer() else float(value)
    return None


def _last_metrics_row(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            last = None
            for last in csv.DictReader(handle):
                pass
    except FileNotFoundError:
        return {}
    return _metric_row(last) if last is not None else {}


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WebQueryError(f"{path.name} line {number} is invalid JSON") from exc
                if not isinstance(record, dict):
                    raise WebQueryError(f"{path.name} line {number} must be an object")
                yield record
    except FileNotFoundError as exc:
        raise WebQueryError(f"{path.name} is missing") from exc


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WebQueryError(f"{path.name} is missing") from exc
    except json.JSONDecodeError as exc:
        raise WebQueryError(f"{path.name} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise WebQueryError(f"{path.name} must contain an object")
    return value


def _validate_page(offset: int, limit: int, *, maximum: int) -> None:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise WebQueryError("offset must be a non-negative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise WebQueryError(f"limit must be between 1 and {maximum}")


def _validate_range(start: int | None, end: int | None) -> None:
    for name, value in (("start_minute", start), ("end_minute", end)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise WebQueryError(f"{name} must be a non-negative integer")
    if start is not None and end is not None and start > end:
        raise WebQueryError("start_minute cannot be after end_minute")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    return list(deque(_iter_jsonl(path), maxlen=limit))


def _select_fields(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: record[key] for key in fields if key in record}
