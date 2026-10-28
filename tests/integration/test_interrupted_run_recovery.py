"""A Run that was computing when the server stopped is marked interrupted at the next
start, and computing it again reaches the same world as an uninterrupted Run.
"""

from __future__ import annotations

import copy
import json

import pytest

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.web.projects import ProjectAPI

END = 60
STOPPED_AT = 20


@pytest.mark.parametrize("status", ["running", "queued"])
def test_an_interrupted_run_resumes_to_the_uninterrupted_world(
    tmp_path, monkeypatch, generated_previews, saved_timeline, status
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    preview = generated_previews(duration_days=END / 1440)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    store, timeline = saved_timeline(tmp_path / "stopped", scenario)
    timeline.advance("main", STOPPED_AT, checkpoint_interval=10)
    # The server stopped while this Run was computing or waiting for the worker.
    store.set_status("p", "r", status)

    api = ProjectAPI(store)
    try:
        restarted = api.get(["p", "runs", "r"], {})["status"]
        api.post(["p", "runs", "r", "resume"], {"branch_id": "main", "target_minute": END})
        # The worker is single; this waits for the resume queued above (#143).
        api._pool.submit(lambda: None).result(timeout=120)
        finished = api.get(["p", "runs", "r"], {})["status"]
    finally:
        api.close()
    _, uninterrupted = saved_timeline(tmp_path / "uninterrupted", scenario)

    assert restarted == "interrupted"
    assert finished == "completed"
    assert json.dumps(timeline.seek("main", END).world.to_dict(), sort_keys=True) == json.dumps(
        uninterrupted.advance("main", END).world.to_dict(), sort_keys=True
    )
