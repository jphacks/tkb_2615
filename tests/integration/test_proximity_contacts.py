"""A generated Scenario with distance-sampled proximity contacts, the option the Project
form offers, records contacts and relationships and keeps its model version in saved
Checkpoints and Run artifacts. The model signature used to be taken for an email
address, which stopped the first save and blanked it in the manifest (#187).
"""

from __future__ import annotations

import json

from sheltersim.core.engine import TraceLevel
from sheltersim.events.models import EventStatus
from sheltersim.experiments.runner import run_scenario

END = 60
PROXIMITY = "distance-sampled-proximity-v1"


def test_proximity_contacts_are_recorded_across_saved_checkpoints(
    tmp_path, monkeypatch, generated_scenario_with, saved_timeline
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    scenario = generated_scenario_with(
        duration_days=END / 1440, social_proximity_policy_id=PROXIMITY
    )
    store, timeline = saved_timeline(tmp_path / "projects", scenario)

    world = timeline.advance("main", END, checkpoint_interval=30).world

    applied = [
        record
        for record in world.event_log.records
        if record.event.event_type == "proximity_contact_update"
        and record.status is EventStatus.APPLIED
    ]
    assert applied, "no proximity contact was recorded"
    assert world.model_state["social_network_v1"]["relationships"]
    _, head = store.load_branch("p", "r", "main")
    assert head.model_versions["operator:social_proximity"].startswith(f"{PROXIMITY}@1")


def test_a_cli_run_records_the_proximity_model_version(tmp_path, generated_scenario_with):
    scenario = generated_scenario_with(
        duration_days=END / 1440, social_proximity_policy_id=PROXIMITY
    )

    run_scenario(scenario, tmp_path / "run", trace_level=TraceLevel.SUMMARY)

    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["policy_versions"]["operator:social_proximity"].startswith(f"{PROXIMITY}@1")
