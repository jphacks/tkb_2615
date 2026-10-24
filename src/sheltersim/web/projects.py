"""Persistent project HTTP adapter and bounded resumable cloud background runs."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sheltersim.core.world import WorldState
from sheltersim.events.effects import EffectProgram
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.experiments.validation import evidence_context
from sheltersim.llm.config import LLMSettings,LLMMode
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.accounting import accounted_router, usage_limits
from sheltersim.storage.usage import UsageLedger
from sheltersim.storage.telemetry import RunTelemetryStore
from sheltersim.storage.projects import ProjectStore, StorageError, digest

try:
    import resource as _resource
except ImportError:  # pragma: no cover - unavailable on Windows
    _resource = None


def fields(document: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    if set(document) - required - (optional or set()) or required - set(document):
        raise StorageError("request fields do not match this operation")


class ProjectAPI:
    """Saved-run application operations, separate from the legacy read-only Inspector."""

    def __init__(self, store: ProjectStore, *, recover: bool = True) -> None:
        self.store = store
        if recover:
            self.store.recover_interrupted()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheltersim-project")
        self._lock = threading.RLock()
        self._active: tuple[str, str, str] | None = None
        self._cancel = threading.Event()
        self._closed = False
        from sheltersim.web.workspace import ProjectWorkspace
        self.workspace = ProjectWorkspace(self)
        from sheltersim.web.project_services import ProjectServices
        self.services = ProjectServices(self)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.set()
        self._pool.shutdown(wait=True)

    def get(self, parts: list[str], query: dict[str, list[str]]) -> dict[str, Any]:
        shared = self.services.get(parts, query)
        if shared is not None:
            return shared
        if not parts:
            if query:
                raise StorageError("projects list takes no query parameters")
            return {"projects": self.store.projects(), "storage": self.store.usage()}
        project_id = parts[0]
        if len(parts) == 1 and not query:
            return self.store.project(project_id)
        if len(parts) == 2 and parts[1] == "scenarios" and not query:
            return {"scenarios": self.store.scenarios(project_id)}
        if len(parts) == 2 and parts[1] == "runs" and not query:
            return {"runs": self.store.runs(project_id)}
        if len(parts) == 3 and parts[1] == "runs" and not query:
            return self.store.run(project_id, parts[2])
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "telemetry" and not query:
            self.store.run(project_id, parts[2])
            return RunTelemetryStore(self.store, project_id, parts[2]).read()
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "compare" and set(query) == {"branch_ids", "minute"}:
            if any(len(values) != 1 for values in query.values()):
                raise StorageError("comparison parameters must each occur once")
            try:
                minute = int(query["minute"][0])
            except ValueError as exc:
                raise StorageError("comparison minute must be an integer") from exc
            return self.workspace.comparison(project_id, parts[2], query["branch_ids"][0].split(","), minute)
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "evidence" and not query:
            run = self.store.run(project_id, parts[2])
            scenario = self.store.scenario(project_id, run["scenario_id"], run["scenario_version"])
            return evidence_context(scenario)
        if len(parts) == 6 and parts[1] == "runs" and parts[3] == "branches":
            run_id, branch_id, action = parts[2], parts[4], parts[5]
            if action == "timeline" and set(query) == {"minute"} and len(query["minute"]) == 1:
                try:
                    minute = int(query["minute"][0])
                except ValueError as exc:
                    raise StorageError("minute must be an integer") from exc
                return TimelineStore(self.store, project_id, run_id).frame(branch_id, minute)
            if action == "report" and not query:
                self.store.load_branch(project_id, run_id, branch_id)
                path = self.store._path(project_id, "runs", run_id, "branches", branch_id, "report.json")
                return self.store._read(path) if path.exists() else {"status": "not_generated"}
            if action == "llm-status" and not query:
                from sheltersim.llm.outcomes import summarize_call_outcomes
                reference = self.store.branch_reference(project_id, run_id, branch_id)
                saved = self.store.checkpoint_fields(
                    project_id, run_id, reference.checkpoint_hash, ("runtime_state",)
                )["runtime_state"].get("llm")
                return {
                    "schema_version": 1, "branch_id": branch_id,
                    "checkpoint_hash": reference.checkpoint_hash,
                    "computed_until": reference.computed_until,
                    "mode": saved.get("mode") if isinstance(saved, dict) else None,
                    "outcomes": summarize_call_outcomes(
                        saved.get("records", []) if isinstance(saved, dict) else []
                    ),
                    "record_scope": "router records in the branch head checkpoint; a child "
                                    "branch includes the parent's records before its fork",
                }
            if action == "conversations" and not query:
                self.store.load_branch(project_id, run_id, branch_id)
                from sheltersim.storage.history_index import HistoryIndex
                return {"turns": HistoryIndex(self.store,project_id,run_id).conversation_tail(branch_id,None,100),
                        "coverage":"latest_100_turns", "history_route":"history?kind=conversations"}
        raise StorageError("unknown project route or query")

    def post(self, parts: list[str], document: dict[str, Any]) -> dict[str, Any]:
        shared = self.services.post(parts, document)
        if shared is not None:
            return shared
        extra = self.workspace.post(parts, document)
        if extra is not None:
            return extra
        if not parts:
            fields(document, {"project_id"})
            return self.store.create_project(document["project_id"])
        project_id = parts[0]
        if len(parts) == 2:
            if parts[1] == "archive":
                fields(document, set())
                self.store.archive(project_id)
                return self.store.project(project_id)
            if parts[1] == "scenarios":
                fields(document, {"scenario", "approved", "approved_hash"})
                if document["approved"] is not True:
                    raise StorageError("scenario needs explicit human approval")
                scenario = ScenarioSpec.model_validate(document["scenario"])
                return self.store.save_scenario(project_id, scenario, approved_hash=document["approved_hash"])
            if parts[1] == "runs":
                fields(document, {"run_id", "scenario_id", "scenario_version"}, {"usage_account_id","execution_mode"})
                if "execution_mode" in document and document["execution_mode"]!="offline":
                    raise StorageError("only_explicit_offline_experiment_mode_is_supported")
                settings=LLMSettings.from_env()
                if document.get("execution_mode")=="offline":
                    from dataclasses import replace
                    settings=replace(settings,mode=LLMMode.OFFLINE,api_key=None)
                existing_path = self.store._path(project_id, "runs", document["run_id"], "run.json")
                if existing_path.exists():
                    run = self.store.run(project_id, document["run_id"])
                    if (run["scenario_id"], run["scenario_version"]) != (document["scenario_id"], document["scenario_version"]):
                        raise StorageError("run_id_conflict")
                    if run["branches"]:
                        if document.get("execution_mode")=="offline":
                            _,initial=self.store.load_branch(project_id,run["run_id"],"main")
                            if initial.runtime_state.get("execution_purpose")!="offline_experiment":
                                raise StorageError("run_execution_mode_conflict")
                        UsageLedger.for_run(self.store, project_id, run["run_id"],
                            usage_limits(settings), document.get("usage_account_id"))
                        return run
                else:
                    run = self.store.create_run(project_id, document["run_id"], document["scenario_id"], document["scenario_version"])
                scenario = self.store.scenario(project_id, document["scenario_id"], document["scenario_version"])
                runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
                if document.get("execution_mode")=="offline":runtime.runtime_state["execution_purpose"]="offline_experiment"
                # Runtime AUTO selection is captured at the first checkpoint, not during seek.
                router = accounted_router(self.store, project_id, run_id=run["run_id"],
                    account_id=document.get("usage_account_id"), operation="runtime", branch_id="main",settings=settings)
                try:
                    TimelineStore(self.store, project_id, run["run_id"]).record(runtime, router=router)
                except Exception:
                    self.store.set_status(project_id, run["run_id"], "failed")
                    raise
                return self.store.run(project_id, run["run_id"])
        if len(parts) == 4 and parts[1] == "runs":
            run_id, action = parts[2], parts[3]
            if action == "telemetry":
                fields(document, {"kind", "branch_id", "samples", "fps", "p95_frame_ms", "visibility"})
                self.store.run(project_id, run_id)
                self.store.branch_reference(project_id, run_id, document["branch_id"])
                return RunTelemetryStore(self.store, project_id, run_id).record_render(document)
            if action == "archive":
                fields(document, set())
                self.store.archive(project_id, run_id)
                return self.store.run(project_id, run_id)
            if action == "delete":
                fields(document, {"confirmation"})
                with self._lock:
                    if self._active is not None and self._active[:2] == (project_id, run_id):
                        raise StorageError("cannot delete an active run")
                    self.store.delete_run(project_id, run_id, confirmation=document["confirmation"])
                return {"deleted": run_id}
            if action == "resume":
                fields(document, {"branch_id", "target_minute"}, {"measure_performance"})
                measure = document.get("measure_performance", False)
                if type(measure) is not bool:
                    raise StorageError("measure_performance must be boolean")
                return self.submit(
                    project_id,
                    run_id,
                    document["branch_id"],
                    document["target_minute"],
                    measure_performance=measure,
                )
            if action == "cancel":
                fields(document, set())
                with self._lock:
                    if self._active is not None and self._active[:2] == (project_id, run_id):
                        self._cancel.set()
                    elif self.store.run(project_id, run_id)["status"] == "running":
                        raise StorageError("run_worker_busy: cancellation must reach the owning worker")
                return self.store.run(project_id, run_id)
        if len(parts) == 6 and parts[1] == "runs" and parts[3] == "branches":
            run_id, branch_id, action = parts[2], parts[4], parts[5]
            if action == "fork":
                fields(document, {"minute", "child_id", "approved"}, {"program", "approved_program_hash"})
                if document["approved"] is not True:
                    raise StorageError("branch creation needs explicit human approval")
                with self._lock:
                    if self._active is not None:
                        raise StorageError("pause background calculation before forking")
                    program = EffectProgram.model_validate(document["program"]) if document.get("program") is not None else None
                    timeline = TimelineStore(self.store, project_id, run_id)
                    child = timeline.fork(branch_id, document["minute"], document["child_id"], program=program, approved_program_hash=document.get("approved_program_hash"))
                return {"branch_id": child.branch_id, "minute": child.world.minute_index, "state_hash": digest(child.world.to_dict())}
        raise StorageError("unknown project operation")

    def submit(
        self,
        project_id: str,
        run_id: str,
        branch_id: str,
        target_minute: int,
        *,
        measure_performance: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._active is not None:
                raise StorageError("background worker is busy; wait or pause the active run")
            _, checkpoint = self.store.load_branch(project_id, run_id, branch_id)
            if type(target_minute) is not int or not checkpoint.simulation_minute < target_minute <= checkpoint.world_state["duration_minutes"]:
                raise StorageError("target minute must be in the uncomputed remaining range")
            lease = self.store.claim_run(project_id, run_id)
            try:
                self._active = (project_id, run_id, branch_id)
                self._cancel = threading.Event()
                self.store.set_status(project_id, run_id, "running")
                self._pool.submit(self._advance, project_id, run_id, branch_id, target_minute, self._cancel, lease, measure_performance)
            except BaseException:
                self._active = None
                try:
                    self.store.set_status(project_id, run_id, "interrupted")
                finally:
                    self.store.release_run(lease)
                raise
            return {"status": "running", "run_id": run_id, "branch_id": branch_id, "target_minute": target_minute}

    def _advance(
        self,
        project_id: str,
        run_id: str,
        branch_id: str,
        target_minute: int,
        cancel: threading.Event,
        lease: int,
        measure_performance: bool = False,
    ) -> None:
        telemetry = RunTelemetryStore(self.store, project_id, run_id) if measure_performance else None
        storage_before = 0
        storage_limit = 0
        if telemetry is not None:
            try:
                storage_snapshot = self.store.usage()
                storage_before = storage_snapshot["bytes"]
                storage_limit = storage_snapshot["limit_bytes"]
            except Exception:
                telemetry = None
        checkpoint_bytes_before = self.store.checkpoint_write_bytes(project_id, run_id) if telemetry else 0
        wall_started = time.perf_counter()
        cpu_clock = getattr(time, "thread_time", time.process_time)
        cpu_started = cpu_clock()
        status = "failed"
        failure_code = None
        from_minute = 0
        computed_until = 0
        try:
            _, checkpoint = self.store.load_branch(project_id, run_id, branch_id)
            from_minute = checkpoint.simulation_minute
            saved_llm = checkpoint.runtime_state.get("llm")
            router = None
            if saved_llm is not None:
                settings = LLMSettings.from_env()
                if saved_llm["mode"]=="offline":
                    from sheltersim.experiments.timeline import settings_from_record
                    settings=settings_from_record(checkpoint.runtime_state.get("llm_settings",{}),mode=LLMMode.OFFLINE,api_key=None)
                elif settings.effective_mode.value != saved_llm["mode"]:
                    raise StorageError("saved LLM mode unavailable; restore original configuration")
                router = accounted_router(self.store, project_id, run_id=run_id,
                                          operation="runtime", branch_id=branch_id, settings=settings)
            runtime = TimelineStore(self.store, project_id, run_id)._advance_locked(branch_id, target_minute, router=router, cancelled=cancel.is_set)
            status = "completed" if runtime.world.minute_index == runtime.world.duration_minutes else "paused"
            computed_until = runtime.world.minute_index
            if status == "completed":
                # The UI fetches the report once when the status leaves "running", so
                # write the report (or its unavailability) before marking completion.
                try:
                    self.workspace.finish_review(project_id, run_id, branch_id, runtime)
                except Exception:
                    # Simulation completion remains valid even if bounded reporting is unavailable.
                    path = self.store._path(project_id, "runs", run_id, "branches", branch_id, "report.json")
                    try:
                        with self.store.transaction():
                            self.store._write(path, {"schema_version": 1, "status": "unavailable", "limitation": "No validated report was generated within the configured mode/budget."})
                    except (StorageError, OSError):
                        # A full disk can also prevent recording the optional report failure.
                        # Keep the already durable completed simulation and its status.
                        pass
            self.store.set_status(project_id, run_id, status)
        except Exception as exc:
            # Provider errors may contain secrets; only a fixed failure code is persisted.
            failure_code = type(exc).__name__
            self.store.set_status(project_id, run_id, "failed")
        finally:
            wall_seconds = time.perf_counter() - wall_started
            cpu_seconds = max(0.0, cpu_clock() - cpu_started)
            if telemetry is not None:
                try:
                    storage_snapshot = self.store.usage()
                    storage_after = storage_snapshot["bytes"]
                    storage_limit = storage_snapshot["limit_bytes"]
                    peak_rss = 0
                    if _resource is not None:
                        peak = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
                        peak_rss = int(peak if sys.platform == "darwin" else peak * 1024)
                    if computed_until == 0:
                        try:
                            computed_until = self.store.branch_reference(project_id, run_id, branch_id).computed_until
                        except StorageError:
                            computed_until = from_minute
                    telemetry.record_execution({
                        "branch_id": branch_id,
                        "from_minute": from_minute,
                        "target_minute": target_minute,
                        "computed_until": computed_until,
                        "status": status,
                        "wall_seconds": wall_seconds,
                        "thread_cpu_seconds": cpu_seconds,
                        "process_peak_rss_bytes": peak_rss,
                        "run_peak_memory_bytes": None,
                        "memory_attribution_status": "unavailable",
                        "memory_attribution_reason": (
                            "Runs execute in the shared server process; process-wide RSS cannot be attributed to this Run."
                        ),
                        "project_storage_before_bytes": storage_before,
                        "project_storage_after_bytes": storage_after,
                        "project_storage_limit_bytes": storage_limit,
                        "checkpoint_write_bytes": max(
                            0,
                            self.store.checkpoint_write_bytes(project_id, run_id)
                            - checkpoint_bytes_before,
                        ),
                        **({"failure_code": failure_code} if failure_code else {}),
                    })
                except Exception:
                    # Measurement failure never changes the durable simulation outcome.
                    pass
            self.store.release_run(lease)
            with self._lock:
                self._active = None
