import pytest

from sheltersim_foundation.core import Checkpoint, CheckpointError, WorldState


def test_checkpoint_restores_independent_state() -> None:
    world = WorldState("scenario", 3, 5, entities={"a": {"needs": 2}})
    checkpoint = Checkpoint.capture(world)
    restored = Checkpoint.from_dict(checkpoint.to_dict()).restore()

    restored.entities["a"]["needs"] = 0
    assert world.entities["a"]["needs"] == 2


def test_checkpoint_detects_modified_state() -> None:
    payload = Checkpoint.capture(WorldState("scenario", 3, 2)).to_dict()
    payload["state"]["minute_index"] = 1

    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(payload)
