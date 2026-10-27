"""Bounded offline background jobs for the local demo.

The job routes over HTTP are checked in tests/e2e/test_demo_jobs_http.py.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from sheltersim.web.demo import DemoError
from sheltersim.web.jobs import DemoJobError, DemoJobManager


def test_job_advances_in_chunks_and_saves_a_live_compute_checkpoint(
    tmp_path: Path, failure_day3_demo, wait_for_job
) -> None:
    demo = failure_day3_demo()
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=2)
    try:
        job = manager.submit("baseline", 10)
        duplicate = manager.submit("baseline", 10)
        completed = wait_for_job(manager, job["job_id"], {"completed"})

        assert duplicate["job_id"] == job["job_id"]
        assert duplicate["duplicate"] is True
        assert completed["status"] == "completed"
        assert completed["percent"] == 100
        assert completed["minute"] == 10
        assert completed["execution_mode"] == "live_compute"
        assert completed["manifest"]["schema_version"] == 1
        assert completed["manifest"]["seed"] == demo.scenario.seed
        branch = demo.state()["branches"][0]
        assert branch["minute"] == 10
        assert branch["status"] == "paused"
        record = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
        assert record["schema_version"] == 1
        assert record["checkpoint"]["simulation_minute"] == 10
    finally:
        manager.close()


def test_branch_exclusivity_rejects_conflicting_jobs_and_mutations(
    tmp_path: Path, failure_day3_demo
) -> None:
    demo = failure_day3_demo()
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=2)
    entered = threading.Event()
    release = threading.Event()
    advance = demo._advance_branch

    def delayed(branch_id, branch, steps):
        entered.set()
        assert release.wait(timeout=3)
        advance(branch_id, branch, steps)

    demo._advance_branch = delayed  # type: ignore[method-assign]
    try:
        job = manager.submit("baseline", 8)
        assert entered.wait(timeout=3)
        assert manager.submit("baseline", 8)["job_id"] == job["job_id"]
        with pytest.raises(DemoJobError, match="different active job"):
            manager.submit("baseline", 9)
        with pytest.raises(DemoError, match="active background job"):
            demo.dispatch("pause", {"branch_id": "baseline"})
    finally:
        release.set()
        manager.close()


def test_job_queue_has_a_configured_active_limit(
    tmp_path: Path, failure_day3_demo, wait_for_job
) -> None:
    demo = failure_day3_demo()
    demo.dispatch("pause", {"branch_id": "baseline"})
    demo.dispatch("fork", {"branch_id": "baseline", "new_branch_id": "staff-plus"})
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=2, max_active_jobs=1)
    entered = threading.Event()
    release = threading.Event()
    advance = demo._advance_branch

    def delayed(branch_id, branch, steps):
        entered.set()
        assert release.wait(timeout=3)
        advance(branch_id, branch, steps)

    demo._advance_branch = delayed  # type: ignore[method-assign]
    try:
        job = manager.submit("baseline", 8)
        assert entered.wait(timeout=3)
        with pytest.raises(DemoJobError, match="queue is full"):
            manager.submit("staff-plus", 8)
        release.set()
        assert wait_for_job(manager, job["job_id"], {"completed"})["percent"] == 100
    finally:
        release.set()
        manager.close()


def test_cancel_stops_at_a_saved_chunk_boundary(
    tmp_path: Path, failure_day3_demo, wait_for_job
) -> None:
    demo = failure_day3_demo()
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=2)
    entered = threading.Event()
    release = threading.Event()
    advance = demo._advance_branch

    def delayed(branch_id, branch, steps):
        entered.set()
        assert release.wait(timeout=3)
        advance(branch_id, branch, steps)

    demo._advance_branch = delayed  # type: ignore[method-assign]
    try:
        job = manager.submit("baseline", 8)
        assert entered.wait(timeout=3)
        assert manager.cancel(job["job_id"])["status"] == "cancelling"
        release.set()
        cancelled = wait_for_job(manager, job["job_id"], {"cancelled"})

        assert cancelled["minute"] == 2
        assert demo.state()["branches"][0]["minute"] == 2
        assert demo.state()["branches"][0]["status"] == "paused"
        saved = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
        assert saved["checkpoint"]["simulation_minute"] == 2
    finally:
        release.set()
        manager.close()


def test_failed_chunk_keeps_the_last_committed_world_state(
    tmp_path: Path, failure_day3_demo, wait_for_job
) -> None:
    demo = failure_day3_demo()
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=2)
    original_hash = demo.branches["baseline"].runtime.world.canonical_json()
    advance = demo._advance_branch

    def fail_after_candidate(branch_id, branch, steps):
        advance(branch_id, branch, steps)
        raise RuntimeError("simulated chunk failure")

    demo._advance_branch = fail_after_candidate  # type: ignore[method-assign]
    try:
        job = manager.submit("baseline", 4)
        failed = wait_for_job(manager, job["job_id"], {"failed"})

        assert "simulated chunk failure" in failed["error"]
        assert demo.branches["baseline"].runtime.world.minute_index == 0
        assert demo.branches["baseline"].runtime.world.canonical_json() == original_hash
        assert demo.state()["branches"][0]["status"] == "paused"
    finally:
        manager.close()


def test_restart_marks_active_job_interrupted_and_restores_last_checkpoint(
    tmp_path: Path, failure_day3_demo, wait_for_job
) -> None:
    demo = failure_day3_demo()
    manager = DemoJobManager(demo, tmp_path, chunk_minutes=2)
    try:
        job = manager.submit("baseline", 4)
        wait_for_job(manager, job["job_id"], {"completed"})
    finally:
        manager.close()

    record_path = next(tmp_path.glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["job"]["status"] = "running"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    recovered_demo = failure_day3_demo(start=False)
    recovered_manager = DemoJobManager(recovered_demo, tmp_path, chunk_minutes=2)
    try:
        interrupted = recovered_manager.get(job["job_id"])
        branch = recovered_demo.state()["branches"][0]
        assert interrupted["status"] == "interrupted"
        assert "server restarted" in interrupted["error"]
        assert branch["minute"] == 4
        assert branch["status"] == "paused"
        assert recovered_demo.state()["execution_mode"] == "live_compute"
    finally:
        recovered_manager.close()
