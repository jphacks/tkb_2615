"""A generated Scenario with distance-sampled proximity contacts, the option the Project
form offers, records contacts and relationships and keeps its saved model versions.

The save is refused while #187 is open: the proximity model signature is taken for an
email address.
"""

from __future__ import annotations

import pytest

from sheltersim.events.models import EventStatus
from sheltersim.storage.projects import StorageError

END = 60
PROXIMITY = "distance-sampled-proximity-v1"


@pytest.mark.xfail(
    strict=True,
    raises=StorageError,
    reason="#187: the proximity model signature is redacted, so the Checkpoint is refused",
)
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
