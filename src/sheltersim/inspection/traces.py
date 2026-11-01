"""Offline run artifacts with explicit trace levels and field allowlists."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from sheltersim import __version__
from sheltersim.contracts.policies import model_catalog, model_signatures, selected_models
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
from sheltersim.domain.information import (
    INFORMATION_SPREAD_STATE_KEY,
    information_spread_summary,
)
from sheltersim.domain.infrastructure import ScenarioInfrastructureSpec
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.domain.resources import (
    RESOURCE_LEDGER_KEY,
    RESOURCE_METRIC_FIELDS,
)
from sheltersim.domain.staff import STAFF_SUPPORT_POLICY_VERSION, default_support_fairness_minutes
from sheltersim.experiments.metrics import MetricSnapshot, MetricsRecorder
from sheltersim.experiments.operational_kpis import (
    OPERATIONAL_KPI_FIELDS,
    operational_kpi_values,
)
from sheltersim.experiments.scenario import PsychologyModelConfig, ScenarioSpec
from sheltersim.llm.outcomes import summarize_call_outcomes
from sheltersim.llm.runtime import (
    SOCIAL_INTERACTIONS_SOURCE_ID,
    SOCIAL_LIMITS_SOURCE_ID,
    SUPPORT_PRIORITY_LIMITS_SOURCE_ID,
    SUPPORT_PRIORITY_POLICY_VERSION,
    SocialRuntimeLimits,
    SupportPriorityLimits,
)
from sheltersim.policies.behavior import (
    ActionProposal,
    behavior_policy_catalog,
    behavior_policy_definition,
)
from sheltersim.policies.social import SocialPropagationParameters
from sheltersim.storage.projects import EMAIL_ADDRESS, PHONE_NUMBER
from sheltersim.storage.streams import TraceSpool

_SENSITIVE_FIELDS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "credential",
    "authorization",
    "email",
    "phone",
    "address",
    "first_name",
    "last_name",
    "full_name",
    "person_name",
)


class RunArtifactWriter:
    """Write the requested JSON/JSONL/CSV records without entering simulation state."""

    # rules/decisions.rs does nothing for a decision when this class would write nothing.
    decision_sink = "artifacts"

    def __init__(
        self,
        scenario: ScenarioSpec,
        output_directory: str | Path,
        *,
        trace_level: TraceLevel = TraceLevel.SUMMARY,
    ) -> None:
        self.scenario = scenario
        self.output_directory = Path(output_directory)
        self.trace_level = TraceLevel(trace_level)
        self.metrics = MetricsRecorder()
        self._operational_kpis: list[dict[str, Any]] = []
        self._spool: TraceSpool | None = None
        self._llm_calls: list[dict[str, Any]] = []
        self._social_decisions: list[dict[str, Any]] = []
        self._psychology_trace_minute_count = 0
        self._psychology_hourly_by_agent: dict[tuple[int, str], dict[str, Any]] = {}
        configured_threshold = PsychologyModelConfig.model_validate(
            scenario.model_configs.get("psychology", {})
        ).high_distress_threshold
        self._high_distress_threshold = (
            0.7 if configured_threshold is None else configured_threshold
        )
        self._llm_review: str | None = None
        self._started = False
        self._finalized = False
        self._base_manifest: dict[str, Any] | None = None

    def write_initial(self, world: WorldState, *, execution_mode: str = "offline") -> None:
        """Write frozen scenario/profile inputs before execution begins."""
        if self._started:
            raise ValueError("run artifact writer has already started")
        if world.scenario_id != self.scenario.id or world.seed != self.scenario.seed:
            raise ValueError("WorldState does not match the artifact ScenarioSpec")
        if world.minute_index != 0:
            raise ValueError("initial artifacts must be written before the first tick")
        world.validate()
        self.output_directory.mkdir(parents=True, exist_ok=True)
        spool_path = self.output_directory / ".trace-spool.sqlite"
        spool_path.unlink(missing_ok=True)
        self._spool = TraceSpool(spool_path)
        for filename in ("llm_calls.jsonl", "social_decisions.jsonl"):
            (self.output_directory / filename).unlink(missing_ok=True)
        (self.output_directory / "psychology_contributions.jsonl").unlink(missing_ok=True)
        (self.output_directory / "psychology_contributions_hourly.jsonl").unlink(missing_ok=True)
        (self.output_directory / "information_contacts.jsonl").unlink(missing_ok=True)
        (self.output_directory / "information_spread.json").unlink(missing_ok=True)
        scenario_hash = hashlib.sha256(self.scenario.canonical_json().encode("utf-8")).hexdigest()
        scenario_definition = self.scenario.model_dump(mode="json")
        scenario_definition.pop("seed", None)
        scenario_definition_hash = hashlib.sha256(
            json.dumps(
                scenario_definition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        scenario_family = self.scenario.model_dump(mode="json")
        scenario_family.pop("id", None)
        scenario_family.pop("seed", None)
        scenario_family.pop("scheduled_events", None)
        scenario_family_hash = hashlib.sha256(
            json.dumps(
                scenario_family,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        run_id = hashlib.sha256(
            f"{scenario_hash}:{self.scenario.seed}".encode("ascii")
        ).hexdigest()[:16]
        model_assumptions = _model_assumptions(self.scenario, execution_mode=execution_mode)
        self._base_manifest = {
            "artifact_schema_version": 2,
            "application_version": __version__,
            "run_id": run_id,
            "scenario_id": self.scenario.id,
            "scenario_schema_version": self.scenario.schema_version,
            "world_state_schema_version": world.schema_version,
            "scenario_hash": scenario_hash,
            "scenario_definition_hash": scenario_definition_hash,
            "scenario_family_hash": scenario_family_hash,
            "seed": self.scenario.seed,
            "duration_minutes": self.scenario.duration_minutes,
            "rng_algorithm": "sha256-keyed-v1",
            "execution_mode": execution_mode,
            "trace_level": self.trace_level.value,
            "model_configs": _sanitize(self.scenario.model_configs),
            "policy_implementations": selected_models(self.scenario.model_configs),
            "policy_versions": model_signatures(self.scenario.model_configs),
            "policy_sources": model_catalog(),
            "model_assumptions": _sanitize(model_assumptions),
            "scenario_provenance": _sanitize(self.scenario.provenance),
            "scenario_input_source_ids": _sanitize(
                self.scenario.provenance.get("input_source_ids", {})
            ),
            "scheduled_event_source_ids": sorted(
                {event.source_id for event in self.scenario.scheduled_events if event.source_id}
            ),
            "facilities": [
                {
                    "id": facility.id,
                    "kind": facility.kind,
                    "zone_id": facility.zone_id,
                    "capacity": facility.capacity,
                    "working_units": facility.working_units,
                }
                for facility in sorted(self.scenario.facilities, key=lambda item: item.id)
            ],
            "staff": [
                {
                    "id": staff.id,
                    "zone_id": staff.zone_id,
                    "skills": list(staff.skills),
                }
                for staff in sorted(self.scenario.staff, key=lambda item: item.id)
            ],
            "run_status": "running",
        }
        if self.scenario.initial_stock is not None:
            self._base_manifest["initial_stock"] = _sanitize(
                self.scenario.initial_stock.model_dump(mode="json")
            )
        self._started = True
        self._write_json("manifest.json", self._base_manifest)
        self._write_json(
            "agents_initial.json",
            {
                "schema_version": 1,
                "scenario_id": self.scenario.id,
                "profiles": [
                    _sanitize(profile.model_dump(mode="json"))
                    for profile in sorted(self.scenario.profiles, key=lambda item: item.id)
                ],
            },
        )
        planned_events = sorted(
            self.scenario.scheduled_events,
            key=lambda item: (item.due_minute, item.priority, item.sequence, item.id),
        )
        self._write_json(
            "events_planned.json",
            {
                "schema_version": 1,
                "scenario_id": self.scenario.id,
                "events": [_sanitize(event.model_dump(mode="json")) for event in planned_events],
            },
        )
        self._write_json(
            "zones.json",
            {
                "schema_version": 1,
                "zones": [
                    {
                        "id": zone.id,
                        "name": zone.name,
                        "capacity": zone.capacity,
                        "display_x": zone.display_x,
                        "display_y": zone.display_y,
                    }
                    for zone in sorted(self.scenario.zones, key=lambda item: item.id)
                ],
                "edges": [
                    edge.model_dump(mode="json")
                    for edge in sorted(self.scenario.zone_edges, key=lambda item: item.id)
                ],
            },
        )

    def capture_metric(self, world: WorldState, *, minute: int | None = None) -> MetricSnapshot:
        """Capture one summary snapshot; the record is detached from WorldState."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        snapshot = self.metrics.capture(world, minute=minute)
        if "resource_service_v1" in world.model_state:
            self._operational_kpis.append(operational_kpi_values(world, snapshot))
        return snapshot

    def record_decision(
        self,
        proposal: ActionProposal,
        *,
        observed_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Collect an immutable policy decision for summary/full output."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        if self.trace_level is not TraceLevel.OFF:
            snapshot = _sanitize(dict(observed_state)) if observed_state is not None else None
            assert self._spool is not None
            self._spool.append(
                "decisions",
                _sanitize(
                    _decision_document(
                        proposal, full=self.trace_level is TraceLevel.FULL, observed_state=snapshot
                    )
                ),
            )

    def record_llm_call(self, record: Any) -> None:
        """Persist a sanitized call record immediately, including failures and budget stops."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        value = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        self._llm_calls.append(value)
        self._append_jsonl_line("llm_calls.jsonl", value)

    def record_social_decision(self, decision: Mapping[str, Any]) -> None:
        """Write typed social proposal acceptance/rejection without free-form model text."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        value = _sanitize(dict(decision))
        self._social_decisions.append(value)
        self._append_jsonl_line("social_decisions.jsonl", value)

    def record_social_contact_trace(self, trace: Mapping[str, Any]) -> None:
        """Collect compact selected-contact evidence without affecting simulation state."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        if self.trace_level is TraceLevel.OFF:
            return
        assert self._spool is not None
        self._spool.append("social_contacts", _sanitize(dict(trace)))

    def record_information_spread_trace(self, trace: Mapping[str, Any]) -> None:
        """Collect a per-minute trace of sampled information contacts."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        if self.trace_level is TraceLevel.OFF:
            return
        assert self._spool is not None
        self._spool.append("information_contacts", _sanitize(dict(trace)))

    def record_psychology_trace(
        self,
        trace: Mapping[str, Any],
        *,
        agent_contributions: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        """Collect causal stress contributions independently of social traces."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        if self.trace_level is TraceLevel.OFF:
            return
        self._append_jsonl_line("psychology_contributions.jsonl", _sanitize(dict(trace)))
        self._psychology_trace_minute_count += 1
        self._accumulate_psychology_contributions(int(trace["minute_index"]), agent_contributions)

    def _accumulate_psychology_contributions(
        self,
        minute: int,
        agent_contributions: Iterable[Mapping[str, Any]],
    ) -> None:
        """Retain bounded per-Agent/hour component totals for summary Trace runs."""
        hour_index = minute // 60
        for record in agent_contributions:
            agent_id = record.get("agent_id")
            if not isinstance(agent_id, str):
                raise ValueError("psychology contribution Agent ID must be a string")
            key = (hour_index, agent_id)
            bucket = self._psychology_hourly_by_agent.get(key)
            if bucket is None:
                bucket = {
                    "hour_index": hour_index,
                    "agent_id": agent_id,
                    "first_minute_index": minute,
                    "last_minute_index": minute,
                    "sample_count": 0,
                    "stress_before_total": 0.0,
                    "stress_after_total": 0.0,
                    "high_distress_minutes": 0,
                    "raw_stress_delta_total": 0.0,
                    "applied_stress_delta_total": 0.0,
                    "social_stress_delta_total": 0.0,
                    "component_totals": {},
                }
                self._psychology_hourly_by_agent[key] = bucket
            bucket["last_minute_index"] = minute
            bucket["sample_count"] += 1
            stress_before = float(record["stress_before"])
            stress_after = float(record["stress_after"])
            bucket["stress_before_total"] += stress_before
            bucket["stress_after_total"] += stress_after
            bucket["high_distress_minutes"] += int(stress_after >= self._high_distress_threshold)
            raw_stress_delta = record.get("raw_stress_delta")
            bucket["raw_stress_delta_total"] += float(
                stress_after - stress_before if raw_stress_delta is None else raw_stress_delta
            )
            bucket["applied_stress_delta_total"] += stress_after - stress_before
            bucket["social_stress_delta_total"] += float(record.get("social_stress_delta", 0.0))
            components = record.get("component_contributions", {})
            if not isinstance(components, Mapping):
                raise ValueError("psychology component contributions must be an object")
            totals = bucket["component_totals"]
            for name, amount in components.items():
                if not isinstance(name, str):
                    raise ValueError("psychology component name must be a string")
                totals[name] = totals.get(name, 0.0) + float(amount)

    def _write_psychology_hourly_contributions(self) -> int:
        if not self._psychology_hourly_by_agent:
            return 0
        rows = []
        for key in sorted(self._psychology_hourly_by_agent):
            bucket = self._psychology_hourly_by_agent[key]
            sample_count = bucket["sample_count"]
            rows.append(
                {
                    "schema_version": 1,
                    "hour_index": bucket["hour_index"],
                    "first_minute_index": bucket["first_minute_index"],
                    "last_minute_index": bucket["last_minute_index"],
                    "agent_id": bucket["agent_id"],
                    "sample_count": sample_count,
                    "mean_stress_before": bucket["stress_before_total"] / sample_count,
                    "mean_stress_after": bucket["stress_after_total"] / sample_count,
                    "high_distress_minutes": bucket["high_distress_minutes"],
                    "raw_stress_delta_total": bucket["raw_stress_delta_total"],
                    "applied_stress_delta_total": bucket["applied_stress_delta_total"],
                    "social_stress_delta_total": bucket["social_stress_delta_total"],
                    "component_totals": bucket["component_totals"],
                }
            )
        self._write_jsonl("psychology_contributions_hourly.jsonl", rows)
        return len(rows)

    def write_llm_review(
        self,
        review: str,
        *,
        structured: dict[str, Any] | None = None,
    ) -> None:
        """Save a post-run Opus review separately from simulation state and decisions."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        self._llm_review = str(_sanitize(review))
        self._write_text("review.md", self._llm_review.rstrip() + "\n")
        if structured is not None:
            self._write_json("review.json", structured)

    def finalize(self, world: WorldState) -> None:
        """Write realized events, recorded decisions, metrics and final state hash."""
        self._require_started()
        if self._finalized:
            raise ValueError("run artifacts have already been finalized")
        if world.scenario_id != self.scenario.id or world.seed != self.scenario.seed:
            raise ValueError("final WorldState does not match the artifact ScenarioSpec")
        world.validate()
        assert self._base_manifest is not None
        self._write_jsonl(
            "events_realized.jsonl",
            (record.to_dict() for record in world.event_log.records),
        )
        assert self._spool is not None
        self._write_jsonl("decisions.jsonl", self._spool.records("decisions", ordered=True))
        self._write_metrics()
        self._write_operational_kpis()
        self._write_jsonl("llm_calls.jsonl", self._llm_calls)
        self._write_jsonl("social_decisions.jsonl", self._social_decisions)
        self._write_jsonl("social_contacts.jsonl", self._spool.records("social_contacts"))
        psychology_hourly_count = self._write_psychology_hourly_contributions()
        information_state = world.model_state.get(INFORMATION_SPREAD_STATE_KEY)
        information_summary = None
        if information_state is not None:
            self._write_jsonl(
                "information_contacts.jsonl", self._spool.records("information_contacts")
            )
            information_summary = information_spread_summary(
                information_state,
                agent_count=len(world.agents),
            )
            config = world.model_configs["information_spread"]
            self._write_json(
                "information_spread.json",
                {
                    "schema_version": 1,
                    "policy_version": selected_models(self.scenario.model_configs)["information"],
                    "source_type": config["source_type"],
                    "source_id": config["source_id"],
                    "config": config,
                    "summary": information_summary,
                    "messages": information_state["messages"],
                    "agent_beliefs": information_state["beliefs"],
                    "support_requests": information_state["support_requests"],
                },
            )
        if self._llm_review is not None:
            self._write_text("review.md", self._llm_review.rstrip() + "\n")
        manifest = {
            **self._base_manifest,
            "run_status": "completed",
            "final_minute": world.minute_index,
            "final_state_hash": hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest(),
            "realized_event_count": len(world.event_log),
            "decision_count": self._spool.counts.get("decisions", 0),
            "social_decision_count": len(self._social_decisions),
            "social_contact_trace_count": self._spool.counts.get("social_contacts", 0),
            "psychology_trace_minute_count": self._psychology_trace_minute_count,
            "psychology_hourly_agent_summary_count": psychology_hourly_count,
            "metric_snapshot_count": len(self.metrics.snapshots),
            "llm_usage": _llm_usage_summary(self._llm_calls),
        }
        ledger = world.model_state.get(RESOURCE_LEDGER_KEY)
        if ledger is not None:
            self._write_json("resource_ledger.json", ledger)
            manifest["resource_ledger_summary"] = {
                "schema_version": ledger["schema_version"],
                "audit_schema_version": 1,
                "warehouse_id": ledger["warehouse_id"],
                "zone_id": ledger["zone_id"],
                "accounts": {
                    resource_id: {
                        name: value
                        for name, value in ledger["accounts"][resource_id].items()
                        if name != "reservations"
                    }
                    for resource_id in sorted(ledger["accounts"])
                },
                "audit_entry_count": len(ledger["audit"]),
            }
        resource_service = world.model_state.get("resource_service_v1")
        if resource_service is not None:
            service_config = world.model_configs["resource_service"]
            service_artifact = {
                "schema_version": 1,
                "policy_version": service_config["policy_version"],
                "source_id": service_config["source_id"],
                "active_reservation_ids": resource_service["active_reservation_ids"],
                "cancelled_task_ids": resource_service["cancelled_task_ids"],
                "queue_stall_history": resource_service["queue_stall_history"],
                "request_history": resource_service["request_history"],
            }
            self._write_json("resource_service.json", service_artifact)
            manifest["operational_kpi_snapshot_count"] = len(self._operational_kpis)
            manifest["resource_service_summary"] = {
                "schema_version": service_artifact["schema_version"],
                "policy_version": service_artifact["policy_version"],
                "source_id": service_artifact["source_id"],
                "active_reservation_count": len(service_artifact["active_reservation_ids"]),
                "cancelled_task_count": len(service_artifact["cancelled_task_ids"]),
                "queue_stall_count": len(service_artifact["queue_stall_history"]),
                "request_outcome_count": len(service_artifact["request_history"]),
            }
        operational_events = world.model_state.get("operational_events_v1")
        if operational_events is not None:
            self._write_json("operational_events.json", operational_events)
            manifest["operational_events_summary"] = {
                "schema_version": operational_events["schema_version"],
                "shipment_count": len(operational_events["shipments"]),
                "water_outage_count": len(operational_events["water_outages"]),
                "active_water_outage_count": sum(
                    record["active"] for record in operational_events["water_outages"].values()
                ),
                "unavailable_staff_count": sum(
                    not record["available"]
                    for record in operational_events["staff_status"].values()
                ),
                "cancelled_staff_task_count": len(operational_events["cancelled_staff_task_ids"]),
            }
        if information_summary is not None:
            manifest["information_spread_summary"] = information_summary
            manifest["information_contact_trace_count"] = self._spool.counts.get(
                "information_contacts", 0
            )
        self._write_json("manifest.json", manifest)
        self._spool.close(remove=True)
        self._finalized = True

    def __call__(self, phase: object, world: WorldState, minute: int, rng: object) -> None:
        """Capture KPI snapshots when composed into a TickEngine phase handler."""
        del rng
        if phase == "collect_metrics":
            self.capture_metric(world, minute=minute)

    def _write_metrics(self) -> None:
        columns = list(MetricSnapshot.__dataclass_fields__)
        resource_snapshots = self.metrics.resource_snapshots
        if resource_snapshots is not None:
            columns.extend(RESOURCE_METRIC_FIELDS)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        if resource_snapshots is None:
            for snapshot in self.metrics.snapshots:
                writer.writerow(snapshot.csv_row())
        else:
            for snapshot, resource_row in zip(
                self.metrics.snapshots, resource_snapshots, strict=True
            ):
                row = snapshot.csv_row()
                row.update({name: resource_row[name] for name in RESOURCE_METRIC_FIELDS})
                writer.writerow(row)
        self._write_text("metrics.csv", output.getvalue())

    def _write_operational_kpis(self) -> None:
        if not self._operational_kpis:
            return
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=OPERATIONAL_KPI_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(self._operational_kpis)
        self._write_text("operational_kpis.csv", output.getvalue())

    def _write_json(self, filename: str, value: Any) -> None:
        encoded = json.dumps(
            _sanitize(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        self._write_text(filename, encoded + "\n")

    def _write_jsonl(self, filename: str, records: Iterable[Any]) -> None:
        destination = self.output_directory / filename
        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            for record in records:
                stream.write(
                    json.dumps(
                        _sanitize(record),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                )
        temporary.replace(destination)

    def _append_jsonl_line(self, filename: str, record: Any) -> None:
        """Append one detached record without retaining full-run traces in memory."""
        line = json.dumps(
            _sanitize(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with (self.output_directory / filename).open("a", encoding="utf-8", newline="") as stream:
            stream.write(line + "\n")

    def _write_text(self, filename: str, contents: str) -> None:
        destination = self.output_directory / filename
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(contents, encoding="utf-8", newline="")
        temporary.replace(destination)

    def _require_started(self) -> None:
        if not self._started:
            raise ValueError("write_initial must be called before recording run artifacts")


def _decision_document(
    proposal: ActionProposal,
    *,
    full: bool,
    observed_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "minute_index": proposal.minute_index,
        "agent_id": proposal.agent_id,
        "action": proposal.action.value,
        "target_id": proposal.target_id,
        "observed_state_hash": proposal.observed_state_hash,
        "trigger": proposal.trigger,
        "selected_action": proposal.action.value,
        "selected_probability": proposal.selection_probability,
        "choice_value": proposal.choice_value,
        "rng_key": list(proposal.rng_key),
        "next_decision_at": proposal.next_decision_at,
        "policy_version": proposal.policy_version,
        "source_type": "assumption",
        "source_id": (
            "ASM-PRIORITY-RULE-BEHAVIOR-V1"
            if proposal.policy_version.startswith("priority-rule-")
            else (
                "ASM-SLEEP-AWARE-BEHAVIOR-V2"
                if proposal.policy_version.startswith("sleep-aware-utility-softmax-v2")
                else "ASM-UTILITY-BEHAVIOR-V1"
            )
        ),
    }
    if full:
        document["candidates"] = [
            {
                "action": candidate.action.value,
                "eligible": candidate.eligible,
                "trigger": candidate.trigger,
                "utility": candidate.utility,
                "urgency_priority": candidate.urgency_priority,
                "probability": candidate.probability,
            }
            for candidate in proposal.candidates
        ]
    if observed_state is not None:
        document["observed_state"] = observed_state
    return document


def _model_assumptions(scenario: ScenarioSpec, *, execution_mode: str) -> dict[str, Any]:
    """Record the active values and stable assumption IDs for every simulator policy."""
    configs = scenario.model_configs
    behavior = configs.get("behavior", {})
    resource_service = configs.get("resource_service")
    resource_service_enabled = resource_service is not None
    profile_generation = scenario.provenance.get("profile_generation")
    if not isinstance(profile_generation, Mapping):
        profile_generation = {}
    policy_variant = behavior.get("policy", "utility-softmax-v1")
    policy_definition = behavior_policy_definition(
        policy_variant,
        resource_service_enabled=resource_service_enabled,
    )
    toilet = configs.get("toilet_service", {})
    staff = configs.get("staff", {})
    selected = selected_models(configs)
    installed_catalog = model_catalog()
    social = SocialPropagationParameters.from_mapping(
        configs.get("social_propagation"),
        source_id=installed_catalog[selected["social"]]["assumption_source_id"],
    )
    information_config = InformationSpreadConfig.from_mapping(configs.get("information_spread"))
    limits = asdict(SocialRuntimeLimits())
    psychology_config = PsychologyModelConfig.model_validate(configs.get("psychology", {}))
    psychology_values = psychology_config.coefficient_values()
    assumptions = {
        "profile_generation": {
            "source_type": "assumption",
            "source_id": "ASM-SYNTHETIC-PROFILES-V1",
            "generator": profile_generation.get("generator", "counter-rng-profile-generator-v1"),
            "template": profile_generation.get("template", {}),
        },
        "psychology": {
            "source_type": "assumption",
            "source_id": installed_catalog[selected["psychology"]]["assumption_source_id"],
            "policy_version": selected["psychology"],
            "values": psychology_values,
        },
        "behavior": {
            "source_type": "assumption",
            "source_id": policy_definition["assumption_source_id"],
            "registry_version": "behavior-policy-registry-v1",
            "variant": policy_variant,
            "policy_version": policy_definition["policy_version"],
            "selection_rule": policy_definition["selection_rule"],
            "capabilities": policy_definition["capabilities"],
            "catalog": behavior_policy_catalog(),
            "values": {
                "temperature": behavior.get("temperature", 0.2),
                "decision_interval_minutes": behavior.get("decision_interval_minutes", 15),
                **(
                    {
                        "hydration_action_threshold": resource_service[
                            "hydration_action_threshold"
                        ],
                        "hunger_action_threshold": resource_service["hunger_action_threshold"],
                        "water_utility_base": resource_service["water_utility_base"],
                        "water_utility_need_weight": resource_service["water_utility_need_weight"],
                        "meal_utility_base": resource_service["meal_utility_base"],
                        "meal_utility_need_weight": resource_service["meal_utility_need_weight"],
                    }
                    if resource_service is not None
                    else {}
                ),
            },
        },
        "toilet_service": {
            "source_type": "assumption",
            "source_id": toilet.get("source_id", "ASM-TOILET-SERVICE-DURATION-V1"),
            "policy_version": "toilet-service-uniform-v1",
            "values": {
                "distribution": toilet.get("duration_distribution", "discrete_uniform_inclusive"),
                "minimum_minutes": toilet.get("minimum_minutes", 4),
                "maximum_minutes": toilet.get("maximum_minutes", 8),
            },
        },
        "staff_support": {
            "source_type": "assumption",
            "source_id": staff.get("source_id", "ASM-STAFF-SUPPORT-LIFECYCLE-V1"),
            "policy_version": STAFF_SUPPORT_POLICY_VERSION,
            "values": {
                "support_task_minutes": staff.get("support_task_minutes", 10),
                "unresolved_after_minutes": staff.get("unresolved_after_minutes", 90),
                "support_cooldown_minutes": staff.get("support_cooldown_minutes", 30),
                "support_fairness_minutes": staff.get(
                    "support_fairness_minutes",
                    default_support_fairness_minutes(staff.get("unresolved_after_minutes", 90)),
                ),
            },
        },
        "social_propagation": social.provenance(),
        "event_triggered_social_limits": {
            "source_type": "assumption",
            "source_id": SOCIAL_LIMITS_SOURCE_ID,
            "policy_version": "event-triggered-social-v2",
            "enabled": execution_mode != "offline",
            "values": limits,
        },
        "event_triggered_support_priority_limits": {
            "source_type": "assumption",
            "source_id": SUPPORT_PRIORITY_LIMITS_SOURCE_ID,
            "policy_version": SUPPORT_PRIORITY_POLICY_VERSION,
            "enabled": execution_mode != "offline",
            "values": asdict(SupportPriorityLimits()),
        },
        "event_triggered_social_interactions": {
            "source_type": "assumption",
            "source_id": SOCIAL_INTERACTIONS_SOURCE_ID,
            "policy_version": "event-triggered-social-v2",
            "effects": {
                "complaint_acknowledgement": {
                    "stress_delta": -0.02,
                    "trust_delta": 0.01,
                },
                "staff_process_explanation": {
                    "stress_delta": -0.01,
                    "trust_delta": 0.01,
                },
                "practical_information": {"trust_delta": 0.005},
                "mutual_support_per_agent": {
                    "stress_delta": -0.02,
                    "trust_delta": 0.01,
                },
                "information_verification": (
                    "raises query pressure by the configured per-claim increment, capped at 1.0"
                ),
            },
        },
        "zone_routing": {
            "source_type": "assumption",
            "source_id": "ASM-ZONE-ROUTING-V1",
            "policy_version": "shortest-travel-time-v1",
            "edge_travel_minutes": {
                edge.id: edge.travel_minutes
                for edge in sorted(scenario.zone_edges, key=lambda item: item.id)
            },
        },
        "inspector_zone_projection": {
            "source_type": "assumption",
            "source_id": "ASM-SYNTHETIC-ZONE-DISPLAY-V1",
            "policy_version": "inspector-zone-grid-v1",
            "affects_simulation": False,
        },
    }
    if resource_service is not None:
        assumptions["resource_service"] = {
            "source_type": resource_service["source_type"],
            "source_id": resource_service["source_id"],
            "policy_version": resource_service["policy_version"],
            "values": {
                key: value
                for key, value in resource_service.items()
                if key not in {"schema_version", "policy_version", "source_id", "source_type"}
            },
            "service_rule": "one idle skilled staff member per active service slot",
        }
    infrastructure = configs.get("infrastructure")
    if infrastructure is not None:
        infrastructure_config = ScenarioInfrastructureSpec.model_validate(infrastructure)
        assumptions["infrastructure"] = {
            "source_type": infrastructure_config.source_type,
            "source_id": infrastructure_config.source_id,
            "policy_version": infrastructure_config.policy_version,
            "values": {
                "dependency_edges": [
                    edge.model_dump(mode="json") for edge in infrastructure_config.dependency_edges
                ],
                "heat_stress_per_minute": infrastructure_config.heat_stress_per_minute,
                "cold_stress_per_minute": infrastructure_config.cold_stress_per_minute,
            },
            "claim_limit": "experimental event logic; not a physical safety forecast",
        }
    zone_environment = configs.get("zone_environment")
    if zone_environment is not None:
        environment_config = ScenarioZoneEnvironmentConfig.model_validate(zone_environment)
        assumptions["zone_environment"] = {
            "source_type": environment_config.source_type,
            "source_id": environment_config.source_id,
            "policy_version": environment_config.policy_version,
            "values": {
                key: value
                for key, value in environment_config.model_dump(mode="json").items()
                if key not in {"schema_version", "policy_version", "source_id", "source_type"}
            },
            "claim_limit": (
                "noise affects modeled rest recovery; separate demand modifiers affect relative "
                "action choice odds. Neither is a physical, medical, or crowd prediction"
            ),
        }
    facility_awareness = configs.get("facility_awareness")
    if facility_awareness is not None:
        from sheltersim.domain.facility_awareness import (
            FACILITY_AWARENESS_POLICY_VERSION,
            FacilityAwarenessConfig,
        )

        awareness_config = FacilityAwarenessConfig.model_validate(facility_awareness)
        assumptions["facility_awareness"] = {
            "source_type": awareness_config.source_type,
            "source_id": awareness_config.source_id,
            "policy_version": FACILITY_AWARENESS_POLICY_VERSION,
            "initial_known_facility_ids": list(awareness_config.initial_known_facility_ids),
            "discover_co_located_facilities_on_arrival": (
                awareness_config.discover_co_located_facilities_on_arrival
            ),
            "interpretation": (
                "entry orientation grants only listed facility IDs; arrival at a facility can "
                "reveal other facilities in its zone. This is a scenario assumption, not a "
                "measured information-access rate."
            ),
        }
    if information_config.enabled:
        assumptions["information_spread"] = information_config.provenance()
    for domain, config_name in (
        ("social", "social_propagation"),
        ("information", "information_spread"),
    ):
        if config_name in assumptions:
            assumptions[config_name]["policy_version"] = selected[domain]
            assumptions[config_name]["operator_source_id"] = installed_catalog[selected[domain]][
                "assumption_source_id"
            ]
    return assumptions


def _llm_usage_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = summarize_call_outcomes(records)
    return {
        "api_request_count": sum(record.get("api_request_count", 0) for record in records),
        "input_tokens": sum(record.get("input_tokens", 0) for record in records),
        "output_tokens": sum(record.get("output_tokens", 0) for record in records),
        "estimated_cost_usd": sum(record.get("estimated_cost_usd", 0.0) for record in records),
        "cache_hits": sum(1 for record in records if record.get("cache_hit")),
        # Every non-offline rule fallback, including call caps and denied allowances.
        "failures": outcomes["fallback_count"],
        "models": sorted({record.get("model_id", "") for record in records if record}),
        "outcomes": outcomes,
    }


def _sanitize(value: Any, *, field_name: str = "") -> Any:
    if field_name and _is_sensitive_field(field_name):
        return "[REDACTED]"
    if isinstance(value, Enum):
        return value.value
    if type(value) in (bool, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if EMAIL_ADDRESS.search(value) or PHONE_NUMBER.search(value):
            return "[REDACTED]"
        return value
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


@lru_cache(maxsize=256)
def _is_sensitive_field(field_name: str) -> bool:
    normalized_name = field_name.casefold().replace("-", "_")
    return (
        any(marker in normalized_name for marker in _SENSITIVE_FIELDS)
        or normalized_name == "token"
        or normalized_name.endswith("_token")
    )
