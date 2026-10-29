"""Persistent project HTTP adapter and bounded resumable cloud background runs."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from sheltersim.core.world import WorldState
from sheltersim.events.effects import EffectProgram
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineError, TimelineStore
from sheltersim.experiments.validation import evidence_context
from sheltersim.llm.accounting import accounted_router, usage_limits
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.storage.telemetry import RunTelemetryStore
from sheltersim.storage.usage import UsageLedger

try:
    import resource as _resource
except ImportError:  # pragma: no cover - unavailable on Windows
    _resource = None


def fields(document: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    if set(document) - required - (optional or set()) or required - set(document):
        raise StorageError("request fields do not match this operation")


def _peak_rss_bytes() -> int:
    """This process's peak resident memory in bytes, or 0 where it cannot be read."""
    if _resource is not None:
        peak = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        # Handles are pointer-sized; without these types ctypes passes a C int.
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.PeakWorkingSetSize)
    return 0


@dataclass
class _Job:
    """A computation or fork running for one Run."""

    branch_id: str
    cancel: Any
    # In process mode, the thread that waits for the job's process to end.
    watcher: threading.Thread | None = None


class ProjectAPI:
    """Saved-run application operations, separate from the legacy read-only Inspector."""

    def __init__(
        self,
        store: ProjectStore,
        *,
        recover: bool = True,
        worker: str = "thread",
        max_jobs: int = 1,
    ) -> None:
        """worker="process" computes each branch and creates each fork in its own process
        (the server, #48), up to max_jobs at once, across Runs and their branches;
        "thread" computes one branch at a time on a thread of this process and forks in
        the caller (tests and CLI)."""
        if worker not in {"thread", "process"}:
            raise ValueError("worker must be 'thread' or 'process'")
        if type(max_jobs) is not int or max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        self.store = store
        if recover:
            self.store.recover_interrupted()
        self._worker = worker
        # A thread shares the GIL, so only processes compute Runs in parallel.
        self._max_jobs = max_jobs if worker == "process" else 1
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheltersim-project")
        self._lock = threading.RLock()
        # The job of each branch being computed or created by a fork, by (project, run,
        # branch). A branch has at most one, as its writer lease allows (#48).
        self._jobs: dict[tuple[str, str, str], _Job] = {}
        # The outcome of the last fork started in this server process, by Run.
        self._fork_outcomes: dict[tuple[str, str], dict[str, Any]] = {}
        self._closed = False
        from sheltersim.web.workspace import ProjectWorkspace

        self.workspace = ProjectWorkspace(self)
        from sheltersim.web.project_services import ProjectServices

        self.services = ProjectServices(self)
        from sheltersim.web.pdf_report import PDFReportService

        self.pdf_reports = PDFReportService(self)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancel.set()
        for job in jobs:
            if job.watcher is not None:
                job.watcher.join()
        self._pool.shutdown(wait=True)

    def wait_idle(self, timeout: float | None = None) -> None:
        """Wait until the computations and forks submitted so far have ended."""
        if self._worker == "thread":
            self._pool.submit(lambda: None).result(timeout=timeout)
            return
        with self._lock:
            watchers = [job.watcher for job in self._jobs.values() if job.watcher is not None]
        deadline = None if timeout is None else time.monotonic() + timeout
        for watcher in watchers:
            watcher.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
            if watcher.is_alive():
                raise TimeoutError("the background job is still running")

    def _admit(self, project_id: str, run_id: str, branch_id: str) -> None:
        """Refuse a job for a branch that has one, or beyond max_jobs; holds self._lock."""
        if self._closed or (project_id, run_id, branch_id) in self._jobs:
            raise StorageError("background worker is busy; wait or pause the active run")
        if len(self._jobs) >= self._max_jobs:
            raise StorageError("background workers are busy; wait until a running computation ends")

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
            run = self.store.run(project_id, parts[2])
            with self._lock:
                outcome = self._fork_outcomes.get((project_id, parts[2]))
            return {**run, "last_fork": outcome} if outcome is not None else run
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "telemetry" and not query:
            self.store.run(project_id, parts[2])
            return RunTelemetryStore(self.store, project_id, parts[2]).read()
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "replay-frames":
            from sheltersim.web.replay import replay_frames

            if set(query) != {"branch_ids", "start", "end"} or any(
                len(values) != 1 for values in query.values()
            ):
                raise StorageError("replay parameters must each occur once")
            try:
                start, end = int(query["start"][0]), int(query["end"][0])
            except ValueError as exc:
                raise StorageError("replay bounds must be integers") from exc
            return replay_frames(
                self.store,
                self.workspace,
                project_id,
                parts[2],
                query["branch_ids"][0].split(","),
                start,
                end,
            )
        if (
            len(parts) == 4
            and parts[1] == "runs"
            and parts[3] == "compare"
            and set(query) == {"branch_ids", "minute"}
        ):
            if any(len(values) != 1 for values in query.values()):
                raise StorageError("comparison parameters must each occur once")
            try:
                minute = int(query["minute"][0])
            except ValueError as exc:
                raise StorageError("comparison minute must be an integer") from exc
            return self.workspace.comparison(
                project_id, parts[2], query["branch_ids"][0].split(","), minute
            )
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
                # The report is its own artifact; the branch pointer proves the
                # branch exists without decoding its checkpoint history.
                self.store.branch_reference(project_id, run_id, branch_id)
                path = self.store._path(
                    project_id, "runs", run_id, "branches", branch_id, "report.json"
                )
                return self.store._read(path) if path.exists() else {"status": "not_generated"}
            if action == "llm-status" and not query:
                from sheltersim.llm.outcomes import summarize_call_outcomes

                reference = self.store.branch_reference(project_id, run_id, branch_id)
                saved = self.store.checkpoint_fields(
                    project_id, run_id, reference.checkpoint_hash, ("runtime_state",)
                )["runtime_state"].get("llm")
                return {
                    "schema_version": 1,
                    "branch_id": branch_id,
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
                self.store.branch_reference(project_id, run_id, branch_id)
                from sheltersim.storage.history_index import HistoryIndex

                return {
                    "turns": HistoryIndex(self.store, project_id, run_id).conversation_tail(
                        branch_id, None, 100
                    ),
                    "coverage": "latest_100_turns",
                    "history_route": "history?kind=conversations",
                }
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
                return self.store.save_scenario(
                    project_id, scenario, approved_hash=document["approved_hash"]
                )
            if parts[1] == "runs":
                fields(
                    document,
                    {"run_id", "scenario_id", "scenario_version"},
                    {"usage_account_id", "execution_mode"},
                )
                if "execution_mode" in document and document["execution_mode"] != "offline":
                    raise StorageError("only_explicit_offline_experiment_mode_is_supported")
                settings = LLMSettings.from_env()
                if document.get("execution_mode") == "offline":
                    from dataclasses import replace

                    settings = replace(settings, mode=LLMMode.OFFLINE, api_key=None)
                existing_path = self.store._path(project_id, "runs", document["run_id"], "run.json")
                if existing_path.exists():
                    run = self.store.run(project_id, document["run_id"])
                    if (run["scenario_id"], run["scenario_version"]) != (
                        document["scenario_id"],
                        document["scenario_version"],
                    ):
                        raise StorageError("run_id_conflict")
                    if run["branches"]:
                        if document.get("execution_mode") == "offline":
                            _, initial = self.store.load_branch(project_id, run["run_id"], "main")
                            if (
                                initial.runtime_state.get("execution_purpose")
                                != "offline_experiment"
                            ):
                                raise StorageError("run_execution_mode_conflict")
                        UsageLedger.for_run(
                            self.store,
                            project_id,
                            run["run_id"],
                            usage_limits(settings),
                            document.get("usage_account_id"),
                        )
                        return run
                else:
                    run = self.store.create_run(
                        project_id,
                        document["run_id"],
                        document["scenario_id"],
                        document["scenario_version"],
                    )
                scenario = self.store.scenario(
                    project_id, document["scenario_id"], document["scenario_version"]
                )
                runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
                if document.get("execution_mode") == "offline":
                    runtime.runtime_state["execution_purpose"] = "offline_experiment"
                # Runtime AUTO selection is captured at the first checkpoint, not during seek.
                router = accounted_router(
                    self.store,
                    project_id,
                    run_id=run["run_id"],
                    account_id=document.get("usage_account_id"),
                    operation="runtime",
                    branch_id="main",
                    settings=settings,
                )
                try:
                    TimelineStore(self.store, project_id, run["run_id"]).record(
                        runtime, router=router
                    )
                except Exception:
                    self.store.set_status(project_id, run["run_id"], "failed")
                    raise
                return self.store.run(project_id, run["run_id"])
        if len(parts) == 4 and parts[1] == "runs":
            run_id, action = parts[2], parts[3]
            if action == "telemetry":
                fields(
                    document, {"kind", "branch_id", "samples", "fps", "p95_frame_ms", "visibility"}
                )
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
                    if any(key[:2] == (project_id, run_id) for key in self._jobs):
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
                # Without branch_id, every computation of the Run stops (#48).
                fields(document, set(), {"branch_id"})
                with self._lock:
                    jobs = [
                        job
                        for key, job in self._jobs.items()
                        if key[:2] == (project_id, run_id)
                        and document.get("branch_id") in {None, key[2]}
                    ]
                    for job in jobs:
                        job.cancel.set()
                    if not jobs and self.store.run(project_id, run_id)["status"] == "running":
                        raise StorageError(
                            "run_worker_busy: cancellation must reach the owning worker"
                        )
                return self.store.run(project_id, run_id)
        if len(parts) == 6 and parts[1] == "runs" and parts[3] == "branches":
            run_id, branch_id, action = parts[2], parts[4], parts[5]
            if action == "fork":
                return self.fork(project_id, run_id, branch_id, document)
        raise StorageError("unknown project operation")

    def fork(
        self,
        project_id: str,
        run_id: str,
        branch_id: str,
        document: dict[str, Any],
        *,
        expected_state_hash: str | None = None,
    ) -> dict[str, Any]:
        """The fork route; an intervention apply also passes the state its preview saw."""
        fields(
            document,
            {"minute", "child_id", "approved"},
            {"program", "approved_program_hash"},
        )
        if document["approved"] is not True:
            raise StorageError("branch creation needs explicit human approval")
        with self._lock:
            # Another branch of the Run may compute meanwhile (#48).
            if (project_id, run_id, branch_id) in self._jobs:
                raise StorageError("pause background calculation before forking")
            program = (
                EffectProgram.model_validate(document["program"])
                if document.get("program") is not None
                else None
            )
            if self._worker == "process":
                self._admit(project_id, run_id, document["child_id"])
                return self._fork_process(
                    project_id, run_id, branch_id, document, program, expected_state_hash
                )
            timeline = TimelineStore(self.store, project_id, run_id)
            child = timeline.fork(
                branch_id,
                document["minute"],
                document["child_id"],
                program=program,
                approved_program_hash=document.get("approved_program_hash"),
                expected_state_hash=expected_state_hash,
            )
            # The child's saved head holds the hash of its world.
            head = self.store.branch_reference(project_id, run_id, child.branch_id)
            saved = self.store.checkpoint_fields(
                project_id, run_id, head.checkpoint_hash, ("state_hash",)
            )
        return {
            "branch_id": child.branch_id,
            "minute": child.world.minute_index,
            "state_hash": saved["state_hash"],
        }

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
            self._admit(project_id, run_id, branch_id)
            # The pointer and the immutable Scenario bound the range without decoding
            # the history; the worker loads and validates the checkpoint once.
            reference = self.store.branch_reference(project_id, run_id, branch_id)
            run = self.store.run(project_id, run_id)
            scenario = self.store.scenario(project_id, run["scenario_id"], run["scenario_version"])
            if (
                type(target_minute) is not int
                or not reference.computed_until < target_minute <= scenario.duration_minutes
            ):
                raise StorageError("target minute must be in the uncomputed remaining range")
            if self._worker == "process":
                return self._submit_process(
                    project_id, run_id, branch_id, target_minute, measure_performance
                )
            lease = self.store.claim_branch(project_id, run_id, branch_id)
            try:
                job = _Job(branch_id, threading.Event())
                self._jobs[(project_id, run_id, branch_id)] = job
                self.store.set_branch_status(project_id, run_id, branch_id, "running")
                self._pool.submit(
                    self._advance,
                    project_id,
                    run_id,
                    branch_id,
                    target_minute,
                    job.cancel.is_set,
                    lease,
                    measure_performance,
                )
            except BaseException:
                self._jobs.pop((project_id, run_id, branch_id), None)
                try:
                    self.store.set_branch_status(project_id, run_id, branch_id, "interrupted")
                finally:
                    self.store.release_run(lease)
                raise
            return {
                "status": "running",
                "run_id": run_id,
                "branch_id": branch_id,
                "target_minute": target_minute,
            }

    def _fork_process(
        self,
        project_id: str,
        run_id: str,
        branch_id: str,
        document: dict[str, Any],
        program: EffectProgram | None,
        expected_state_hash: str | None,
    ) -> dict[str, Any]:
        """Start the fork in its own process; called with self._lock held.

        The checks that need no restore answer at once; the fork's outcome is the
        run's last_fork once the Run's status leaves running.
        """
        reference = self.store.branch_reference(project_id, run_id, branch_id)
        minute = document["minute"]
        if (
            type(minute) is not int
            or not reference.fork_minute <= minute <= reference.computed_until
        ):
            raise TimelineError("minute is outside this branch's computed range")
        child_id = document["child_id"]
        child = self.store._path(project_id, "runs", run_id, "branches", child_id, "branch.json")
        if child.exists():
            raise TimelineError("child branch already exists")
        if program is not None and document.get("approved_program_hash") != digest(
            program.model_dump(mode="json")
        ):
            raise TimelineError("EffectProgram approval hash does not match preview")
        previous = self.store.run(project_id, run_id)["status"]
        # The Run's status once no branch computes; a fork itself computes nothing.
        idle = previous if previous != "running" else "paused"
        self._fork_outcomes.pop((project_id, run_id), None)
        self._start_process(
            {
                "kind": "fork",
                "project_id": project_id,
                "run_id": run_id,
                "branch_id": branch_id,
                "document": document,
                "expected_state_hash": expected_state_hash,
            },
            (project_id, run_id, child_id),
            idle,
        )
        return {"status": "running", "branch_id": child_id, "minute": minute}

    def _submit_process(
        self,
        project_id: str,
        run_id: str,
        branch_id: str,
        target_minute: int,
        measure_performance: bool,
    ) -> dict[str, Any]:
        """Start the run's process; called with self._lock held."""
        self._start_process(
            {
                "kind": "advance",
                "project_id": project_id,
                "run_id": run_id,
                "branch_id": branch_id,
                "target_minute": target_minute,
                "measure_performance": measure_performance,
            },
            (project_id, run_id, branch_id),
            None,
        )
        return {
            "status": "running",
            "run_id": run_id,
            "branch_id": branch_id,
            "target_minute": target_minute,
        }

    def _start_process(
        self, job: dict[str, Any], active: tuple[str, str, str], restore: str | None
    ) -> None:
        from sheltersim.web.worker_process import start_worker

        process, cancel, outcome = start_worker(self.store, job)
        running = _Job(active[2], cancel)
        running.watcher = threading.Thread(
            target=self._watch,
            args=(process, outcome, job["kind"], *active, restore, running),
            name=f"sheltersim-watch-{active[1]}",
            daemon=True,
        )
        self._jobs[active] = running
        running.watcher.start()

    def _watch(
        self,
        process: Any,
        outcome: Any,
        kind: str,
        project_id: str,
        run_id: str,
        branch_id: str,
        restore: str | None,
        job: _Job,
    ) -> None:
        """Wait for a job's process. A process that ended without recording an
        outcome (killed, crashed) leaves its branch interrupted, as a restart would. A
        fork's child is paused once created, its status is forgotten otherwise, and
        the Run returns to restore unless another branch computes."""
        result = None
        try:
            while True:
                result = outcome.recv()
        except (EOFError, OSError):
            pass
        finally:
            outcome.close()
        process.join()
        try:
            if kind == "fork":
                state, detail = result if result is not None else (None, None)
                recorded = (
                    {"status": "created", **detail}
                    if state == "forked"
                    else {"status": "failed", **detail}
                    if state == "fork_failed"
                    else {"status": "failed", "error": "run_worker_ended", "code": None}
                )
                with self._lock:
                    self._fork_outcomes[(project_id, run_id)] = recorded
            try:
                lease = self.store.claim_branch(project_id, run_id, branch_id)
            except StorageError:
                # Another process owns the branch now, or the Run was deleted.
                pass
            else:
                try:
                    if kind == "fork":
                        created = self.store._path(
                            project_id, "runs", run_id, "branches", branch_id, "branch.json"
                        ).exists()
                        if created:
                            self.store.set_branch_status(
                                project_id, run_id, branch_id, "paused", run_status=restore
                            )
                        else:
                            self.store.forget_branch_status(project_id, run_id, branch_id)
                            self.store.settle_run_status(project_id, run_id, restore or "paused")
                    elif self.store.branch_status(project_id, run_id, branch_id) in {
                        "queued",
                        "running",
                    }:
                        self.store.set_branch_status(project_id, run_id, branch_id, "interrupted")
                finally:
                    self.store.release_run(lease)
        finally:
            with self._lock:
                if self._jobs.get((project_id, run_id, branch_id)) is job:
                    del self._jobs[(project_id, run_id, branch_id)]

    def _advance(
        self,
        project_id: str,
        run_id: str,
        branch_id: str,
        target_minute: int,
        cancelled: Callable[[], bool],
        lease: int,
        measure_performance: bool = False,
        *,
        own_process: bool = False,
    ) -> None:
        telemetry = (
            RunTelemetryStore(self.store, project_id, run_id) if measure_performance else None
        )
        storage_before = 0
        storage_limit = 0
        if telemetry is not None:
            try:
                storage_snapshot = self.store.usage()
                storage_before = storage_snapshot["bytes"]
                storage_limit = storage_snapshot["limit_bytes"]
            except Exception:
                telemetry = None
        checkpoint_bytes_before = (
            self.store.checkpoint_write_bytes(project_id, run_id) if telemetry else 0
        )
        wall_started = time.perf_counter()
        cpu_clock = getattr(time, "thread_time", time.process_time)
        cpu_started = cpu_clock()
        status = "failed"
        failure_code = None
        from_minute = 0
        computed_until = 0
        try:
            # Router setup needs only the head pointer and runtime_state; the full
            # checkpoint is loaded and validated once by _advance_locked.
            reference = self.store.branch_reference(project_id, run_id, branch_id)
            runtime_state = self.store.checkpoint_fields(
                project_id, run_id, reference.checkpoint_hash, ("runtime_state",)
            )["runtime_state"]
            from_minute = reference.computed_until
            saved_llm = runtime_state.get("llm")
            router = None
            if saved_llm is not None:
                settings = LLMSettings.from_env()
                if saved_llm["mode"] == "offline":
                    from sheltersim.experiments.timeline import settings_from_record

                    settings = settings_from_record(
                        runtime_state.get("llm_settings", {}), mode=LLMMode.OFFLINE, api_key=None
                    )
                elif settings.effective_mode.value != saved_llm["mode"]:
                    raise StorageError("saved LLM mode unavailable; restore original configuration")
                router = accounted_router(
                    self.store,
                    project_id,
                    run_id=run_id,
                    operation="runtime",
                    branch_id=branch_id,
                    settings=settings,
                )
            runtime = TimelineStore(self.store, project_id, run_id)._advance_locked(
                branch_id, target_minute, router=router, cancelled=cancelled
            )
            status = (
                "completed"
                if runtime.world.minute_index == runtime.world.duration_minutes
                else "paused"
            )
            computed_until = runtime.world.minute_index
            if status == "completed":
                # The UI fetches the report once when the status leaves "running", so
                # write the report (or its unavailability) before marking completion.
                try:
                    self.workspace.finish_review(project_id, run_id, branch_id, runtime)
                except Exception:
                    # Simulation completion remains valid even if bounded reporting is unavailable.
                    path = self.store._path(
                        project_id, "runs", run_id, "branches", branch_id, "report.json"
                    )
                    try:
                        with self.store.transaction():
                            self.store._write(
                                path,
                                {
                                    "schema_version": 1,
                                    "status": "unavailable",
                                    "limitation": (
                                        "No validated report was generated within the configured "
                                        "mode/budget."
                                    ),
                                },
                            )
                    except (StorageError, OSError):
                        # A full disk can also prevent recording the optional report failure.
                        # Keep the already durable completed simulation and its status.
                        pass
            self.store.set_branch_status(project_id, run_id, branch_id, status)
        except Exception as exc:
            # Provider errors may contain secrets; only a fixed failure code is persisted.
            failure_code = type(exc).__name__
            self.store.set_branch_status(project_id, run_id, branch_id, "failed")
        finally:
            wall_seconds = time.perf_counter() - wall_started
            cpu_seconds = max(0.0, cpu_clock() - cpu_started)
            if telemetry is not None:
                try:
                    storage_snapshot = self.store.usage()
                    storage_after = storage_snapshot["bytes"]
                    storage_limit = storage_snapshot["limit_bytes"]
                    peak_rss = _peak_rss_bytes()
                    if computed_until == 0:
                        try:
                            computed_until = self.store.branch_reference(
                                project_id, run_id, branch_id
                            ).computed_until
                        except StorageError:
                            computed_until = from_minute
                    telemetry.record_execution(
                        {
                            "branch_id": branch_id,
                            "from_minute": from_minute,
                            "target_minute": target_minute,
                            "computed_until": computed_until,
                            "status": status,
                            "wall_seconds": wall_seconds,
                            "thread_cpu_seconds": cpu_seconds,
                            "process_peak_rss_bytes": peak_rss,
                            **(
                                {
                                    "run_peak_memory_bytes": peak_rss,
                                    "memory_attribution_status": "measured",
                                    "memory_attribution_reason": (
                                        "The Run executed in its own worker process; this is "
                                        "that process's peak RSS."
                                    ),
                                }
                                if own_process and peak_rss
                                else {
                                    "run_peak_memory_bytes": None,
                                    "memory_attribution_status": "unavailable",
                                    "memory_attribution_reason": (
                                        "Runs execute in the shared server process; "
                                        "process-wide RSS cannot be attributed to this Run."
                                    ),
                                }
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
                        }
                    )
                except Exception:
                    # Measurement failure never changes the durable simulation outcome.
                    pass
            self.store.release_run(lease)
            with self._lock:
                self._jobs.pop((project_id, run_id, branch_id), None)
