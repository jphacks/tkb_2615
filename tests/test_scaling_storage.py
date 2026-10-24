"""Storage-scale invariants for the final #122 suite."""

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.checkpoints import CheckpointArchive


def test_compressed_checkpoint_retains_hash_and_reuses_chunks(tmp_path):
    checkpoint = Checkpoint.capture(WorldState.from_scenario(ScenarioSpec.load("fixtures/scenario_100_agents.json")))
    archive = CheckpointArchive(tmp_path)
    manifest = archive.encode(checkpoint)
    first = sorted(tmp_path.rglob("*.json.gz"))
    assert archive.decode(manifest).to_json() == checkpoint.to_json()
    assert archive.encode(checkpoint) == manifest
    assert sorted(tmp_path.rglob("*.json.gz")) == first
