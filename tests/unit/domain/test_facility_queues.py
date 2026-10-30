"""A checkpoint taken before independent queues existed keeps the old FIFO mode.

The queues themselves are run in tests/integration/test_facility_queues.py.
"""

from __future__ import annotations

from copy import deepcopy

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState


def test_prequeue_checkpoint_keeps_legacy_fifo_mode_and_can_be_saved_again(
    generated_scenario_with_split_queues,
) -> None:
    from sheltersim.domain.facility_queues import (
        FACILITY_QUEUES_LEGACY_MODE_KEY,
        FACILITY_QUEUES_STATE_KEY,
    )

    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    document = Checkpoint.capture(world).to_dict()
    legacy_world = deepcopy(document["world_state"])
    legacy_world["model_state"].pop(FACILITY_QUEUES_STATE_KEY)
    document["world_state"] = legacy_world
    document["model_versions"].pop("simulator:facility_queues_v1")
    document["state_hash"] = Checkpoint.document_hashes(document)[0]
    document["checkpoint_hash"] = Checkpoint.document_hashes(document)[1]

    restored = Checkpoint.from_dict(document).restore_world()
    assert restored.model_state[FACILITY_QUEUES_LEGACY_MODE_KEY]["mode"] == (
        "pre_independent_queue_checkpoint"
    )
    assert FACILITY_QUEUES_STATE_KEY not in restored.model_state
    new_checkpoint = Checkpoint.capture(restored)
    reloaded = Checkpoint.from_json(new_checkpoint.to_json()).restore_world()
    assert FACILITY_QUEUES_STATE_KEY not in reloaded.model_state
