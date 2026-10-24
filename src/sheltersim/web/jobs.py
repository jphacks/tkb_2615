"""Bounded local background jobs for long offline demo advances."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.web.demo import DemoError, DemoService

_ACTIVE = frozenset({"queued", "running", "cancelling"})
_TERMINAL = frozenset({"cancelled", "completed", "failed", "interrupted"})
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


class DemoJobError(ValueError):
    """Invalid job request, unavailable job, or bounded queue conflict."""


class DemoJobManager:
    """One-worker, bounded, restart-aware manager for local simulation advances."""

    def __init__(
        self,
        demo: DemoService,
        storage_directory: str | Path,
        *,
        chunk_minutes: int = 30,
        max_active_jobs: int = 2,
        max_retained_jobs: int = 25,
    ) -> None:
        if chunk_minutes < 1 or max_active_jobs < 1 or max_retained_jobs < max_active_jobs:
            raise ValueError("job limits must be positive and retained jobs must cover the queue")
        self.demo = demo
        self.storage_directory = Path(storage_directory)
        self.storage_directory.mkdir(parents=True, exist_ok=True)
        self.chunk_minutes = chunk_minutes
        self.max_active_jobs = max_active_jobs
        self.max_retained_jobs = max_retained_jobs
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheltersim-demo")
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_by_branch: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._closed = False
        self._restore_saved_jobs()

    def submit(self, branch_id: str, target_minute: int) -> dict[str, Any]:
        """Create or return an idempotent active job for the requested branch target."""
        if not isinstance(branch_id, str) or not branch_id or len(branch_id) > 128:
            raise DemoJobError("branch_id must be a non-empty string of at most 128 characters")
        if not isinstance(target_minute, int) or isinstance(target_minute, bool):
            raise DemoJobError("target_minute must be an integer")
        with self._lock:
            if self._closed:
                raise DemoJobError("background job manager is shutting down")
            existing_id = self._active_by_branch.get(branch_id)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing["target_minute"] == target_minute:
                    return {**self._snapshot(existing), "duplicate": True}
                raise DemoJobError("branch already has a different active job")
            if sum(job["status"] in _ACTIVE for job in self._jobs.values()) >= self.max_active_jobs:
                raise DemoJobError("background job queue is full")
            if len(self._jobs) >= self.max_retained_jobs:
                self._prune_terminal_jobs()
            if len(self._jobs) >= self.max_retained_jobs:
                raise DemoJobError(
                    "retained job limit reached; restart the local demo to clear history"
                )

            job_id = uuid.uuid4().hex
            try:
                start_minute = self.demo.reserve_job(branch_id, job_id, target_minute)
            except DemoError as exc:
                raise DemoJobError(str(exc)) from exc
            demo_state = self.demo.state()
            job: dict[str, Any] = {
                "job_id": job_id,
                "branch_id": branch_id,
                "target_minute": target_minute,
                "start_minute": start_minute,
                "minute": start_minute,
                "percent": 0,
                "status": "queued",
                "error": None,
                "execution_mode": "live_compute",
                "scenario_id": demo_state["scenario_id"],
                "seed": demo_state["seed"],
                "agent_count": demo_state["agent_count"],
                "duration_minutes": demo_state["duration_minutes"],
                "scenario_source": demo_state["scenario_source"],
                "scenario_hash": demo_state["scenario_hash"],
                "scenario_family_hash": demo_state["scenario_family_hash"],
                "run_id": demo_state["run_id"],
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._jobs[job_id] = job
            self._active_by_branch[branch_id] = job_id
            self._cancel_events[job_id] = threading.Event()
            self._persist(job, self.demo.checkpoint_for_job(branch_id))
            self._executor.submit(self._run, job_id)
            return {**self._snapshot(job), "duplicate": False}

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DemoJobError("unknown job_id")
            return self._snapshot(job)

    def list(self) -> dict[str, list[dict[str, Any]]]:
        """Return a bounded recent-job summary for local reload recovery."""
        with self._lock:
            recent = sorted(self._jobs.values(), key=lambda job: job["updated_at"], reverse=True)[
                : self.max_retained_jobs
            ]
            return {"jobs": [self._snapshot(job) for job in recent]}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DemoJobError("unknown job_id")
            if job["status"] in _TERMINAL:
                return self._snapshot(job)
            self._cancel_events[job_id].set()
            job["status"] = "cancelling"
            job["updated_at"] = _now()
            # Reuse the last persisted chunk; cancellation must not wait for the
            # worker's current isolated chunk to release the DemoService lock.
            self._persist(job)
            return self._snapshot(job)

    def close(self) -> None:
        """Request cancellation at the next chunk boundary and wait for local cleanup."""
        with self._lock:
            self._closed = True
            for job_id, event in self._cancel_events.items():
                if self._jobs[job_id]["status"] in _ACTIVE:
                    event.set()
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            event = self._cancel_events[job_id]
            if event.is_set():
                should_cancel = True
            else:
                should_cancel = False
                job["status"] = "running"
                job["updated_at"] = _now()
                self._persist(job, self.demo.checkpoint_for_job(job["branch_id"]))
        try:
            if not should_cancel:
                while True:
                    with self._lock:
                        job = self._jobs[job_id]
                        if event.is_set():
                            break
                        remaining = job["target_minute"] - job["minute"]
                        if remaining <= 0:
                            break
                        steps = min(remaining, self.chunk_minutes)
                        branch_id = job["branch_id"]
                    minute = self.demo.advance_job_chunk(branch_id, job_id, steps)
                    with self._lock:
                        job["minute"] = minute
                        job["percent"] = _percent(job["start_minute"], job["target_minute"], minute)
                        job["updated_at"] = _now()
                        self._persist(job, self.demo.checkpoint_for_job(branch_id))
            cancelled = event.is_set()
            checkpoint = self.demo.finish_job(job["branch_id"], job_id)
            with self._lock:
                job["status"] = "cancelled" if cancelled else "completed"
                job["minute"] = checkpoint.simulation_minute
                job["percent"] = _percent(job["start_minute"], job["target_minute"], job["minute"])
                job["updated_at"] = _now()
                if cancelled:
                    job["error"] = "cancelled at the last completed chunk"
                self._active_by_branch.pop(job["branch_id"], None)
                self._persist(job, checkpoint)
        except Exception as exc:
            try:
                checkpoint = self.demo.finish_job(job["branch_id"], job_id)
            except DemoError:
                checkpoint = None
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
                job["updated_at"] = _now()
                self._active_by_branch.pop(job["branch_id"], None)
                if checkpoint is None:
                    self._persist(job)
                else:
                    job["minute"] = checkpoint.simulation_minute
                    job["percent"] = _percent(
                        job["start_minute"], job["target_minute"], job["minute"]
                    )
                    self._persist(job, checkpoint)

    def _restore_saved_jobs(self) -> None:
        loaded: list[tuple[dict[str, Any], Checkpoint]] = []
        for path in sorted(self.storage_directory.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or set(document) != {"schema_version", "job", "checkpoint"}
                or document["schema_version"] != 1
            ):
                raise DemoJobError(f"invalid persisted job record: {path.name}")
            job = document["job"]
            checkpoint = Checkpoint.from_dict(document["checkpoint"])
            if not isinstance(job, dict) or not _JOB_ID.fullmatch(str(job.get("job_id", ""))):
                raise DemoJobError(f"invalid persisted job metadata: {path.name}")
            if job["job_id"] != path.stem:
                raise DemoJobError(f"persisted job id does not match filename: {path.name}")
            loaded.append((job, checkpoint))
        loaded.sort(
            key=lambda pair: (
                pair[0]["branch_id"],
                pair[1].simulation_minute,
                pair[0]["updated_at"],
            ),
            reverse=True,
        )
        restored_branches: set[str] = set()
        restore_errors: dict[str, str] = {}
        for job, checkpoint in loaded:
            branch_id = job["branch_id"]
            if branch_id not in restored_branches:
                try:
                    self.demo.restore_job_branch(branch_id, checkpoint)
                except (DemoError, ValueError) as exc:
                    restore_errors[branch_id] = str(exc)
                restored_branches.add(branch_id)
            if branch_id in restore_errors:
                job["status"] = "failed"
                job["error"] = f"saved branch could not be restored: {restore_errors[branch_id]}"[
                    :300
                ]
                job["updated_at"] = _now()
            elif job["status"] in _ACTIVE:
                job["status"] = "interrupted"
                job["error"] = "server restarted; branch restored from the last completed chunk"
                job["updated_at"] = _now()
            self._jobs[job["job_id"]] = job
            if job["status"] in {"interrupted", "failed"}:
                self._persist(job, checkpoint)
        if len(self._jobs) > self.max_retained_jobs:
            self._prune_terminal_jobs()

    def _persist(self, job: dict[str, Any], checkpoint: Checkpoint | None = None) -> None:
        if checkpoint is None:
            path = self._job_path(job["job_id"])
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                checkpoint = Checkpoint.from_dict(existing["checkpoint"])
            else:
                checkpoint = self.demo.checkpoint_for_job(job["branch_id"])
        document = json.dumps(
            {"schema_version": 1, "job": job, "checkpoint": checkpoint.to_dict()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        destination = self._job_path(job["job_id"])
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(document + "\n", encoding="utf-8")
        os.replace(temporary, destination)

    def _prune_terminal_jobs(self) -> None:
        newest_by_branch: dict[str, str] = {}
        for job in self._jobs.values():
            current_id = newest_by_branch.get(job["branch_id"])
            current = self._jobs.get(current_id) if current_id else None
            if current is None or current["updated_at"] < job["updated_at"]:
                newest_by_branch[job["branch_id"]] = job["job_id"]
        removable = sorted(
            (
                job
                for job in self._jobs.values()
                if job["status"] in _TERMINAL
                and newest_by_branch.get(job["branch_id"]) != job["job_id"]
            ),
            key=lambda job: job["updated_at"],
        )
        while len(self._jobs) >= self.max_retained_jobs and removable:
            job = removable.pop(0)
            self._jobs.pop(job["job_id"], None)
            self._cancel_events.pop(job["job_id"], None)
            self._job_path(job["job_id"]).unlink(missing_ok=True)

    def _job_path(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise DemoJobError("invalid job_id")
        return self.storage_directory / f"{job_id}.json"

    @staticmethod
    def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": job["job_id"],
            "branch_id": job["branch_id"],
            "status": job["status"],
            "percent": job["percent"],
            "minute": job["minute"],
            "target_minute": job["target_minute"],
            "error": job["error"],
            "execution_mode": job["execution_mode"],
            "scenario_id": job["scenario_id"],
            "seed": job["seed"],
            "manifest": {
                "schema_version": 1,
                "execution_mode": job["execution_mode"],
                "scenario_id": job["scenario_id"],
                "seed": job["seed"],
                "agent_count": job["agent_count"],
                "duration_minutes": job["duration_minutes"],
                "scenario_source": job["scenario_source"],
                "scenario_hash": job.get("scenario_hash"),
                "scenario_family_hash": job.get("scenario_family_hash"),
                "run_id": job.get("run_id"),
            },
            "updated_at": job["updated_at"],
        }


def _percent(start_minute: int, target_minute: int, minute: int) -> int:
    total = target_minute - start_minute
    if total <= 0:
        return 100
    return max(0, min(100, round((minute - start_minute) * 100 / total)))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
