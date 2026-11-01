"""Offline, in-memory Day 3 checkpoint and intervention demonstration."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.events.effects import validate_effect_program
from sheltersim.experiments.branch import (
    BranchError,
    BranchRuntime,
    RelocateFacilityCommand,
    SetZoneEdgeCommand,
    command_from_dict,
)
from sheltersim.experiments.comparison import compare_branches
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.scenario_editor import (
    ScenarioEditorRequest,
    build_scenario_preview,
    list_scenario_presets,
    scenario_family_hash,
    scenario_hash,
)
from sheltersim.llm.config import LLMSettings
from sheltersim.llm.natural_language import (
    InterventionCompilerUnavailable,
    _sanitize_narrative,
    compile_natural_language,
    scenario_summary_from_world,
)
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.report import (
    build_narrative_card,
    build_review_evidence,
    generate_evidence_review,
    scenario_assumption_ids,
)
from sheltersim.llm.schemas import (
    ApprovedPlan,
    MultiPlanApproval,
    MultiPlanDraft,
    ParseRequest,
    multi_plan_effect_signature,
)
from sheltersim.web.inspector import load_gym_layout_spec

_DEFAULT_SCENARIO = Path(__file__).with_name("examples") / "failure_day3.json"
_MAX_ADVANCE = 3000
_MAX_OPERATIONS = 1000
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMPARISON_METRICS = frozenset(
    {
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
    }
)
_ROUTING_EVIDENCE_LIMIT = 100


def _routing_metrics(world: WorldState) -> dict[str, Any]:
    """Summarize modeled travel and capacity waits with bounded event references."""
    completed_travel: list[dict[str, Any]] = []
    pending_travel: list[dict[str, Any]] = []
    capacity_waits: list[dict[str, Any]] = []
    for record in world.event_log.records:
        event = record.event
        if event.event_type == "agent_travel_complete" and record.status.value == "applied":
            completed_travel.append(
                {
                    "event_id": event.event_id,
                    "source_id": event.provenance.get("source_id", event.source_id),
                    "causation_id": event.causation_id,
                    "minute": record.realized_minute,
                    "travel_minutes": event.payload.get("travel_minutes", 0),
                    "edge_ids": list(event.payload.get("route_edge_ids", ())),
                }
            )
        elif event.event_type == "zone_edge_capacity_wait" and record.status.value == "suppressed":
            capacity_waits.append(
                {
                    "event_id": event.event_id,
                    "source_id": event.provenance.get("source_id", event.source_id),
                    "causation_id": event.causation_id,
                    "minute": record.realized_minute,
                    "agent_id": event.payload.get("agent_id"),
                    "edge_id": event.payload.get("edge_id"),
                }
            )
    for event in world.event_queue.pending():
        if event.event_type == "agent_travel_complete":
            pending_travel.append(
                {
                    "event_id": event.event_id,
                    "source_id": event.provenance.get("source_id", event.source_id),
                    "causation_id": event.causation_id,
                    "minute": event.due_minute,
                    "travel_minutes": event.payload.get("travel_minutes", 0),
                    "edge_ids": list(event.payload.get("route_edge_ids", ())),
                }
            )

    travel = completed_travel + pending_travel
    travel_minutes = sum(
        item["travel_minutes"]
        for item in travel
        if isinstance(item["travel_minutes"], int)
        and not isinstance(item["travel_minutes"], bool)
        and item["travel_minutes"] >= 0
    )
    waiting_agents = {item["agent_id"] for item in capacity_waits if item["agent_id"]}
    evidence = {
        "completed_travel_events": completed_travel[:_ROUTING_EVIDENCE_LIMIT],
        "pending_travel_events": pending_travel[:_ROUTING_EVIDENCE_LIMIT],
        "capacity_wait_events": capacity_waits[:_ROUTING_EVIDENCE_LIMIT],
        "truncated_count": max(0, len(completed_travel) - _ROUTING_EVIDENCE_LIMIT)
        + max(0, len(pending_travel) - _ROUTING_EVIDENCE_LIMIT)
        + max(0, len(capacity_waits) - _ROUTING_EVIDENCE_LIMIT),
    }
    return {
        "travel_trip_count": len(travel),
        "travel_minutes_total": travel_minutes,
        "capacity_wait_observation_count": len(capacity_waits),
        "capacity_wait_unique_agent_count": len(waiting_agents),
        "evidence": evidence,
    }


class DemoError(ValueError):
    """Invalid or unavailable offline demo operation."""


class DemoConflictError(DemoError):
    """A preview no longer matches the current state of its source Branch."""


@dataclass(frozen=True, slots=True)
class DemoSnapshotView:
    """Canonical detached JSON for one checkpoint; each read returns a fresh copy."""

    _document_json: str

    @classmethod
    def capture(cls, document: dict[str, Any]) -> DemoSnapshotView:
        return cls(
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._document_json)


@dataclass
class _Branch:
    runtime: BranchRuntime
    status: str
    checkpoints: dict[str, Checkpoint] = field(default_factory=dict)
    next_checkpoint: int = 0


class _MetricSink:
    """Minimal artifact sink used by the normal offline controller in this demo."""

    trace_level = TraceLevel.OFF
    # See experiments/timeline.py.
    decision_sink = "silent"

    def __init__(self, metrics: MetricsRecorder) -> None:
        self.metrics = metrics

    def capture_metric(self, world: WorldState, *, minute: int | None = None) -> None:
        self.metrics.capture(world, minute=minute)

    def record_decision(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_social_decision(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_social_contact_trace(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class DemoService:
    """A single-process offline demo that reuses the checkpoint and branch runtime."""

    def __init__(
        self,
        scenario: ScenarioSpec | None = None,
        *,
        scenario_storage_path: str | Path | None = None,
        usage_store: Any | None = None,
    ) -> None:
        self.usage_store = usage_store
        self.scenario_storage_path = (
            Path(scenario_storage_path) if scenario_storage_path is not None else None
        )
        restored_manifest = None
        if scenario is None and self.scenario_storage_path is not None:
            restored = self._load_frozen_scenario(self.scenario_storage_path)
            if restored is not None:
                scenario, restored_manifest = restored
        self.scenario = scenario or ScenarioSpec.load(_DEFAULT_SCENARIO)
        self.layout = load_gym_layout_spec()
        if self.layout is not None:
            self.layout.validate_scenario(self.scenario)
        self.branches: dict[str, _Branch] = {}
        self.operations: list[dict[str, Any]] = []
        self.started = False
        self._lock = RLock()
        self._active_jobs: dict[str, str] = {}
        self.compiler_router: LLMRouter | None = None
        self._compiler_routers: dict[str, LLMRouter] = {}
        self.natural_language_previews: dict[str, dict[str, Any]] = {}
        self.natural_language_audit: list[dict[str, Any]] = []
        self.layout_branch_receipts: dict[str, dict[str, Any]] = {}
        self.scenario_run_manifest: dict[str, Any] | None = restored_manifest

    def llm_router(self, branch_id: str | None = None) -> LLMRouter:
        """Return the branch-scoped legacy router backed by the shared UsageLedger."""
        key = branch_id or "unscoped"
        if key not in self._compiler_routers:
            self._compiler_routers[key] = self._accounted_router(branch_id=branch_id)
        self.compiler_router = self._compiler_routers[key]
        return self.compiler_router

    def _accounted_router(self, *, branch_id: str | None = None) -> LLMRouter:
        from sheltersim.llm.accounting import runtime_accounted_router, runtime_usage_store

        return runtime_accounted_router(
            self.usage_store or runtime_usage_store(),
            account_id="legacy_demo",
            operation="legacy_demo",
            branch_id=branch_id,
            settings=LLMSettings.from_env(),
        )

    def frame_view(self, branch_id: str, *, at_minute: int | None = None) -> dict[str, Any]:
        from sheltersim.inspection.frames import world_frame

        with self._lock:
            branch = self._require_branch(branch_id)
            minute = branch.runtime.world.minute_index if at_minute is None else at_minute
            if minute == branch.runtime.world.minute_index:
                world = WorldState.from_dict(branch.runtime.world.to_dict())
            else:
                checkpoint = next(
                    (cp for cp in branch.checkpoints.values() if cp.simulation_minute == minute),
                    None,
                )
                if checkpoint is None:
                    return {
                        "exact": False,
                        "branch_id": branch_id,
                        "minute": minute,
                        "limitation": (
                            "Legacy demo has no exact checkpoint here; use Project timeline for "
                            "any-minute replay."
                        ),
                    }
                world = checkpoint.restore_world()
            frame = world_frame(
                world,
                run_id="legacy-demo",
                branch_id=branch_id,
                computed_until=branch.runtime.world.minute_index,
            )
            frame.update({"namespace": "legacy_demo", "status": branch.status})
            return frame

    def compare_frames(self, branch_ids: list[str], *, at_minute: int) -> dict[str, Any]:
        if not 2 <= len(branch_ids) <= 8 or len(set(branch_ids)) != len(branch_ids):
            raise DemoError("legacy comparison needs 2–8 distinct branches")
        frames = [self.frame_view(key, at_minute=at_minute) for key in branch_ids]
        return {
            "namespace": "legacy_demo",
            "minute": at_minute,
            "frames": frames,
            "exact": all(frame["exact"] for frame in frames),
            "interpretation": (
                "Legacy demo checkpoints only; Project and Demo identities cannot be mixed."
            ),
        }

    def scenario_presets(self) -> dict[str, Any]:
        """Return fixed, packaged scenario choices with bounded comparison metadata."""
        return {"presets": list_scenario_presets(), "maximum_agent_count": 100}

    def preview_scenario(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate a detached operator draft and return a summary without mutating the demo."""
        try:
            request = ScenarioEditorRequest.model_validate(body)
            _scenario, preview = build_scenario_preview(request)
        except ValueError as exc:
            raise DemoError(str(exc)) from exc
        return {"preview": preview.to_dict(), "frozen": False}

    def freeze_scenario(self, body: dict[str, Any]) -> dict[str, Any]:
        """Freeze a validated operator draft as the next fresh live demo scenario."""
        with self._lock:
            if self.started or self.branches or self._active_jobs:
                raise DemoError("scenario settings are locked after the live run starts")
            try:
                request = ScenarioEditorRequest.model_validate(body)
                scenario, preview = build_scenario_preview(request)
                if self.layout is not None:
                    self.layout.validate_scenario(scenario)
            except ValueError as exc:
                raise DemoError(str(exc)) from exc
            created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            run_id = f"live-{uuid.uuid4().hex}"
            manifest = {
                **preview.to_dict(),
                "run_id": run_id,
                "created_at": created_at,
                "frozen": True,
            }
            try:
                self._save_frozen_scenario(scenario, manifest)
            except OSError as exc:
                raise DemoError(f"could not persist the frozen scenario: {exc}") from exc
            self.scenario = scenario
            self.scenario_run_manifest = manifest
            return {"scenario": dict(self.scenario_run_manifest), "state": self.state()}

    @staticmethod
    def _load_frozen_scenario(
        path: Path,
    ) -> tuple[ScenarioSpec, dict[str, Any]] | None:
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "scenario", "manifest"}
            or document["schema_version"] != 1
            or not isinstance(document["manifest"], dict)
        ):
            raise ValueError("persisted frozen scenario has an invalid format")
        scenario = ScenarioSpec.model_validate(document["scenario"])
        manifest = document["manifest"]
        if (
            manifest.get("schema_version") != 1
            or manifest.get("execution_mode") != "live_compute"
            or manifest.get("frozen") is not True
            or manifest.get("scenario_id") != scenario.id
            or manifest.get("seed") != scenario.seed
            or manifest.get("scenario_hash") != scenario_hash(scenario)
            or manifest.get("scenario_family_hash") != scenario_family_hash(scenario)
            or not isinstance(manifest.get("run_id"), str)
            or not manifest["run_id"].startswith("live-")
            or not isinstance(manifest.get("created_at"), str)
        ):
            raise ValueError("persisted frozen scenario manifest does not match its ScenarioSpec")
        return scenario, manifest

    def _save_frozen_scenario(self, scenario: ScenarioSpec, manifest: dict[str, Any]) -> None:
        if self.scenario_storage_path is None:
            return
        path = self.scenario_storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 1,
            "scenario": scenario.model_dump(mode="json"),
            "manifest": manifest,
        }
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(encoded + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def dispatch(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate and apply one named operation, recording both accepts and rejects."""
        with self._lock:
            body = {} if payload is None else payload
            if not isinstance(body, dict):
                raise DemoError("request body must be a JSON object")
            branch_id = body.get("branch_id", body.get("source_branch_id"))
            before = self._requested_hash(branch_id)
            try:
                if (
                    action
                    in {
                        "advance",
                        "pause",
                        "resume",
                        "checkpoint",
                        "rewind",
                        "fork",
                        "command",
                        "commands",
                        "branch-plan",
                        "multi-plan",
                        "layout-branch",
                    }
                    and branch_id in self._active_jobs
                ):
                    raise DemoError("branch has an active background job")
                result = self._apply(action, body)
                self._record(
                    action,
                    body,
                    accepted=True,
                    before_hash=before,
                    after_hash=self._requested_hash(branch_id),
                    result=result,
                )
                return result
            except (BranchError, ValueError, KeyError, TypeError) as exc:
                self._record(
                    action,
                    body,
                    accepted=False,
                    before_hash=before,
                    after_hash=self._requested_hash(branch_id),
                    error=str(exc),
                )
                if isinstance(exc, DemoError):
                    raise
                raise DemoError(str(exc)) from exc

    def state(self) -> dict[str, Any]:
        """Return bounded branch status, lineage, checkpoints and recent operations."""
        with self._lock:
            branches = [
                self._branch_summary(branch_id, branch)
                for branch_id, branch in sorted(self.branches.items())
            ]
            return {
                "started": self.started,
                "run_id": self.scenario_run_manifest.get("run_id")
                if self.scenario_run_manifest
                else None,
                "scenario_id": self.scenario.id,
                "scenario_hash": scenario_hash(self.scenario),
                "scenario_family_hash": scenario_family_hash(self.scenario),
                "seed": self.scenario.seed,
                "duration_minutes": self.scenario.duration_minutes,
                "agent_count": len(self.scenario.profiles),
                "zones": [
                    {"id": zone.id, "name": zone.name, "capacity": zone.capacity}
                    for zone in self.scenario.zones
                ],
                "layout": self.layout.model_dump(mode="json") if self.layout else None,
                "default_toilet_zone_id": next(
                    (
                        facility.zone_id
                        for facility in self.scenario.facilities
                        if "toilet" in facility.kind.casefold()
                    ),
                    self.scenario.zones[0].id,
                ),
                "branches": branches,
                "operations": self.operations[-100:],
                "offline": True,
                "execution_mode": "live_compute",
                "scenario_source": "operator_configured_scenario"
                if self.scenario_run_manifest
                and self.scenario_run_manifest.get("source_kind") == "operator_configured_scenario"
                else "packaged_or_local_scenario",
                "scenario_manifest": self.scenario_run_manifest,
                "natural_language_audit": self.natural_language_audit[-100:],
            }

    def interpret_natural_language(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded, read-only compiler preview for one existing demo Branch."""
        if set(body) != {"text", "branch_id", "selected_minute"}:
            raise DemoError("interpret requires text, branch_id, and selected_minute")
        try:
            request = ParseRequest.model_validate(
                {
                    "text": body["text"],
                    "branch_id": body["branch_id"],
                    "selected_minute": body["selected_minute"],
                }
            )
        except ValueError as exc:
            raise DemoError(str(exc)) from exc

        with self._lock:
            branch = self._require_branch(request.branch_id)
            if branch.status not in {"paused", "completed"}:
                raise DemoConflictError("pause the source branch before requesting a preview")
            if request.branch_id in self._active_jobs:
                raise DemoConflictError("pause background computation before requesting a preview")
            snapshot = WorldState.from_dict(branch.runtime.world.to_dict())
            state_hash = _world_hash(snapshot)
            replay_checkpoint = self._checkpoint_before_minute(
                branch, request.selected_minute, snapshot.minute_index
            )
            parent_branch_id = branch.runtime.parent_branch_id
        validation_snapshot = self._snapshot_at_minute(
            request.branch_id,
            parent_branch_id,
            snapshot,
            replay_checkpoint,
            request.selected_minute,
        )
        with self._lock:
            current = self._require_branch(request.branch_id)
            if (
                current.status not in {"paused", "completed"}
                or request.branch_id in self._active_jobs
                or _world_hash(current.runtime.world) != state_hash
            ):
                raise DemoConflictError("branch changed while the preview was being prepared")
        summary = scenario_summary_from_world(
            validation_snapshot,
            default_zone_id=next(
                (
                    facility.zone_id
                    for facility in validation_snapshot.facilities.values()
                    if "toilet" in facility.kind.casefold()
                ),
                None,
            ),
        )
        request = request.model_copy(update={"scenario_summary": summary})
        try:
            result = compile_natural_language(request, self.llm_router(request.branch_id))
        except InterventionCompilerUnavailable as exc:
            raise DemoConflictError(str(exc)) from exc
        plan = result.draft_plan
        validation_issues = (
            list(validate_effect_program(validation_snapshot, plan))
            if plan is not None
            else list(result.limitations)
        )
        validation = {
            "valid": plan is not None and not validation_issues,
            "issues": validation_issues,
            "source_state_hash": state_hash,
        }

        with self._lock:
            current = self._require_branch(request.branch_id)
            if (
                current.status not in {"paused", "completed"}
                or request.branch_id in self._active_jobs
                or _world_hash(current.runtime.world) != state_hash
            ):
                raise DemoConflictError("branch changed while the preview was being prepared")
            preview_id = secrets.token_urlsafe(24)
            self.natural_language_previews[preview_id] = {
                "branch_id": request.branch_id,
                "selected_minute": request.selected_minute,
                "source_state_hash": state_hash,
                "result": result,
                "validation": validation,
            }
            if len(self.natural_language_previews) > 100:
                self.natural_language_previews.pop(next(iter(self.natural_language_previews)))
            audit = {
                "preview_id": preview_id,
                "branch_id": request.branch_id,
                "selected_minute": request.selected_minute,
                "source_state_hash": state_hash,
                "source_text": _sanitize_narrative(request.text),
                "status": result.status,
                "support_mode": result.support_mode.value,
                "mechanism_summary": result.mechanism_summary,
                "preview_notice": result.preview_notice,
                "limitations": result.limitations,
                "questions": result.questions,
                "choices": result.choices,
                "fallback_used": result.fallback_used,
                "usage": result.usage.model_dump(mode="json"),
                "validation": validation,
                "effects": (
                    [
                        {
                            "effect_id": effect.effect_id,
                            "op": effect.op,
                            "target_kind": effect.target_selector.kind,
                            "target_id": effect.target_selector.target_id,
                            "start_minute": effect.start_minute,
                            "end_minute": effect.end_minute,
                            "units": effect.units,
                            "parameters": effect.parameters,
                        }
                        for effect in plan.effects
                    ]
                    if plan is not None
                    else []
                ),
                "assumptions": (
                    [item.model_dump(mode="json") for item in plan.assumptions]
                    if plan is not None
                    else []
                ),
                "compiler_provenance": (
                    plan.compiler_provenance.model_dump(mode="json") if plan is not None else None
                ),
                "approved_branch_id": None,
            }
            self.natural_language_audit.append(audit)
            del self.natural_language_audit[:-100]

        return {
            "preview_id": preview_id,
            "source_state_hash": state_hash,
            "result": result.model_dump(mode="json"),
            "validation": validation,
        }

    def validate_natural_language_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """Recheck a preview against a detached current state without changing it."""
        if set(body) != {"branch_id", "preview_id"}:
            raise DemoError("validate requires branch_id and preview_id")
        branch_id = body["branch_id"]
        preview_id = body["preview_id"]
        if not isinstance(branch_id, str) or not isinstance(preview_id, str):
            raise DemoError("branch_id and preview_id must be strings")
        with self._lock:
            branch = self._require_branch(branch_id)
            if branch.status not in {"paused", "completed"}:
                raise DemoConflictError("pause the source branch before validating a preview")
            if branch_id in self._active_jobs:
                raise DemoConflictError("pause background computation before validating a preview")
            preview = self.natural_language_previews.get(preview_id)
            if not isinstance(preview, dict) or preview.get("branch_id") != branch_id:
                raise DemoError("unknown preview_id for this Branch")
            current_snapshot = WorldState.from_dict(branch.runtime.world.to_dict())
            current_hash = _world_hash(current_snapshot)
            selected_minute = preview["selected_minute"]
            replay_checkpoint = self._checkpoint_before_minute(
                branch, selected_minute, current_snapshot.minute_index
            )
            parent_branch_id = branch.runtime.parent_branch_id
        if current_hash != preview["source_state_hash"]:
            return {
                "valid": False,
                "stale": True,
                "issues": ["branch changed after this preview; prepare a new preview"],
                "source_state_hash": current_hash,
            }
        snapshot = self._snapshot_at_minute(
            branch_id,
            parent_branch_id,
            current_snapshot,
            replay_checkpoint,
            selected_minute,
        )
        with self._lock:
            current = self._require_branch(branch_id)
            if (
                current.status not in {"paused", "completed"}
                or branch_id in self._active_jobs
                or _world_hash(current.runtime.world) != current_hash
            ):
                return {
                    "valid": False,
                    "stale": True,
                    "issues": ["branch changed during preview validation; prepare a new preview"],
                    "source_state_hash": _world_hash(current.runtime.world),
                }
        result = preview["result"]
        plan = result.draft_plan
        issues = (
            list(validate_effect_program(snapshot, plan))
            if plan is not None
            else list(result.limitations)
        )
        validation = {
            "valid": plan is not None and not issues,
            "stale": False,
            "issues": issues,
            "source_state_hash": current_hash,
        }
        return validation

    def apply_natural_language_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """Treat this explicit request as confirmation, then atomically create a Branch."""
        if set(body) != {"source_branch_id", "preview_id", "idempotency_key"}:
            raise DemoError("apply requires source_branch_id, preview_id, and idempotency_key")
        source_branch_id = body["source_branch_id"]
        preview_id = body["preview_id"]
        idempotency_key = body["idempotency_key"]
        if not all(
            isinstance(value, str) for value in (source_branch_id, preview_id, idempotency_key)
        ):
            raise DemoError("apply identifiers must be strings")
        with self._lock:
            preview = self.natural_language_previews.get(preview_id)
            if not isinstance(preview, dict) or preview.get("branch_id") != source_branch_id:
                raise DemoError("unknown preview_id for this source Branch")
            result = preview["result"]
            plan = result.draft_plan
            if result.status != "compiled" or plan is None:
                raise DemoError("only a compiled, supported preview can be approved")
            try:
                approved = ApprovedPlan.from_explicit_confirmation(plan)
            except ValueError as exc:
                raise DemoError(str(exc)) from exc
            key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            child_id = _plan_branch_id(source_branch_id, key_hash)
            is_existing = child_id in self.branches
            if not is_existing:
                validation = self.validate_natural_language_preview(
                    {"branch_id": source_branch_id, "preview_id": preview_id}
                )
                if not validation["valid"]:
                    if validation.get("stale"):
                        raise DemoConflictError(validation["issues"][0])
                    raise DemoError("preview is not valid: " + "; ".join(validation["issues"][:5]))
            if not is_existing:
                source = self._require_branch(source_branch_id)
                if source.status not in {"paused", "completed"}:
                    raise DemoConflictError("source branch must be paused before approving a plan")
                if source_branch_id in self._active_jobs:
                    raise DemoConflictError("source branch has an active background job")
            response = self.dispatch(
                "branch-plan",
                {
                    "source_branch_id": source_branch_id,
                    "approved_plan": approved.model_dump(mode="json"),
                    "idempotency_key": idempotency_key,
                },
            )
            if not response.get("idempotent_replay"):
                for audit in reversed(self.natural_language_audit):
                    if audit["preview_id"] == preview_id:
                        audit["approved_branch_id"] = response["branch_id"]
                        audit["approval_hash"] = response["plan_hash"]
                        break
            return response

    def validate_multi_plan_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate every edited alternative against one detached common origin."""
        if set(body) != {"source_branch_id", "multi_plan"}:
            raise DemoError("multi-validate requires source_branch_id and multi_plan")
        source_branch_id = body["source_branch_id"]
        if not isinstance(source_branch_id, str) or not isinstance(body["multi_plan"], dict):
            raise DemoError("source_branch_id and multi_plan have invalid types")
        try:
            multi_plan = MultiPlanDraft.model_validate(body["multi_plan"])
        except ValueError as exc:
            raise DemoError(str(exc)) from exc
        if multi_plan.branch_origin != source_branch_id:
            raise DemoError("multi-plan branch_origin does not match source_branch_id")
        payload, _, _, _ = self._preflight_multi_plan(multi_plan)
        return payload

    def apply_multi_plan_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """Require exact multi-plan confirmation and apply it as one transaction."""
        if set(body) != {"source_branch_id", "approved_plan", "idempotency_key"}:
            raise DemoError(
                "multi-apply requires source_branch_id, approved_plan, and idempotency_key"
            )
        source_branch_id = body["source_branch_id"]
        idempotency_key = body["idempotency_key"]
        if not isinstance(source_branch_id, str) or not isinstance(body["approved_plan"], dict):
            raise DemoError("source_branch_id and approved_plan have invalid types")
        if not isinstance(idempotency_key, str):
            raise DemoError("idempotency_key must be a string")
        try:
            approved = MultiPlanApproval.model_validate(body["approved_plan"])
        except ValueError as exc:
            raise DemoError(str(exc)) from exc
        if approved.multi_plan.branch_origin != source_branch_id:
            raise DemoError("approved multi-plan origin does not match source_branch_id")
        return self.dispatch(
            "multi-plan",
            {
                "source_branch_id": source_branch_id,
                "approved_plan": approved.model_dump(mode="json"),
                "idempotency_key": idempotency_key,
            },
        )

    def _preflight_multi_plan(
        self, multi_plan: MultiPlanDraft
    ) -> tuple[dict[str, Any], BranchRuntime | None, int, str | None]:
        """Replay one origin once, then validate every candidate without mutating a live Branch."""
        source_branch_id = multi_plan.branch_origin
        with self._lock:
            source = self._require_branch(source_branch_id)
            if source.status not in {"paused", "completed"}:
                raise DemoConflictError(
                    "source branch must be paused before validating alternatives"
                )
            if source_branch_id in self._active_jobs:
                raise DemoConflictError("source branch has an active background job")
            source_state_hash = _world_hash(source.runtime.world)
            source_minute = source.runtime.world.minute_index
            if source_state_hash != multi_plan.source_state_hash:
                return (
                    {
                        "valid": False,
                        "stale": True,
                        "source_state_hash": source_state_hash,
                        "issues": ["source Branch changed; prepare the alternatives again"],
                        "alternatives": [],
                    },
                    None,
                    source_minute,
                    None,
                )
            if multi_plan.fork_at_minute >= source.runtime.world.duration_minutes:
                raise DemoError("fork_at_minute must be before the scenario end")
            apply_minute = min(source_minute, multi_plan.fork_at_minute)
            eligible = [
                (checkpoint.simulation_minute, index, checkpoint_id, checkpoint)
                for index, (checkpoint_id, checkpoint) in enumerate(source.checkpoints.items())
                if checkpoint.simulation_minute <= apply_minute
            ]
            if not eligible:
                raise DemoError("no saved checkpoint or initial state is available before the fork")
            _, _, origin_checkpoint_id, origin_checkpoint = max(eligible)
            source_checkpoint_hash = origin_checkpoint.checkpoint_hash
            parent_branch_id = source.runtime.parent_branch_id

        try:
            origin = BranchRuntime.from_checkpoint(
                origin_checkpoint,
                branch_id=source_branch_id,
                parent_branch_id=parent_branch_id,
            )
            replay = _Branch(runtime=origin, status="paused")
            steps = apply_minute - origin.world.minute_index
            if steps < 0:
                raise DemoError("selected fork minute is before the saved origin checkpoint")
            if steps:
                self._advance_branch(source_branch_id, replay, steps)
            origin_checkpoint = origin.checkpoint()
            origin_world_hash = _world_hash(origin.world)
            model_versions_hash = _json_hash(
                {
                    "model_versions": origin_checkpoint.model_versions,
                    "model_configs": origin.world.model_configs,
                }
            )
        except (BranchError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, DemoError):
                raise
            raise DemoError(str(exc)) from exc

        alternative_results: list[dict[str, Any]] = []
        aggregate_issues: list[str] = []
        effect_signatures: set[str] = set()
        if len(multi_plan.branches) < 2:
            aggregate_issues.append("at least two different alternatives are required")
        for alternative in multi_plan.branches:
            plan = alternative.plan
            issues: list[str] = []
            if plan.ambiguities:
                issues.append("unresolved time, target, or scope ambiguity")
            if plan.support_mode.value in {"clarification_required", "not_simulatable"}:
                issues.append("alternative is not executable")
            issues.extend(validate_effect_program(origin.world, plan))
            signature = multi_plan_effect_signature(plan)
            if signature in effect_signatures:
                issues.append("alternative has the same modeled effects as another branch")
            effect_signatures.add(signature)
            if issues:
                aggregate_issues.extend(
                    f"{alternative.variant_id}: {issue}" for issue in issues[:5]
                )
            alternative_results.append(
                {
                    "variant_id": alternative.variant_id,
                    "label": alternative.label,
                    "valid": not issues,
                    "issues": issues[:10],
                }
            )

        with self._lock:
            current = self._require_branch(source_branch_id)
            current_hash = _world_hash(current.runtime.world)
            if (
                current.status not in {"paused", "completed"}
                or source_branch_id in self._active_jobs
                or current_hash != source_state_hash
            ):
                return (
                    {
                        "valid": False,
                        "stale": True,
                        "source_state_hash": current_hash,
                        "issues": ["source Branch changed while alternatives were validated"],
                        "alternatives": alternative_results,
                    },
                    None,
                    source_minute,
                    None,
                )

        is_valid = not aggregate_issues
        approval_hash = None
        if is_valid:
            try:
                approval_hash = MultiPlanApproval.from_explicit_confirmation(
                    multi_plan
                ).approval_hash
            except ValueError as exc:
                aggregate_issues.append(str(exc))
                is_valid = False
        return (
            {
                "valid": is_valid,
                "stale": False,
                "source_state_hash": source_state_hash,
                "fork_at_minute": multi_plan.fork_at_minute,
                "origin_checkpoint_id": origin_checkpoint_id,
                "source_checkpoint_hash": source_checkpoint_hash,
                "origin_checkpoint_hash": origin_checkpoint.checkpoint_hash,
                "origin_world_hash": origin_world_hash,
                "model_versions_hash": model_versions_hash,
                "approval_hash": approval_hash,
                "issues": aggregate_issues[:50],
                "alternatives": alternative_results,
            },
            origin,
            source_minute,
            origin_checkpoint_id,
        )

    def _checkpoint_before_minute(
        self, branch: _Branch, selected_minute: int, current_minute: int
    ) -> Checkpoint | None:
        """Select the closest saved world needed to validate a historical fork time."""
        if selected_minute >= current_minute:
            return None
        candidates = [
            (checkpoint.simulation_minute, index, checkpoint)
            for index, checkpoint in enumerate(branch.checkpoints.values())
            if checkpoint.simulation_minute <= selected_minute
        ]
        if not candidates:
            raise DemoError(
                "no saved checkpoint or initial state is available before fork_at_minute"
            )
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _snapshot_at_minute(
        self,
        branch_id: str,
        parent_branch_id: str | None,
        current_snapshot: WorldState,
        checkpoint: Checkpoint | None,
        selected_minute: int,
    ) -> WorldState:
        """Replay a detached checkpoint for validation when the selected time is in the past."""
        if checkpoint is None:
            return current_snapshot
        runtime = BranchRuntime.from_checkpoint(
            checkpoint,
            branch_id=branch_id,
            parent_branch_id=parent_branch_id,
        )
        replay_branch = _Branch(runtime=runtime, status="paused")
        steps = selected_minute - runtime.world.minute_index
        if steps < 0:
            raise DemoError("selected fork minute is before the earliest saved checkpoint")
        if steps:
            self._advance_branch(branch_id, replay_branch, steps)
        return WorldState.from_dict(runtime.world.to_dict())

    def snapshot_view(
        self,
        branch_id: str,
        *,
        at_minute: int | None = None,
        agent_id: str | None = None,
    ) -> DemoSnapshotView:
        """Capture a bounded read-only checkpoint view without exposing mutable runtime state."""
        if at_minute is not None and (
            not isinstance(at_minute, int) or isinstance(at_minute, bool) or at_minute < 0
        ):
            raise DemoError("minute must be a non-negative integer")
        with self._lock:
            branch = self._require_branch(branch_id)
            current = branch.runtime.world
            requested_minute = current.minute_index if at_minute is None else at_minute
            checkpoints = sorted(
                branch.checkpoints.items(),
                key=lambda item: (item[1].simulation_minute, item[0]),
            )
            eligible = [
                item for item in checkpoints if item[1].simulation_minute <= requested_minute
            ]
            checkpoint_id, checkpoint = eligible[-1] if eligible else checkpoints[0]
            world = checkpoint.restore_world()
            if agent_id is not None and not _IDEMPOTENCY_KEY.fullmatch(agent_id):
                raise DemoError("agent_id contains unsupported characters")
            agent = world.agents.get(agent_id) if agent_id is not None else None
            if agent_id is not None and agent is None:
                raise DemoError("unknown agent_id")
            metric_samples = checkpoint.restore_metrics().snapshots
            selected_metric = next(
                (
                    sample.csv_row()
                    for sample in reversed(metric_samples)
                    if sample.minute_index <= requested_minute
                ),
                None,
            )
            command_fields = {
                "command",
                "kind",
                "command_id",
                "branch_id",
                "source_branch_id",
                "source_checkpoint_id",
                "fork_at_minute",
                "event_ids",
            }
            commands = []
            for command in branch.runtime.command_log[-20:]:
                if not isinstance(command, dict):
                    continue
                projected = {}
                for key in command_fields:
                    value = command.get(key)
                    if isinstance(value, str) and len(value) <= 128:
                        projected[key] = value
                    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        projected[key] = value
                    elif (
                        isinstance(value, list)
                        and len(value) <= 100
                        and all(isinstance(item, str) and len(item) <= 128 for item in value)
                    ):
                        projected[key] = list(value)
                if projected:
                    commands.append(projected)
            checkpoint_history = [
                {
                    "checkpoint_id": identifier,
                    "minute": item.simulation_minute,
                    "state_hash": item.state_hash,
                }
                for identifier, item in checkpoints[-100:]
            ]
            document = {
                "schema_version": 1,
                "source": "immutable_checkpoint_snapshot",
                "scenario_id": world.scenario_id,
                "seed": world.seed,
                "requested_minute": requested_minute,
                "captured_minute": world.minute_index,
                "minute_match": requested_minute == world.minute_index,
                "branch": {
                    "branch_id": branch_id,
                    "parent_branch_id": branch.runtime.parent_branch_id,
                    "status": branch.status,
                    "current_minute": current.minute_index,
                    "duration_minutes": current.duration_minutes,
                    "agent_count": len(world.agents),
                    "staff_count": len(world.staff),
                    "facility_count": len(world.facilities),
                    "realized_event_count": len(world.event_log),
                },
                "world": {
                    "schema_version": world.schema_version,
                    "state_hash": _world_hash(world),
                },
                "selected_agent": (
                    {
                        "id": agent.id,
                        "group_id": agent.profile.group_id,
                        "mobility": agent.profile.mobility.value,
                        "zone_id": agent.zone_id,
                        "activity": agent.activity.value,
                        "target_id": agent.target_id,
                        "needs": {
                            "toilet_urgency": agent.needs.toilet_urgency,
                            "hunger": agent.needs.hunger,
                            "hydration_need": agent.needs.hydration_need,
                            "fatigue": agent.needs.fatigue,
                        },
                        "stress": agent.stress,
                        "trust": agent.trust,
                        "support_request_id": agent.support_request_id,
                        "next_decision_at": agent.next_decision_at,
                        "source": (
                            "checkpoint WorldState snapshot; decision trace "
                            "unavailable in live demo"
                        ),
                    }
                    if agent is not None
                    else None
                ),
                "checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "schema_version": checkpoint.schema_version,
                    "state_hash": checkpoint.state_hash,
                    "checkpoint_hash": checkpoint.checkpoint_hash,
                },
                "checkpoint_history": checkpoint_history,
                "checkpoint_history_total": len(checkpoints),
                "metric_snapshot": selected_metric,
                "commands": commands,
            }
            return DemoSnapshotView.capture(document)

    def comparison_view(
        self,
        baseline_branch_id: str,
        candidate_branch_id: str,
    ) -> dict[str, Any]:
        """Return a comparison without recording an operation or mutating either branch."""
        with self._lock:
            self._require_started()
            before = self._require_branch(baseline_branch_id)
            after = self._require_branch(candidate_branch_id)
            if before.status != "completed" or after.status != "completed":
                raise DemoError("both branches must be completed before comparison")
            return self._comparison_payload(
                baseline_branch_id,
                before,
                candidate_branch_id,
                after,
            )

    def story_review(self, baseline_branch_id: str, candidate_branch_id: str) -> dict[str, Any]:
        """Optionally run the evidence-checked Opus review for a completed comparison."""
        with self._lock:
            self._require_started()
            before = self._require_branch(baseline_branch_id)
            after = self._require_branch(candidate_branch_id)
            if before.status != "completed" or after.status != "completed":
                raise DemoError("both branches must be completed before story review")
            comparison = compare_branches(
                before.runtime, after.runtime, maximum_state_differences=0
            )
            comparison_document = comparison.to_dict()
            metric_rows = [asdict(item) for item in after.runtime.metrics.snapshots]
            realized_events = [record.to_dict() for record in after.runtime.world.event_log.records]
            evidence = build_review_evidence(
                metrics=metric_rows,
                realized_events=realized_events,
                assumptions=scenario_assumption_ids(self.scenario),
                comparison_report=comparison_document,
            )
            router = self._accounted_router(branch_id=candidate_branch_id)
            call_count = len(router.records)

        result = generate_evidence_review(evidence, router)
        card = build_narrative_card(
            realized_events=realized_events,
            comparison_report=comparison_document,
            review_result=result,
        )
        records = router.records[call_count:]
        usage = records[-1].to_dict() if records else {}
        return {
            "card": card.to_dict(),
            "review_status": result.status,
            "usage": {
                key: usage[key]
                for key in (
                    "model_id",
                    "api_request_count",
                    "input_tokens",
                    "output_tokens",
                    "estimated_cost_usd",
                    "fallback_used",
                )
                if key in usage
            },
        }

    def reserve_job(self, branch_id: str, job_id: str, target_minute: int) -> int:
        """Reserve one branch for a worker and return its current simulation minute."""
        with self._lock:
            branch = self._require_branch(branch_id)
            if branch_id in self._active_jobs:
                raise DemoError("branch already has an active background job")
            if branch.status not in {"running", "paused"}:
                raise DemoError("branch must be running or paused to start a job")
            if branch.runtime.world.minute_index >= branch.runtime.world.duration_minutes:
                raise DemoError("branch has already reached the scenario end")
            if (
                not isinstance(target_minute, int)
                or isinstance(target_minute, bool)
                or not branch.runtime.world.minute_index < target_minute
                or target_minute > branch.runtime.world.duration_minutes
            ):
                raise DemoError("target_minute must be after the current minute and within the run")
            self._active_jobs[branch_id] = job_id
            branch.status = "running"
            return branch.runtime.world.minute_index

    def advance_job_chunk(self, branch_id: str, job_id: str, steps: int) -> int:
        """Advance an isolated candidate and commit only a fully completed chunk."""
        with self._lock:
            if self._active_jobs.get(branch_id) != job_id:
                raise DemoError("background job no longer owns this branch")
            branch = self._require_branch(branch_id)
            count = min(
                steps,
                branch.runtime.world.duration_minutes - branch.runtime.world.minute_index,
            )
            if count <= 0:
                return branch.runtime.world.minute_index
            candidate = BranchRuntime.from_checkpoint(
                branch.runtime.checkpoint(),
                branch_id=branch.runtime.branch_id,
                parent_branch_id=branch.runtime.parent_branch_id,
            )
            candidate_branch = _Branch(runtime=candidate, status="running")
        # Run the bounded chunk outside the service lock. Readers can poll the last
        # committed state, while mutation routes quickly reject the reserved Branch.
        self._advance_branch(branch_id, candidate_branch, count)
        with self._lock:
            if self._active_jobs.get(branch_id) != job_id:
                raise DemoError("background job no longer owns this branch")
            branch = self._require_branch(branch_id)
            branch.runtime = candidate
            if candidate.world.minute_index >= candidate.world.duration_minutes:
                branch.status = "completed"
            self._save_checkpoint(branch_id, branch, "job-chunk")
            return candidate.world.minute_index

    def finish_job(self, branch_id: str, job_id: str) -> Checkpoint:
        """Release a branch after the last complete chunk and return its save image."""
        with self._lock:
            if self._active_jobs.get(branch_id) != job_id:
                raise DemoError("background job no longer owns this branch")
            branch = self._require_branch(branch_id)
            if branch.runtime.world.minute_index < branch.runtime.world.duration_minutes:
                branch.status = "paused"
            self._active_jobs.pop(branch_id, None)
            return branch.checkpoints[next(reversed(branch.checkpoints))]

    def checkpoint_for_job(self, branch_id: str) -> Checkpoint:
        with self._lock:
            branch = self._require_branch(branch_id)
            if branch.checkpoints:
                return branch.checkpoints[next(reversed(branch.checkpoints))]
            return branch.runtime.checkpoint()

    def restore_job_branch(self, branch_id: str, checkpoint: Checkpoint) -> None:
        """Restore the last atomically saved job chunk as a paused local branch."""
        with self._lock:
            if checkpoint.scenario_id != self.scenario.id:
                raise DemoError("saved job belongs to a different demo scenario")
            if branch_id in self.branches:
                return
            runtime_metadata = checkpoint.runtime_state.get("branch", {})
            runtime = BranchRuntime.from_checkpoint(
                checkpoint,
                branch_id=branch_id,
                parent_branch_id=runtime_metadata.get("parent_branch_id"),
            )
            checkpoint_id = checkpoint.runtime_state.get("demo_checkpoint_id")
            if not isinstance(checkpoint_id, str):
                checkpoint_id = f"{branch_id}-cp-0000"
            match = re.search(r"-cp-(\d+)$", checkpoint_id)
            next_checkpoint = int(match.group(1)) + 1 if match else 1
            self.branches[branch_id] = _Branch(
                runtime=runtime,
                status=(
                    "completed"
                    if runtime.world.minute_index >= runtime.world.duration_minutes
                    else "paused"
                ),
                checkpoints={checkpoint_id: checkpoint},
                next_checkpoint=next_checkpoint,
            )
            self.started = True

    def _apply(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action == "state":
            self._fields(body, set())
            return self.state()
        if action == "start":
            self._fields(body, set())
            if self.started:
                raise DemoError("demo has already started; reload the page to reset it")
            world = WorldState.from_scenario(self.scenario)
            if self.scenario_run_manifest is None:
                self.scenario_run_manifest = {
                    "schema_version": 1,
                    "run_id": f"live-{uuid.uuid4().hex}",
                    "scenario_id": self.scenario.id,
                    "scenario_hash": scenario_hash(self.scenario),
                    "scenario_family_hash": scenario_family_hash(self.scenario),
                    "seed": self.scenario.seed,
                    "duration_minutes": self.scenario.duration_minutes,
                    "agent_count": len(self.scenario.profiles),
                    "source_kind": "packaged_or_local_scenario",
                    "execution_mode": "live_compute",
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "frozen": True,
                }
            root = BranchRuntime.from_world(world, branch_id="baseline")
            branch = _Branch(runtime=root, status="running")
            self.branches[root.branch_id] = branch
            self.started = True
            self._save_checkpoint(root.branch_id, branch, "initial")
            return self.state()

        self._require_started()
        if action == "compare":
            self._fields(body, {"baseline_branch_id", "candidate_branch_id"})
            before = self._require_branch(body["baseline_branch_id"])
            after = self._require_branch(body["candidate_branch_id"])
            if before.status != "completed" or after.status != "completed":
                raise DemoError("both branches must be completed before comparison")
            return self._comparison_payload(
                body["baseline_branch_id"],
                before,
                body["candidate_branch_id"],
                after,
            )

        if action == "compare-set":
            self._fields(body, {"baseline_branch_id", "candidate_branch_ids"})
            return self._comparison_set_payload(
                body.get("baseline_branch_id"), body.get("candidate_branch_ids")
            )

        if action == "branch-plan":
            self._fields(body, {"source_branch_id", "approved_plan", "idempotency_key"})
            return self._apply_approved_plan(body)

        if action == "multi-plan":
            self._fields(body, {"source_branch_id", "approved_plan", "idempotency_key"})
            return self._apply_approved_multi_plan(body)

        if action == "layout-branch":
            self._fields(body, {"source_branch_id", "commands", "idempotency_key"})
            return self._apply_layout_branch(body)

        action_fields = {
            "advance": {"branch_id", "steps"},
            "pause": {"branch_id"},
            "resume": {"branch_id"},
            "checkpoint": {"branch_id"},
            "rewind": {"branch_id", "checkpoint_id"},
            "fork": {"branch_id", "new_branch_id"},
            "command": {"branch_id", "command"},
            "commands": {"branch_id", "commands"},
        }.get(action)
        if action_fields is None:
            raise DemoError("unknown demo action")
        self._fields(body, action_fields)
        branch_id = body.get("branch_id")
        branch = self._require_branch(branch_id)
        if action == "advance":
            self._require_status(branch, "running")
            steps = body.get("steps")
            if (
                not isinstance(steps, int)
                or isinstance(steps, bool)
                or not 1 <= steps <= _MAX_ADVANCE
            ):
                raise DemoError(f"steps must be an integer from 1 to {_MAX_ADVANCE}")
            count = min(
                steps, branch.runtime.world.duration_minutes - branch.runtime.world.minute_index
            )
            if count:
                self._advance_branch(branch_id, branch, count)
            if branch.runtime.world.minute_index >= branch.runtime.world.duration_minutes:
                branch.status = "completed"
                self._save_checkpoint(branch_id, branch, "completed")
            return self.state()
        if action == "pause":
            self._require_status(branch, "running")
            branch.status = "paused"
            self._save_checkpoint(branch_id, branch, "paused")
            return self.state()
        if action == "resume":
            self._require_status(branch, "paused")
            branch.status = "running"
            return self.state()
        if action == "checkpoint":
            self._require_status(branch, "paused")
            self._save_checkpoint(branch_id, branch, "manual")
            return self.state()
        if action == "rewind":
            self._require_status(branch, "paused")
            checkpoint_id = body.get("checkpoint_id")
            if checkpoint_id is None:
                checkpoint_id = next(reversed(branch.checkpoints))
            checkpoint = branch.checkpoints.get(checkpoint_id)
            if checkpoint is None:
                raise DemoError("unknown checkpoint_id for this branch")
            restored = BranchRuntime.from_checkpoint(
                checkpoint,
                branch_id=branch_id,
                parent_branch_id=branch.runtime.parent_branch_id,
            )
            branch.runtime = restored
            branch.status = "paused"
            return self.state()
        if action == "fork":
            self._require_status(branch, "paused")
            new_id = body.get("new_branch_id")
            if not isinstance(new_id, str) or not new_id:
                raise DemoError("new_branch_id is required")
            if new_id in self.branches:
                raise DemoError(f"branch {new_id!r} already exists")
            child = branch.runtime.fork(new_id)
            child_branch = _Branch(runtime=child, status="paused")
            self.branches[new_id] = child_branch
            self._save_checkpoint(new_id, child_branch, "fork")
            return self.state()
        if action in {"command", "commands"}:
            self._require_status(branch, "paused")
            raw_commands = [body.get("command")] if action == "command" else body.get("commands")
            if not isinstance(raw_commands, list) or not 1 <= len(raw_commands) <= 10:
                raise DemoError("commands must contain between one and ten typed interventions")
            parsed_commands = [command_from_dict(item) for item in raw_commands]
            candidate = BranchRuntime.from_checkpoint(
                branch.runtime.checkpoint(),
                branch_id=branch.runtime.branch_id,
                parent_branch_id=branch.runtime.parent_branch_id,
            )
            for parsed in parsed_commands:
                candidate.apply(parsed)
            branch.runtime = candidate
            self._save_checkpoint(branch_id, branch, "intervention")
            return self.state()
        raise DemoError("unknown demo action")

    def _apply_layout_branch(self, body: dict[str, Any]) -> dict[str, Any]:
        """Fork and apply typed route/facility edits atomically from a paused source."""
        source_branch_id = body.get("source_branch_id")
        idempotency_key = body.get("idempotency_key")
        raw_commands = body.get("commands")
        if not isinstance(source_branch_id, str):
            raise DemoError("source_branch_id is required")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise DemoError("idempotency_key must be a stable identifier of at most 128 characters")
        if not isinstance(raw_commands, list) or not 1 <= len(raw_commands) <= 10:
            raise DemoError("commands must contain between one and ten layout edits")
        commands = [command_from_dict(item) for item in raw_commands]
        if any(
            not isinstance(command, (SetZoneEdgeCommand, RelocateFacilityCommand))
            for command in commands
        ):
            raise DemoError("layout branches accept only typed zone-edge or facility changes")

        command_document = [command.to_dict() for command in commands]
        command_hash = hashlib.sha256(
            json.dumps(command_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        prior = self.layout_branch_receipts.get(key_hash)
        if prior is not None:
            if (
                prior["source_branch_id"] != source_branch_id
                or prior["command_hash"] != command_hash
            ):
                raise DemoError("idempotency_key was already used for a different route edit")
            return {**prior["result"], "idempotent_replay": True}

        source = self._require_branch(source_branch_id)
        if source.status != "paused":
            raise DemoConflictError("source branch must be paused before applying route edits")
        if source_branch_id in self._active_jobs:
            raise DemoConflictError("source branch has an active background job")
        child_id = f"layout-{key_hash[:16]}"
        if child_id in self.branches:
            raise DemoError("layout branch ID already exists without a matching receipt")

        source_hash = _world_hash(source.runtime.world)
        candidate = source.runtime.fork(child_id)
        for command in commands:
            candidate.apply(command)
        child = _Branch(runtime=candidate, status="paused")
        self.branches[child_id] = child
        checkpoint_id = self._save_checkpoint(child_id, child, "layout-intervention")
        result = {
            "branch_id": child_id,
            "parent_branch_id": source_branch_id,
            "source_state_hash": source_hash,
            "state_hash": _world_hash(candidate.world),
            "command_hash": command_hash,
            "commands": command_document,
            "checkpoint_id": checkpoint_id,
            "idempotent_replay": False,
        }
        self.layout_branch_receipts[key_hash] = {
            "source_branch_id": source_branch_id,
            "command_hash": command_hash,
            "result": result,
        }
        return result

    def _apply_approved_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        source_branch_id = body.get("source_branch_id")
        if not isinstance(source_branch_id, str):
            raise DemoError("source_branch_id is required")
        source = self._require_branch(source_branch_id)

        raw_plan = body.get("approved_plan")
        if not isinstance(raw_plan, dict):
            raise DemoError("approved_plan must be an explicit confirmation receipt")
        approved = ApprovedPlan.model_validate(raw_plan)
        plan = approved.draft_plan
        if plan.branch_id != source_branch_id:
            raise DemoError("approved plan branch_id does not match source_branch_id")
        if plan.fork_at_minute >= source.runtime.world.duration_minutes:
            raise DemoError("fork_at_minute must be before the scenario end")

        idempotency_key = body.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise DemoError("idempotency_key must be a stable identifier of at most 128 characters")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        child_id = _plan_branch_id(source_branch_id, key_hash)
        existing = self.branches.get(child_id)
        if existing is not None:
            return self._existing_plan_result(
                existing,
                source_branch_id=source_branch_id,
                key_hash=key_hash,
                approval_hash=approved.approval_hash,
            )
        if source.status == "running":
            raise DemoError("source branch must be paused before approving a plan")

        source_minute = source.runtime.world.minute_index
        latest_fork_minute = min(source_minute, plan.fork_at_minute)
        candidates = [
            (checkpoint.simulation_minute, index, checkpoint_id, checkpoint)
            for index, (checkpoint_id, checkpoint) in enumerate(source.checkpoints.items())
            if checkpoint.simulation_minute <= latest_fork_minute
        ]
        if not candidates:
            raise DemoError(
                "no saved checkpoint or initial state is available before fork_at_minute"
            )
        _, _, source_checkpoint_id, source_checkpoint = max(candidates)
        apply_minute = min(source_minute, plan.fork_at_minute)

        try:
            runtime = BranchRuntime.from_checkpoint(
                source_checkpoint,
                branch_id=child_id,
                parent_branch_id=source_branch_id,
            )
            child = _Branch(runtime=runtime, status="paused")
            origin_checkpoint_id = self._save_checkpoint(child_id, child, "plan-origin")
            replay_to_plan_minute = apply_minute - runtime.world.minute_index
            if replay_to_plan_minute < 0:
                raise DemoError("selected checkpoint is ahead of the plan application minute")
            if replay_to_plan_minute:
                self._advance_branch(child_id, child, replay_to_plan_minute)

            # Reserve plan event IDs only after replaying the shared history. This keeps
            # source and child event identities identical up to the selected fork minute.
            receipt = runtime.apply_effect_program(plan)
            runtime.runtime_state["approved_plan_v1"] = {
                "schema_version": 1,
                "source_branch_id": source_branch_id,
                "source_checkpoint_id": source_checkpoint_id,
                "idempotency_key_hash": key_hash,
                "approval_hash": approved.approval_hash,
                "program_id": receipt.program_id,
                "program_hash": receipt.program_hash,
                "scheduled_event_ids": list(receipt.event_ids),
                "fork_at_minute": plan.fork_at_minute,
                "source_text_hash": hashlib.sha256(plan.source_text.encode("utf-8")).hexdigest(),
                "compiler_provenance": plan.compiler_provenance.model_dump(mode="json"),
                "assumptions": [item.model_dump(mode="json") for item in plan.assumptions],
                "effects": [
                    {
                        "effect_id": effect.effect_id,
                        "op": effect.op,
                        "target_kind": effect.target_selector.kind,
                        "target_id": effect.target_selector.target_id,
                        "start_minute": effect.start_minute,
                        "end_minute": effect.end_minute,
                    }
                    for effect in plan.effects
                ],
            }
            runtime.command_log.append(
                {
                    "command": "approved_effect_program",
                    "approval_hash": approved.approval_hash,
                    "idempotency_key_hash": key_hash,
                    "source_branch_id": source_branch_id,
                    "source_checkpoint_id": source_checkpoint_id,
                    "fork_at_minute": plan.fork_at_minute,
                    "event_ids": list(receipt.event_ids),
                }
            )
            scheduled_checkpoint_id = self._save_checkpoint(child_id, child, "plan-scheduled")
            replay_steps = source_minute - runtime.world.minute_index
            if replay_steps:
                self._advance_branch(child_id, child, replay_steps)
            child.status = (
                "completed"
                if runtime.world.minute_index >= runtime.world.duration_minutes
                else "paused"
            )
        except (BranchError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, DemoError):
                raise
            raise DemoError(str(exc)) from exc

        final_checkpoint_id = f"{child_id}-cp-{child.next_checkpoint:04d}"
        result = {
            "branch_id": child_id,
            "parent_branch_id": source_branch_id,
            "source_checkpoint_id": source_checkpoint_id,
            "checkpoint_id": origin_checkpoint_id,
            "scheduled_checkpoint_id": scheduled_checkpoint_id,
            "final_checkpoint_id": final_checkpoint_id,
            "fork_at_minute": plan.fork_at_minute,
            "current_minute": runtime.world.minute_index,
            "plan_hash": approved.approval_hash,
            "receipts": [
                {
                    "program_id": receipt.program_id,
                    "program_hash": receipt.program_hash,
                    "event_ids": list(receipt.event_ids),
                }
            ],
            "lineage": {
                "parent_branch_id": source_branch_id,
                "source_checkpoint_id": source_checkpoint_id,
                "fork_at_minute": plan.fork_at_minute,
            },
            "idempotent_replay": False,
        }
        runtime.runtime_state["approved_plan_v1"]["result"] = result
        # Persist the idempotency receipt in the final checkpoint without touching the source.
        saved_checkpoint_id = self._save_checkpoint(child_id, child, "plan-applied")
        if saved_checkpoint_id != final_checkpoint_id:
            raise DemoError("approved-plan checkpoint sequence changed unexpectedly")
        self.branches[child_id] = child
        return result

    def _apply_approved_multi_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        source_branch_id = body.get("source_branch_id")
        idempotency_key = body.get("idempotency_key")
        if not isinstance(source_branch_id, str):
            raise DemoError("source_branch_id is required")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise DemoError("idempotency_key must be a stable identifier of at most 128 characters")
        try:
            approved = MultiPlanApproval.model_validate(body.get("approved_plan"))
        except ValueError as exc:
            raise DemoError(str(exc)) from exc
        multi_plan = approved.multi_plan
        if multi_plan.branch_origin != source_branch_id:
            raise DemoError("approved multi-plan origin does not match source_branch_id")

        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        multi_plan_id = f"multi-{key_hash[:24]}"
        child_ids = {
            item.variant_id: _multi_plan_branch_id(source_branch_id, key_hash, item.variant_id)
            for item in multi_plan.branches
        }
        key_receipts = [
            branch.runtime.runtime_state.get("multi_plan_v1")
            for branch in self.branches.values()
            if isinstance(branch.runtime.runtime_state.get("multi_plan_v1"), dict)
            and branch.runtime.runtime_state["multi_plan_v1"].get("source_branch_id")
            == source_branch_id
            and branch.runtime.runtime_state["multi_plan_v1"].get("idempotency_key_hash")
            == key_hash
        ]
        if key_receipts:
            receipt = key_receipts[0]
            if any(item.get("approval_hash") != approved.approval_hash for item in key_receipts):
                raise DemoError("idempotency_key was already used for a different multi-plan")
            result = receipt.get("result")
            if not isinstance(result, dict):
                raise DemoError("saved multi-plan receipt is incomplete")
            expected_ids = set(child_ids.values())
            if {item.get("branch_id") for item in result.get("branches", [])} != expected_ids:
                raise DemoError("saved multi-plan receipt does not match its branch set")
            if not expected_ids <= set(self.branches):
                raise DemoError("multi-plan transaction left an incomplete branch set")
            return {**result, "idempotent_replay": True}

        existing = [branch_id in self.branches for branch_id in child_ids.values()]
        if any(existing):
            if not all(existing):
                raise DemoError("multi-plan transaction left an incomplete branch set")
            first_branch = self.branches[next(iter(child_ids.values()))]
            receipt = first_branch.runtime.runtime_state.get("multi_plan_v1")
            if not isinstance(receipt, dict):
                raise DemoError("deterministic multi-plan branch ID is already in use")
            if (
                receipt.get("source_branch_id") != source_branch_id
                or receipt.get("idempotency_key_hash") != key_hash
                or receipt.get("approval_hash") != approved.approval_hash
                or receipt.get("multi_plan_id") != multi_plan_id
            ):
                raise DemoError("idempotency_key was already used for a different multi-plan")
            result = receipt.get("result")
            if not isinstance(result, dict):
                raise DemoError("saved multi-plan receipt is incomplete")
            expected_ids = set(child_ids.values())
            if {item.get("branch_id") for item in result.get("branches", [])} != expected_ids:
                raise DemoError("saved multi-plan receipt does not match its branch set")
            return {**result, "idempotent_replay": True}

        validation, origin, source_minute, origin_checkpoint_id = self._preflight_multi_plan(
            multi_plan
        )
        if not validation["valid"]:
            issues = "; ".join(validation.get("issues", [])[:8])
            if validation.get("stale"):
                raise DemoConflictError(issues or "source Branch changed after preview")
            raise DemoError("multi-plan is not valid: " + issues)
        if origin is None or origin_checkpoint_id is None:
            raise DemoError("multi-plan origin could not be restored")
        origin_checkpoint = origin.checkpoint()

        staged: dict[str, _Branch] = {}
        branch_results: list[dict[str, Any]] = []
        for alternative in multi_plan.branches:
            child_id = child_ids[alternative.variant_id]
            runtime = BranchRuntime.from_checkpoint(
                origin_checkpoint,
                branch_id=child_id,
                parent_branch_id=source_branch_id,
            )
            child = _Branch(runtime=runtime, status="paused")
            origin_child_checkpoint_id = self._save_checkpoint(child_id, child, "multi-plan-origin")
            try:
                scheduled = runtime.apply_effect_program(alternative.plan)
            except (BranchError, ValueError) as exc:
                raise DemoError(str(exc)) from exc
            metadata = {
                "schema_version": 1,
                "multi_plan_id": multi_plan_id,
                "source_branch_id": source_branch_id,
                "source_state_hash": multi_plan.source_state_hash,
                "idempotency_key_hash": key_hash,
                "approval_hash": approved.approval_hash,
                "variant_id": alternative.variant_id,
                "label": alternative.label,
                "program_id": scheduled.program_id,
                "program_hash": scheduled.program_hash,
                "scheduled_event_ids": list(scheduled.event_ids),
                "source_checkpoint_id": origin_checkpoint_id,
                "source_checkpoint_hash": validation["source_checkpoint_hash"],
                "origin_checkpoint_hash": validation["origin_checkpoint_hash"],
                "origin_world_hash": validation["origin_world_hash"],
                "fork_at_minute": multi_plan.fork_at_minute,
                "model_versions_hash": validation["model_versions_hash"],
                "source_text_hash": hashlib.sha256(
                    alternative.plan.source_text.encode("utf-8")
                ).hexdigest(),
            }
            runtime.runtime_state["multi_plan_v1"] = metadata
            runtime.command_log.append(
                {
                    "command": "approved_multi_plan_alternative",
                    "multi_plan_id": multi_plan_id,
                    "variant_id": alternative.variant_id,
                    "source_branch_id": source_branch_id,
                    "approval_hash": approved.approval_hash,
                    "idempotency_key_hash": key_hash,
                    "source_checkpoint_id": origin_checkpoint_id,
                    "fork_at_minute": multi_plan.fork_at_minute,
                    "event_ids": list(scheduled.event_ids),
                }
            )
            scheduled_checkpoint_id = self._save_checkpoint(child_id, child, "multi-plan-scheduled")
            replay_steps = source_minute - runtime.world.minute_index
            if replay_steps < 0:
                raise DemoError("multi-plan origin is ahead of the source Branch")
            if replay_steps:
                self._advance_branch(child_id, child, replay_steps)
            child.status = (
                "completed"
                if runtime.world.minute_index >= runtime.world.duration_minutes
                else "paused"
            )
            final_checkpoint_id = f"{child_id}-cp-{child.next_checkpoint:04d}"
            branch_results.append(
                {
                    "variant_id": alternative.variant_id,
                    "label": alternative.label,
                    "branch_id": child_id,
                    "parent_branch_id": source_branch_id,
                    "origin_checkpoint_id": origin_child_checkpoint_id,
                    "scheduled_checkpoint_id": scheduled_checkpoint_id,
                    "final_checkpoint_id": final_checkpoint_id,
                    "program_id": scheduled.program_id,
                    "program_hash": scheduled.program_hash,
                    "event_ids": list(scheduled.event_ids),
                    "status": child.status,
                    "current_minute": runtime.world.minute_index,
                }
            )
            staged[child_id] = child

        result = {
            "multi_plan_id": multi_plan_id,
            "source_branch_id": source_branch_id,
            "source_state_hash": multi_plan.source_state_hash,
            "source_checkpoint_id": origin_checkpoint_id,
            "source_checkpoint_hash": validation["source_checkpoint_hash"],
            "origin_checkpoint_hash": validation["origin_checkpoint_hash"],
            "origin_world_hash": validation["origin_world_hash"],
            "model_versions_hash": validation["model_versions_hash"],
            "fork_at_minute": multi_plan.fork_at_minute,
            "approval_hash": approved.approval_hash,
            "branches": branch_results,
            "idempotent_replay": False,
        }
        for branch_id, child in staged.items():
            child.runtime.runtime_state["multi_plan_v1"]["result"] = result
            saved_checkpoint_id = self._save_checkpoint(branch_id, child, "multi-plan-applied")
            expected_checkpoint_id = next(
                item["final_checkpoint_id"]
                for item in branch_results
                if item["branch_id"] == branch_id
            )
            if saved_checkpoint_id != expected_checkpoint_id:
                raise DemoError("multi-plan final checkpoint sequence changed unexpectedly")

        # Commit only after every staged alternative has applied, replayed and checkpointed.
        self.branches.update(staged)
        return result

    def _advance_branch(self, branch_id: str, branch: _Branch, steps: int) -> None:
        sink = _MetricSink(branch.runtime.metrics)
        controller = OfflineSimulationController(branch.runtime.world, sink)
        TickEngine(
            branch.runtime.world,
            phase_handler=controller,
            rng=CounterRNG(branch.runtime.world.seed),
        ).run(steps=steps)

    def _existing_plan_result(
        self,
        branch: _Branch,
        *,
        source_branch_id: str,
        key_hash: str,
        approval_hash: str,
    ) -> dict[str, Any]:
        receipt = branch.runtime.runtime_state.get("approved_plan_v1")
        if not isinstance(receipt, dict):
            raise DemoError("deterministic plan branch ID is already in use")
        if (
            receipt.get("source_branch_id") != source_branch_id
            or receipt.get("idempotency_key_hash") != key_hash
            or receipt.get("approval_hash") != approval_hash
        ):
            raise DemoError("idempotency_key was already used for a different approved plan")
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise DemoError("saved approved-plan receipt is incomplete")
        return {**result, "idempotent_replay": True}

    def _save_checkpoint(self, branch_id: str, branch: _Branch, reason: str) -> str:
        checkpoint_id = f"{branch_id}-cp-{branch.next_checkpoint:04d}"
        branch.next_checkpoint += 1
        runtime = branch.runtime.checkpoint()
        runtime_state = dict(runtime.runtime_state)
        runtime_state["demo_checkpoint_reason"] = reason
        runtime_state["demo_checkpoint_id"] = checkpoint_id
        # Capture again so the checkpoint hash covers the small UI metadata addition.
        runtime = Checkpoint.capture(
            branch.runtime.world,
            metrics=branch.runtime.metrics,
            runtime_state=runtime_state,
            model_versions=runtime.model_versions,
        )
        branch.checkpoints[checkpoint_id] = runtime
        return checkpoint_id

    def _record(
        self,
        action: str,
        body: dict[str, Any],
        *,
        accepted: bool,
        before_hash: str | None,
        after_hash: str | None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        source_branch_id = body.get("source_branch_id")
        record = {
            "sequence": len(self.operations),
            "action": action,
            "branch_id": body.get("branch_id", source_branch_id),
            "minute": self._minute(body.get("branch_id", source_branch_id)),
            "accepted": accepted,
            "state_hash_before": before_hash,
            "state_hash_after": after_hash,
        }
        if action == "command" and isinstance(body.get("command"), dict):
            record["command"] = body["command"]
        elif action == "commands" and isinstance(body.get("commands"), list):
            record["commands"] = body["commands"]
        if action == "fork":
            record["new_branch_id"] = body.get("new_branch_id")
        if action == "branch-plan":
            record["source_branch_id"] = source_branch_id
            if result is not None:
                record["new_branch_id"] = result.get("branch_id")
                record["plan_hash"] = result.get("plan_hash")
                record["idempotent_replay"] = result.get("idempotent_replay", False)
        if action == "multi-plan":
            record["source_branch_id"] = source_branch_id
            if result is not None:
                record["multi_plan_id"] = result.get("multi_plan_id")
                record["branch_ids"] = [item["branch_id"] for item in result.get("branches", [])]
                record["approval_hash"] = result.get("approval_hash")
                record["idempotent_replay"] = result.get("idempotent_replay", False)
        if action == "layout-branch":
            record["source_branch_id"] = source_branch_id
            if result is not None:
                record["new_branch_id"] = result.get("branch_id")
                record["command_hash"] = result.get("command_hash")
                record["idempotent_replay"] = result.get("idempotent_replay", False)
        if error:
            record["error"] = error[:300]
        self.operations.append(record)
        if len(self.operations) > _MAX_OPERATIONS:
            del self.operations[: len(self.operations) - _MAX_OPERATIONS]

    def _requested_hash(self, branch_id: Any) -> str | None:
        if not isinstance(branch_id, str) or branch_id not in self.branches:
            return None
        return _world_hash(self.branches[branch_id].runtime.world)

    def _minute(self, branch_id: Any) -> int | None:
        if not isinstance(branch_id, str) or branch_id not in self.branches:
            return None
        return self.branches[branch_id].runtime.world.minute_index

    def _branch_summary(self, branch_id: str, branch: _Branch) -> dict[str, Any]:
        world = branch.runtime.world
        route_config = world.model_configs.get("layout_routing", {})
        route_capacities = route_config.get("max_agents_in_transit_by_edge", {})
        summary = {
            "branch_id": branch_id,
            "parent_branch_id": branch.runtime.parent_branch_id,
            "status": branch.status,
            "minute": world.minute_index,
            "duration_minutes": world.duration_minutes,
            "agent_count": len(world.agents),
            "staff_count": len(world.staff),
            "facility_count": len(world.facilities),
            "event_count": len(world.event_log),
            "state_hash": _world_hash(world),
            "checkpoint_ids": list(branch.checkpoints),
            "latest_checkpoint_id": next(reversed(branch.checkpoints), None),
            "commands": branch.runtime.command_log[-20:],
            "route_edges": [
                {
                    "id": edge.id,
                    "from_zone_id": edge.from_zone_id,
                    "to_zone_id": edge.to_zone_id,
                    "travel_minutes": edge.travel_minutes,
                    "closed": edge.closed,
                    "max_agents_in_transit": route_capacities.get(edge.id),
                }
                for edge in sorted(world.zone_edges.values(), key=lambda item: item.id)
            ],
            "routing_metrics": _routing_metrics(world),
            "facility_locations": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "zone_id": item.zone_id,
                    "capacity": item.capacity,
                    "working_units": item.working_units,
                }
                for item in sorted(world.facilities.values(), key=lambda item: item.id)
            ],
        }
        approved_plan = branch.runtime.runtime_state.get("approved_plan_v1")
        if isinstance(approved_plan, dict):
            summary["approved_plan"] = {
                key: approved_plan[key]
                for key in (
                    "source_branch_id",
                    "source_checkpoint_id",
                    "approval_hash",
                    "program_id",
                    "program_hash",
                    "scheduled_event_ids",
                    "fork_at_minute",
                    "source_text_hash",
                    "compiler_provenance",
                    "assumptions",
                    "effects",
                )
                if key in approved_plan
            }
        multi_plan = branch.runtime.runtime_state.get("multi_plan_v1")
        if isinstance(multi_plan, dict):
            summary["multi_plan"] = {
                key: multi_plan[key]
                for key in (
                    "multi_plan_id",
                    "source_branch_id",
                    "variant_id",
                    "label",
                    "source_checkpoint_id",
                    "source_checkpoint_hash",
                    "origin_checkpoint_hash",
                    "fork_at_minute",
                    "model_versions_hash",
                    "program_hash",
                )
                if key in multi_plan
            }
        return summary

    @staticmethod
    def _comparison_payload(
        baseline_branch_id: str,
        before: _Branch,
        candidate_branch_id: str,
        after: _Branch,
    ) -> dict[str, Any]:
        report = compare_branches(before.runtime, after.runtime, maximum_state_differences=0)
        comparison_document = report.to_dict()
        realized_events = [record.to_dict() for record in after.runtime.world.event_log.records]
        story_card = build_narrative_card(
            realized_events=realized_events,
            comparison_report=comparison_document,
        )
        before_routing = _routing_metrics(before.runtime.world)
        after_routing = _routing_metrics(after.runtime.world)
        routing_metric_names = (
            "travel_trip_count",
            "travel_minutes_total",
            "capacity_wait_observation_count",
            "capacity_wait_unique_agent_count",
        )
        return {
            "scenario_id": report.scenario_id,
            "seed": report.seed,
            "baseline_branch_id": baseline_branch_id,
            "candidate_branch_id": candidate_branch_id,
            "first_divergence_minute": report.first_divergence_minute,
            "changed_metrics_at_first_divergence": _aggregate_metric_differences(
                report.changed_metrics_at_first_divergence
            ),
            "final_metric_differences": _aggregate_metric_differences(
                report.final_metric_differences
            ),
            "no_op_equivalent": report.no_op_equivalent,
            "event_counts": {
                "baseline": len(before.runtime.world.event_log),
                "candidate": len(after.runtime.world.event_log),
            },
            "routing_metrics": {
                name: {
                    "baseline": before_routing[name],
                    "candidate": after_routing[name],
                    "delta": after_routing[name] - before_routing[name],
                }
                for name in routing_metric_names
            },
            "routing_evidence": {
                "baseline": before_routing["evidence"],
                "candidate": after_routing["evidence"],
            },
            "interpretation": report.interpretation,
            "story_card": story_card.to_dict(),
        }

    def _comparison_set_payload(
        self, baseline_branch_id: Any, candidate_branch_ids: Any
    ) -> dict[str, Any]:
        """Compare two or more sibling alternatives from one frozen origin."""
        if not isinstance(baseline_branch_id, str):
            raise DemoError("baseline_branch_id must be a string")
        if (
            not isinstance(candidate_branch_ids, list)
            or not 2 <= len(candidate_branch_ids) <= 8
            or any(not isinstance(item, str) for item in candidate_branch_ids)
            or len(set(candidate_branch_ids)) != len(candidate_branch_ids)
            or baseline_branch_id in candidate_branch_ids
        ):
            raise DemoError("compare-set needs two to eight distinct candidate branch IDs")
        baseline = self._require_branch(baseline_branch_id)
        if baseline.status != "completed":
            raise DemoError("the common source Branch must be completed before comparison")
        candidates = [self._require_branch(item) for item in candidate_branch_ids]
        if any(candidate.status != "completed" for candidate in candidates):
            raise DemoError("all alternative branches must be completed before comparison")
        if any(
            candidate.runtime.parent_branch_id != baseline_branch_id for candidate in candidates
        ):
            raise DemoError("all alternatives must be direct children of the selected baseline")

        metadata = [
            candidate.runtime.runtime_state.get("multi_plan_v1") for candidate in candidates
        ]
        if any(not isinstance(item, dict) for item in metadata):
            raise DemoError("candidate branches must belong to an approved multi-plan set")
        first = metadata[0]
        compatibility_fields = (
            "multi_plan_id",
            "source_branch_id",
            "source_state_hash",
            "source_checkpoint_id",
            "source_checkpoint_hash",
            "origin_checkpoint_hash",
            "origin_world_hash",
            "fork_at_minute",
            "model_versions_hash",
        )
        if any(
            any(item.get(key) != first.get(key) for key in compatibility_fields)
            for item in metadata[1:]
        ):
            raise DemoError("candidate branches do not share one plan origin/checkpoint/model")
        if first.get("source_branch_id") != baseline_branch_id:
            raise DemoError("selected baseline is not the source of this multi-plan set")
        baseline_model_hash = _json_hash(
            {
                "model_versions": baseline.runtime.checkpoint().model_versions,
                "model_configs": baseline.runtime.world.model_configs,
            }
        )
        if baseline_model_hash != first.get("model_versions_hash"):
            raise DemoError("baseline model version or configuration differs from the plan origin")
        if any(
            candidate.runtime.world.scenario_id != baseline.runtime.world.scenario_id
            or candidate.runtime.world.seed != baseline.runtime.world.seed
            or candidate.runtime.world.minute_index != baseline.runtime.world.minute_index
            for candidate in candidates
        ):
            raise DemoError("branches must use the same scenario, seed, and comparison minute")

        comparisons = [
            self._comparison_payload(
                baseline_branch_id,
                baseline,
                candidate_id,
                candidate,
            )
            for candidate_id, candidate in zip(candidate_branch_ids, candidates, strict=True)
        ]
        return {
            "schema_version": 1,
            "multi_plan_id": first["multi_plan_id"],
            "baseline_branch_id": baseline_branch_id,
            "candidate_branch_ids": list(candidate_branch_ids),
            "compatibility": {
                "scenario_id": baseline.runtime.world.scenario_id,
                "seed": baseline.runtime.world.seed,
                "comparison_minute": baseline.runtime.world.minute_index,
                "source_checkpoint_id": first["source_checkpoint_id"],
                "source_checkpoint_hash": first["source_checkpoint_hash"],
                "origin_checkpoint_hash": first["origin_checkpoint_hash"],
                "fork_at_minute": first["fork_at_minute"],
                "model_versions_hash": first["model_versions_hash"],
                "matched": True,
            },
            "comparisons": comparisons,
        }

    def _require_started(self) -> None:
        if not self.started:
            raise DemoError("start the demo first")

    def _require_branch(self, branch_id: Any) -> _Branch:
        if not isinstance(branch_id, str) or branch_id not in self.branches:
            raise DemoError("unknown branch_id")
        return self.branches[branch_id]

    @staticmethod
    def _require_status(branch: _Branch, expected: str) -> None:
        if branch.status != expected:
            raise DemoError(f"branch must be {expected}; current status is {branch.status}")

    @staticmethod
    def _fields(body: dict[str, Any], allowed: set[str]) -> None:
        unknown = set(body) - allowed
        missing = {"branch_id"} if "branch_id" in allowed and "branch_id" not in body else set()
        if unknown or missing:
            raise DemoError("request fields are missing or unsupported")


def _world_hash(world: WorldState) -> str:
    return hashlib.sha256(world.canonical_json().encode("utf-8")).hexdigest()


def _json_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_branch_id(source_branch_id: str, idempotency_key_hash: str) -> str:
    identity = hashlib.sha256(
        f"approved-effect-plan-v1:{source_branch_id}:{idempotency_key_hash}".encode("utf-8")
    ).hexdigest()[:32]
    return f"plan-{identity}"


def _multi_plan_branch_id(source_branch_id: str, idempotency_key_hash: str, variant_id: str) -> str:
    identity = hashlib.sha256(
        f"approved-multi-plan-v1:{source_branch_id}:{idempotency_key_hash}:{variant_id}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    return f"multi-{identity}"


def _aggregate_metric_differences(
    differences: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Expose only aggregate KPI deltas, never the source-ID lists carried internally."""
    return {key: value for key, value in differences.items() if key in _COMPARISON_METRICS}


def load_demo_scenario(path: str | Path | None = None) -> ScenarioSpec:
    """Load the packaged fixture or a local test scenario without any network access."""
    return ScenarioSpec.load(Path(path) if path is not None else _DEFAULT_SCENARIO)


def scenario_json(scenario: ScenarioSpec) -> str:
    """Stable local-only representation for test fixtures and offline diagnostics."""
    return json.dumps(scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
