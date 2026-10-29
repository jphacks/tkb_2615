"""A computation run in its own process (#48) saves what a worker thread saves, can be
cancelled, refuses a Run another process owns, and leaves a killed run interrupted."""

from __future__ import annotations

import multiprocessing
import time

import pytest

from sheltersim.storage.projects import StorageError
from sheltersim.storage.telemetry import RunTelemetryStore
from sheltersim.web.projects import ProjectAPI


def _duration(store):
    run = store.run("p", "r")
    return store.scenario("p", run["scenario_id"], run["scenario_version"]).duration_minutes


def _computed(tmp_path, saved_timeline, worker, target):
    store, _ = saved_timeline(tmp_path)
    api = ProjectAPI(store, recover=False, worker=worker)
    try:
        api.submit("p", "r", "main", target, measure_performance=True)
        api.wait_idle(timeout=300)
    finally:
        api.close()
    return store


def test_a_process_saves_the_same_head_as_a_thread(tmp_path, monkeypatch, saved_timeline):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    thread = _computed(tmp_path / "thread", saved_timeline, "thread", 30)
    process = _computed(tmp_path / "process", saved_timeline, "process", 30)
    assert process.run("p", "r")["status"] == thread.run("p", "r")["status"] == "paused"
    expected = thread.branch_reference("p", "r", "main")
    head = process.branch_reference("p", "r", "main")
    assert (head.computed_until, head.checkpoint_hash) == (30, expected.checkpoint_hash)
    # The process is the Run's own, so its peak memory is the Run's.
    execution = RunTelemetryStore(process, "p", "r").read()["executions"][-1]
    assert execution["memory_attribution_status"] == "measured"
    assert execution["run_peak_memory_bytes"] == execution["process_peak_rss_bytes"] > 0
    threaded = RunTelemetryStore(thread, "p", "r").read()["executions"][-1]
    assert threaded["memory_attribution_status"] == "unavailable"


def test_a_process_run_can_be_cancelled(tmp_path, monkeypatch, saved_timeline):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_timeline(tmp_path)
    duration = _duration(store)
    api = ProjectAPI(store, recover=False, worker="process")
    try:
        api.submit("p", "r", "main", duration)
        assert store.run("p", "r")["status"] == "running"
        with pytest.raises(StorageError, match="busy"):
            api.submit("p", "r", "main", duration)
        api.post(["p", "runs", "r", "cancel"], {})
        api.wait_idle(timeout=300)
    finally:
        api.close()
    assert store.run("p", "r")["status"] == "paused"
    assert store.branch_reference("p", "r", "main").computed_until < duration


def test_a_run_another_process_owns_is_refused(tmp_path, monkeypatch, saved_timeline):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_timeline(tmp_path)
    status = store.run("p", "r")["status"]
    lease = store.claim_run("p", "r")
    api = ProjectAPI(store, recover=False, worker="process")
    try:
        with pytest.raises(StorageError, match="run_worker_busy"):
            api.submit("p", "r", "main", 30)
        assert store.run("p", "r")["status"] == status
        assert not multiprocessing.active_children()
    finally:
        store.release_run(lease)
        api.close()
    # Once the lease is free the Run computes.
    api = ProjectAPI(store, recover=False, worker="process")
    try:
        api.submit("p", "r", "main", 30)
        api.wait_idle(timeout=300)
    finally:
        api.close()
    assert store.branch_reference("p", "r", "main").computed_until == 30


def test_a_killed_process_leaves_the_run_interrupted(tmp_path, monkeypatch, saved_timeline):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_timeline(tmp_path)
    head = store.branch_reference("p", "r", "main")
    duration = _duration(store)
    api = ProjectAPI(store, recover=False, worker="process")
    try:
        api.submit("p", "r", "main", duration)
        (worker,) = multiprocessing.active_children()
        worker.kill()
        api.wait_idle(timeout=300)
        assert store.run("p", "r")["status"] == "interrupted"
        # The killed process held the lease; it ended with the process.
        api.submit("p", "r", "main", head.computed_until + 30)
        api.wait_idle(timeout=300)
    finally:
        api.close()
    assert store.branch_reference("p", "r", "main").computed_until == head.computed_until + 30


