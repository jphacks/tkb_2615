"""A computation run in its own process (#48) saves what a worker thread saves, can be
cancelled, refuses a Run another process owns, and leaves a killed run interrupted."""

from __future__ import annotations

import multiprocessing

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
