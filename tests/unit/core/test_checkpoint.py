"""Checkpoint capture, save and load, and the versions and hashes it refuses.

That a Run resumed from a Checkpoint continues exactly as the uninterrupted Run
is checked in tests/integration/test_checkpoint_resume.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.paths import FAILURE_DAY3, SMALL_REPLAY

from sheltersim import native
from sheltersim.core.checkpoint import Checkpoint, CheckpointError, CheckpointSchedule
from sheltersim.core.checkpoint_cache import CanonicalEncoder
from sheltersim.core.saved_items import SavedItems
from sheltersim.core.world import WorldState
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.scenario import ScenarioSpec


def test_registered_state_proposals_are_atomic_and_versions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    from sheltersim.contracts import runtime as contracts
    from sheltersim.contracts.workspace import StateDescriptor
    from sheltersim.core.checkpoint import Checkpoint, CheckpointError
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.scenario import ScenarioSpec

    namespace = "qa_body_v1"
    descriptor = StateDescriptor(
        namespace=namespace,
        implementation_id="qa-body",
        implementation_version="1",
        schema_version=1,
        fields={"sleep_debt": {"unit": "minute", "public": True}},
    )

    def initial(world: WorldState) -> dict:
        return {"schema_version": 1, "agents": {key: {"sleep_debt": 0} for key in world.agents}}

    def validate(world: WorldState, state: dict) -> None:
        if set(state["agents"]) != set(world.agents) or any(
            row["sleep_debt"] < 0 for row in state["agents"].values()
        ):
            raise ValueError("invalid synthetic debt state")

    def project(state: dict, entity_id: str) -> dict:
        return {"sleep_debt": state["agents"][entity_id]["sleep_debt"]}

    monkeypatch.setitem(contracts._CONTRACTS, namespace, (descriptor, validate, initial, project))
    document = ScenarioSpec.load(SMALL_REPLAY).model_dump(mode="json")
    document["model_configs"]["state_contracts"] = {
        "schema_version": 1,
        "namespaces": {
            namespace: {
                "implementation_id": "qa-body",
                "implementation_version": "1",
                "schema_version": 1,
            }
        },
    }
    world = WorldState.from_scenario(ScenarioSpec.model_validate(document))
    original = deepcopy(world.to_dict())
    assert contracts.entity_projections(world)
    assert world.to_dict() == original
    invalid = deepcopy(world.model_state[namespace])
    invalid["agents"][next(iter(world.agents))]["sleep_debt"] = -1
    with pytest.raises(ValueError):
        contracts.apply_model_proposal(
            world,
            contracts.ModelStateProposal(
                namespace=namespace, model_id="qa-body", minute=0, proposed_state=invalid
            ),
        )
    assert world.to_dict() == original
    checkpoint = Checkpoint.capture(world)
    monkeypatch.setitem(
        contracts._CONTRACTS,
        namespace,
        (descriptor.model_copy(update={"implementation_version": "2"}), validate, initial, project),
    )
    with pytest.raises(CheckpointError, match="migration"):
        checkpoint.restore_world()


def test_paired_restore_constructs_one_validated_detached_world(monkeypatch) -> None:
    world = WorldState.from_scenario(ScenarioSpec.load(FAILURE_DAY3))
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    checkpoint = Checkpoint.capture(world, metrics=metrics)
    original = WorldState.from_dict.__func__
    calls = []

    def restore(cls, document, **kwargs):
        calls.append(document["minute_index"])
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(restore))
    restored_world, restored_metrics = checkpoint.restore_state()
    assert calls == [0]
    assert restored_world.canonical_json() == world.canonical_json()
    assert restored_metrics.to_dict() == metrics.to_dict()
    restored_world.metric_state["test_only"] = 1.0
    restored_metrics.snapshots.clear()
    again_world, again_metrics = checkpoint.restore_state()
    assert "test_only" not in again_world.metric_state
    assert again_metrics.to_dict() == metrics.to_dict()


def test_capture_restored_returns_the_restore_it_checked_detached_from_both(
    live_checkpoint_world, monkeypatch
) -> None:
    world = live_checkpoint_world
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    original = WorldState.from_dict.__func__
    calls = []

    def restore(cls, document, **kwargs):
        calls.append(document["minute_index"])
        return original(cls, document, **kwargs)

    monkeypatch.setattr(WorldState, "from_dict", classmethod(restore))
    checkpoint, restored_world, restored_metrics = Checkpoint.capture_restored(
        world, metrics=metrics, runtime_state={"branch": {"command_log": []}}
    )
    # The capture's own restore check is the only restore (#46).
    assert calls == [0]
    saved = checkpoint.to_json()
    assert (
        saved
        == Checkpoint.capture(
            world, metrics=metrics, runtime_state={"branch": {"command_log": []}}
        ).to_json()
    )
    expected_world, expected_metrics = checkpoint.restore_state()
    assert restored_world.canonical_json() == expected_world.canonical_json()
    assert restored_metrics.to_dict() == expected_metrics.to_dict()
    before = world.canonical_json()
    restored_world.model_state["behavior-model"]["adaptive_state"].append(1.0)
    restored_world.metric_state["model_accumulator"] = 0.0
    restored_metrics.snapshots.clear()
    assert checkpoint.to_json() == saved
    assert world.canonical_json() == before


def test_checkpoint_has_independent_schema_and_captures_every_runtime_state(
    tmp_path: Path, live_checkpoint_world
) -> None:
    world = live_checkpoint_world
    metrics = MetricsRecorder()
    metrics.capture(world, minute=0)
    CheckpointSchedule(interval_minutes=30)

    checkpoint = Checkpoint.capture(
        world,
        metrics=metrics,
        runtime_state={"decision_tape_cursor": 7, "policy_state": {"recent": ["x"]}},
    )
    path = tmp_path / "minute-0.checkpoint.json"
    checkpoint.save(path)
    restored_checkpoint = Checkpoint.load(path)
    restored = restored_checkpoint.restore_world(
        expected_model_versions=restored_checkpoint.model_versions
    )

    assert checkpoint.schema_version == restored_checkpoint.schema_version == 1
    assert checkpoint.world_state_schema_version == world.schema_version == 5
    assert restored.canonical_json() == world.canonical_json()
    assert restored.event_queue.pending() == world.event_queue.pending()
    assert restored.event_log.records == world.event_log.records
    assert restored.active_tasks == world.active_tasks
    assert restored.rng_state == world.rng_state
    assert restored.model_state == world.model_state
    assert restored_checkpoint.runtime_state == {
        "decision_tape_cursor": 7,
        "policy_state": {"recent": ["x"]},
    }
    assert restored_checkpoint.restore_metrics().to_dict() == metrics.to_dict()


def test_checkpoint_fails_closed_for_versions_corruption_and_model_mismatch(
    live_checkpoint_world,
) -> None:
    checkpoint = Checkpoint.capture(live_checkpoint_world)
    document = checkpoint.to_dict()

    old_checkpoint = dict(document, schema_version=2)
    with pytest.raises(CheckpointError, match="checkpoint schema"):
        Checkpoint.from_dict(old_checkpoint)

    extended_checkpoint = dict(document, unexpected_contract_field=True)
    with pytest.raises(CheckpointError, match="fields do not match"):
        Checkpoint.from_dict(extended_checkpoint)

    corrupted = json.loads(checkpoint.to_json())
    corrupted["world_state"]["resources"]["water"] = 500
    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(corrupted)

    wrong_world_version = dict(document, world_state_schema_version=4)
    with pytest.raises(CheckpointError, match="WorldState schema version"):
        Checkpoint.from_dict(wrong_world_version)

    with pytest.raises(CheckpointError, match="model versions"):
        checkpoint.restore_world(expected_model_versions={"behavior": "unexpected"})


def test_capture_hashes_once_and_later_reads_still_detect_mutation(
    monkeypatch, live_checkpoint_world
) -> None:
    import sheltersim.core.checkpoint as module

    world_digests, body_digests = [], []
    original = module._digest

    def digest(value):
        if isinstance(value, dict) and "event_log" in value:
            world_digests.append(1)
        if isinstance(value, dict) and "world_state" in value:
            body_digests.append(1)
        return original(value)

    monkeypatch.setattr(module, "_digest", digest)
    checkpoint = Checkpoint.capture(live_checkpoint_world)
    assert (len(world_digests), len(body_digests)) == (1, 1)
    monkeypatch.undo()

    checkpoint.runtime_state["unexpected"] = 1
    with pytest.raises(CheckpointError, match="^checkpoint integrity hash mismatch"):
        checkpoint.to_dict()
    del checkpoint.runtime_state["unexpected"]
    checkpoint.world_state["resources"]["water"] = 500
    with pytest.raises(CheckpointError, match="WorldState integrity hash mismatch"):
        checkpoint.to_dict()


def test_checkpoint_cadence_is_configurable_and_defaults_to_thirty_minutes() -> None:
    schedule = CheckpointSchedule()

    assert schedule.interval_minutes == 30
    assert schedule.should_capture(0)
    assert schedule.should_capture(60)
    assert not schedule.should_capture(59)
    assert CheckpointSchedule(10).should_capture(20)


def test_verifying_a_checkpoint_encodes_its_world_once_and_restores_a_detached_world(
    monkeypatch, live_checkpoint_world
) -> None:
    """Both hashes come from one encoding of the world, which also gives the restore copy."""
    import sheltersim.core.checkpoint as module
    import sheltersim.core.checkpoint_cache as cache_module

    checkpoint = Checkpoint.capture(live_checkpoint_world)
    encoded = []
    original = cache_module.canonical_bytes

    def counting(value):
        # The world, alone or inside the Checkpoint body.
        world = value.get("world_state") if isinstance(value, dict) else None
        encoded.append(checkpoint.world_state in (value, world))
        return original(value)

    monkeypatch.setattr(module, "canonical_bytes", counting)
    monkeypatch.setattr(cache_module, "canonical_bytes", counting)
    world, _ = checkpoint.restore_state()
    assert encoded.count(True) == 1
    monkeypatch.undo()
    assert world.canonical_json() == live_checkpoint_world.canonical_json()
    saved = checkpoint.to_json()
    world.model_state["behavior-model"]["adaptive_state"].append(1.0)
    world.metric_state["model_accumulator"] = 0.0
    assert checkpoint.to_json() == saved


def _with_saved_event_log(checkpoint: Checkpoint, encode) -> dict:
    """The Checkpoint's document with its EventLog as item bytes, rehashed over them."""
    document = json.loads(checkpoint.to_json())
    world = document["world_state"]
    raws = [encode(item) for item in world["event_log"]]
    world["event_log"] = SavedItems(raws)
    encoder = CanonicalEncoder()
    encoder.splice(world["event_log"], raws, world)
    document["state_hash"] = encoder.digest(world)
    body = {key: value for key, value in document.items() if key != "checkpoint_hash"}
    encoder.contain(body)
    document["checkpoint_hash"] = encoder.digest(body)
    return document