def _forked(tmp_path, saved_timeline, worker, **options):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    status = store.run("p", "r")["status"]
    api = ProjectAPI(store, recover=False, worker=worker)
    try:
        result = api.fork(
            "p", "r", "main", {"minute": 15, "child_id": "child", "approved": True}, **options
        )
        api.wait_idle(timeout=300)
        run = api.get(["p", "runs", "r"], {})
    finally:
        api.close()
    assert run["status"] == status
    return store, result, run


def test_a_process_fork_creates_the_child_a_thread_fork_creates(
    tmp_path, monkeypatch, saved_timeline
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    thread, expected, _ = _forked(tmp_path / "thread", saved_timeline, "thread")
    process, result, run = _forked(tmp_path / "process", saved_timeline, "process")
    # The request returns at once; the outcome is the run's last fork.
    assert result == {"status": "running", "branch_id": "child", "minute": 15}
    assert run["last_fork"] == {"status": "created", **expected}
    assert (
        process.branch_reference("p", "r", "child").checkpoint_hash
        == thread.branch_reference("p", "r", "child").checkpoint_hash
    )


def test_a_failed_process_fork_is_reported_and_creates_nothing(
    tmp_path, monkeypatch, saved_timeline
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _, run = _forked(tmp_path, saved_timeline, "process", expected_state_hash="0" * 64)
    assert run["last_fork"]["status"] == "failed"
    assert run["last_fork"]["error"] == "source state changed; preview the intervention again"
    assert [branch["branch_id"] for branch in run["branches"]] == ["main"]


def test_a_process_fork_checks_what_needs_no_restore_at_once(tmp_path, monkeypatch, saved_timeline):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    api = ProjectAPI(store, recover=False, worker="process")
    try:
        with pytest.raises(StorageError, match="computed range"):
            api.fork("p", "r", "main", {"minute": 45, "child_id": "c", "approved": True})
        with pytest.raises(StorageError, match="already exists"):
            api.fork("p", "r", "main", {"minute": 15, "child_id": "main", "approved": True})
        assert not multiprocessing.active_children()
        assert "last_fork" not in api.get(["p", "runs", "r"], {})
    finally:
        api.close()


def _second_run(store):
    """A second Run of the same Scenario beside Run r, at minute 0."""
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.branch import BranchRuntime
    from sheltersim.experiments.timeline import TimelineStore

    run = store.run("p", "r")
    store.create_run("p", "r2", run["scenario_id"], run["scenario_version"])
    scenario = store.scenario("p", run["scenario_id"], run["scenario_version"])
    TimelineStore(store, "p", "r2").record(
        BranchRuntime.from_world(WorldState.from_scenario(scenario))
    )


def test_two_runs_compute_at_once_up_to_the_limit(tmp_path, monkeypatch, saved_timeline):
    """Different Runs compute in parallel processes, up to max_jobs (#48)."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_timeline(tmp_path)
    _second_run(store)
    duration = _duration(store)
    api = ProjectAPI(store, recover=False, worker="process", max_jobs=2)
    try:
        api.submit("p", "r", "main", 30)
        api.submit("p", "r2", "main", 30)
        assert {store.run("p", run)["status"] for run in ("r", "r2")} == {"running"}
        assert len(multiprocessing.active_children()) == 2
        api.wait_idle(timeout=300)
    finally:
        api.close()
    heads = {store.branch_reference("p", run, "main").checkpoint_hash for run in ("r", "r2")}
    assert len(heads) == 1
    assert {store.branch_reference("p", run, "main").computed_until for run in ("r", "r2")} == {30}
    api = ProjectAPI(store, recover=False, worker="process", max_jobs=1)
    try:
        api.submit("p", "r", "main", duration)
        with pytest.raises(StorageError, match="workers are busy"):
            api.submit("p", "r2", "main", 60)
        with pytest.raises(StorageError, match="workers are busy"):
            api.fork("p", "r2", "main", {"minute": 30, "child_id": "c", "approved": True})
        assert store.run("p", "r2")["status"] == "paused"
    finally:
        api.close()


def test_a_run_forks_and_cancels_while_another_computes(tmp_path, monkeypatch, saved_timeline):
    """A computation holds only its own Run: another Run forks, and cancelling one Run
    leaves the other computing."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    _second_run(store)
    duration = _duration(store)
    api = ProjectAPI(store, recover=False, worker="process", max_jobs=2)
    try:
        api.submit("p", "r2", "main", duration)
        with pytest.raises(StorageError, match="pause background calculation"):
            api.fork("p", "r2", "main", {"minute": 0, "child_id": "c", "approved": True})
        api.fork("p", "r", "main", {"minute": 15, "child_id": "c", "approved": True})
        api.post(["p", "runs", "r2", "cancel"], {})
        api.wait_idle(timeout=300)
        assert api.get(["p", "runs", "r"], {})["last_fork"]["status"] == "created"
    finally:
        api.close()
    assert store.run("p", "r2")["status"] == "paused"
    assert store.branch_reference("p", "r", "c").computed_until == 15


def _with_child(tmp_path, saved_timeline):
    """Run r with main and a child forked from it at minute 0."""
    store, _ = saved_timeline(tmp_path)
    api = ProjectAPI(store, recover=False)
    try:
        api.fork("p", "r", "main", {"minute": 0, "child_id": "child", "approved": True})
    finally:
        api.close()
    return store


def test_two_branches_of_a_run_compute_at_once(tmp_path, monkeypatch, saved_timeline):
    """Branches of one Run compute in parallel processes; the Run is running while
    either is and each branch shows its own status (#48)."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store = _with_child(tmp_path, saved_timeline)
    api = ProjectAPI(store, recover=False, worker="process", max_jobs=2)
    try:
        api.submit("p", "r", "main", 30)
        api.submit("p", "r", "child", 30)
        assert len(multiprocessing.active_children()) == 2
        run = api.get(["p", "runs", "r"], {})
        assert run["status"] == "running"
        assert {branch["branch_id"]: branch["status"] for branch in run["branches"]} == {
            "main": "running",
            "child": "running",
        }
        api.wait_idle(timeout=300)
    finally:
        api.close()
    run = store.run("p", "r")
    assert run["status"] == "paused"
    assert {
        branch["branch_id"]: (branch["status"], branch["computed_until"])
        for branch in run["branches"]
    } == {
        "main": ("paused", 30),
        "child": ("paused", 30),
    }


def test_a_branch_forks_and_cancels_while_another_branch_computes(
    tmp_path, monkeypatch, saved_timeline
):
    """A fork needs only its source branch to be still; cancelling one branch leaves
    the other computing (#48)."""
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store = _with_child(tmp_path, saved_timeline)
    duration = _duration(store)
    api = ProjectAPI(store, recover=False, worker="process", max_jobs=3)
    try:
        api.submit("p", "r", "child", duration)
        api.submit("p", "r", "main", duration)
        with pytest.raises(StorageError, match="pause background calculation"):
            api.fork("p", "r", "main", {"minute": 0, "child_id": "c2", "approved": True})
        api.post(["p", "runs", "r", "cancel"], {"branch_id": "main"})
        # The branch's job ends once its process has ended, after it saved its status.
        deadline = time.monotonic() + 300
        while ("p", "r", "main") in api._jobs and time.monotonic() < deadline:
            time.sleep(0.1)
        assert store.branch_status("p", "r", "main") == "paused"
        assert store.branch_status("p", "r", "child") == "running"
        api.fork("p", "r", "main", {"minute": 0, "child_id": "c2", "approved": True})
        api.post(["p", "runs", "r", "cancel"], {})
        api.wait_idle(timeout=300)
        assert api.get(["p", "runs", "r"], {})["last_fork"]["status"] == "created"
    finally:
        api.close()
    statuses = {branch["branch_id"]: branch["status"] for branch in store.run("p", "r")["branches"]}
    assert statuses == {"main": "paused", "child": "paused", "c2": "paused"}
    assert store.run("p", "r")["status"] == "paused"
