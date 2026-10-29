"""Project API reads that need only a Branch pointer do not decode or load whole
checkpoints; resuming a computation checks its range without loading history and fails
closed on a corrupt head.

The routes that run a computation over a saved Run are checked in
tests/integration/test_pointer_only_reads.py and test_resume_checkpoint_loads.py.
"""

from __future__ import annotations

import pytest

from sheltersim.storage.projects import StorageError
from sheltersim.web.projects import ProjectAPI


def test_report_and_conversations_check_only_the_branch_pointer(
    tmp_path, no_checkpoint_decode, saved_timeline
):
    store, _ = saved_timeline(tmp_path)
    api = ProjectAPI(store, recover=False)
    try:
        assert api.get(["p", "runs", "r", "branches", "main", "report"], {}) == {
            "status": "not_generated"
        }
        assert api.get(["p", "runs", "r", "branches", "main", "conversations"], {})["turns"] == []
        assert no_checkpoint_decode == []
        for action in ("report", "conversations"):
            with pytest.raises(StorageError):
                api.get(["p", "runs", "r", "branches", "missing", action], {})
    finally:
        api.close()


def test_resume_range_is_still_checked_without_loading_history(
    tmp_path, monkeypatch, saved_timeline
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_timeline(tmp_path)
    run = store.run("p", "r")
    scenario = store.scenario("p", run["scenario_id"], run["scenario_version"])
    api = ProjectAPI(store)
    status = store.run("p", "r")["status"]
    try:
        for target in (0, scenario.duration_minutes + 1, "5"):
            with pytest.raises(StorageError, match="uncomputed remaining range"):
                api.submit("p", "r", "main", target)
        with pytest.raises(StorageError):
            api.submit("p", "r", "missing", 5)
        assert store.run("p", "r")["status"] == status
    finally:
        api.close()


def test_corrupt_head_still_fails_closed_in_the_worker(tmp_path, monkeypatch, saved_timeline):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, _ = saved_timeline(tmp_path)
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    for chunk in (store._path("p", "runs", "r", "checkpoints") / "chunks").iterdir():
        chunk.write_bytes(b"corrupt")
    api = ProjectAPI(store)
    try:
        api.submit("p", "r", "main", 5)
        api.wait_idle(timeout=60)
    finally:
        api.close()
    assert store.run("p", "r")["status"] == "failed"
    assert store.branch_reference("p", "r", "main").checkpoint_hash == head