def _canonical(item) -> bytes:
    return json.dumps(
        item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


@pytest.mark.skipif(not native.available(), reason="needs the Rust EventLog")
def test_a_checkpoint_with_saved_event_bytes_restores_without_making_records(
    live_checkpoint_world,
) -> None:
    """B1: the hashes are checked over the item bytes and the EventLog is built from
    them; a record is made only when read, and the restored world is the same."""
    checkpoint = Checkpoint.capture(live_checkpoint_world)
    document = _with_saved_event_log(checkpoint, _canonical)
    assert document["checkpoint_hash"] == checkpoint.checkpoint_hash
    loaded, (world, _) = Checkpoint._load(document)
    count = len(live_checkpoint_world.event_log)
    assert count and world.event_log.unread() == count
    assert loaded.to_json() == checkpoint.to_json()
    assert world.canonical_json() == live_checkpoint_world.canonical_json()


@pytest.mark.skipif(not native.available(), reason="needs the Rust EventLog")
def test_a_saved_event_record_that_is_not_canonical_json_fails_closed(
    live_checkpoint_world,
) -> None:
    """Hashes over item bytes alone would accept any spelling of a record, so each must
    be the canonical JSON a save writes."""
    checkpoint = Checkpoint.capture(live_checkpoint_world)
    spaced = _with_saved_event_log(
        checkpoint, lambda item: json.dumps(item, sort_keys=True).encode()
    )
    with pytest.raises(CheckpointError, match="not canonical JSON"):
        Checkpoint._load(spaced)
    unknown = _with_saved_event_log(
        checkpoint, lambda item: _canonical({**item, "status": "maybe"})
    )
    with pytest.raises(CheckpointError, match="unknown event status"):
        Checkpoint._load(unknown)
