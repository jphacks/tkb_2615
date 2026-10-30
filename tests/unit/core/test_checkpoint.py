"""Checkpoint capture, save and load, and the versions and hashes it refuses.

That a Run resumed from a Checkpoint continues exactly as the uninterrupted Run
is checked in tests/integration/test_checkpoint_resume.py.
"""

from __future__ import annotations

import hashlib
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

    assert checkpoint.schema_version == restored_checkpoint.schema_version == 2
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

    unknown_checkpoint = dict(document, schema_version=3)
    with pytest.raises(CheckpointError, match="checkpoint schema"):
        Checkpoint.from_dict(unknown_checkpoint)

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
    world_digests, body_digests = [], []
    original_world = CanonicalEncoder.world_tree_digest
    original_body = CanonicalEncoder.body_tree_digest

    def world_digest(self, value):
        world_digests.append(1)
        return original_world(self, value)

    def body_digest(self, value):
        body_digests.append(1)
        return original_body(self, value)

    monkeypatch.setattr(CanonicalEncoder, "world_tree_digest", world_digest)
    monkeypatch.setattr(CanonicalEncoder, "body_tree_digest", body_digest)
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
    document["state_hash"] = encoder.world_tree_digest(world)
    body = {key: value for key, value in document.items() if key != "checkpoint_hash"}
    document["checkpoint_hash"] = encoder.body_tree_digest(body)
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_object(hashes: dict[str, str]) -> str:
    return _sha256(b"\0sheltersim-tree-object\0" + _canonical(hashes))


def _tree_list(items: list) -> str:
    chunks = [_sha256(_canonical(items[i : i + 128])) for i in range(0, len(items), 128)]
    return _sha256(b"\0sheltersim-tree-list\0" + _canonical([len(items), chunks]))


def _tree_v2_document() -> dict:
    return {
        "schema_version": 2,
        "world_state_schema_version": 5,
        "scenario_id": "s",
        "simulation_minute": 3,
        "state_hash": "0" * 64,
        "model_versions": {"m": "1"},
        "world_state": {
            "event_log": [{"item": index, "text": "あ"} for index in range(300)],
            "model_state": {
                "body_state_v1": {
                    "agents": {"a": 1},
                    "history": [{"minute": index} for index in range(129)],
                },
                "social_network_v1": {"relationships": [1, 2]},
            },
            "resources": {"water": 1.5},
        },
        "metrics_state": {"schema_version": 4, "snapshots": [[index] for index in range(128)]},
        "runtime_state": {"history": [1]},
        "checkpoint_hash": "0" * 64,
    }


def test_the_v2_hashes_are_the_tree_of_the_documented_rule() -> None:
    """The rule written out here is the saved format: a change of it needs a new version."""
    document = _tree_v2_document()
    world = document["world_state"]
    model_state = world["model_state"]
    state_hash = _tree_object(
        {
            "event_log": _tree_list(world["event_log"]),
            "model_state": _tree_object(
                {
                    "body_state_v1": _tree_object(
                        {
                            "agents": _sha256(_canonical(model_state["body_state_v1"]["agents"])),
                            "history": _tree_list(model_state["body_state_v1"]["history"]),
                        }
                    ),
                    "social_network_v1": _sha256(_canonical(model_state["social_network_v1"])),
                }
            ),
            "resources": _sha256(_canonical(world["resources"])),
        }
    )
    document["state_hash"] = state_hash
    body_hashes = {
        key: _sha256(_canonical(value))
        for key, value in document.items()
        if key not in {"world_state", "metrics_state", "checkpoint_hash"}
    }
    body_hashes["world_state"] = state_hash
    body_hashes["metrics_state"] = _tree_object(
        {
            "schema_version": _sha256(b"4"),
            "snapshots": _tree_list(document["metrics_state"]["snapshots"]),
        }
    )
    assert Checkpoint.document_hashes(document) == (state_hash, _tree_object(body_hashes))


def _changed(change) -> dict:
    document = _tree_v2_document()
    change(document)
    return document


def _swap(items: list, first: int, second: int) -> None:
    items[first], items[second] = items[second], items[first]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["world_state"]["event_log"][0].update(item=-1),
        lambda d: d["world_state"]["event_log"][127].update(item=-1),
        lambda d: d["world_state"]["event_log"][128].update(item=-1),
        lambda d: d["world_state"]["event_log"][-1].update(text="い"),
        lambda d: d["world_state"]["event_log"].pop(),
        lambda d: d["world_state"]["event_log"].pop(0),
        lambda d: d["world_state"]["event_log"].append({"item": 300}),
        lambda d: _swap(d["world_state"]["event_log"], 127, 128),
        lambda d: _swap(d["world_state"]["event_log"], 10, 20),
        lambda d: d["world_state"]["model_state"]["body_state_v1"]["history"][128].update(minute=0),
        lambda d: d["world_state"]["model_state"]["body_state_v1"]["agents"].update(a=2),
        lambda d: d["world_state"]["model_state"]["social_network_v1"]["relationships"].append(3),
        lambda d: d["world_state"]["resources"].update(water=1.0),
        lambda d: d["world_state"].update(extra=None),
        lambda d: d["world_state"].update(event_log={}),
        lambda d: d["metrics_state"]["snapshots"].__setitem__(5, [-1]),
        lambda d: d["runtime_state"]["history"].append(2),
        lambda d: d["model_versions"].update(m="2"),
        lambda d: d.update(simulation_minute=4),
    ],
)
def test_any_change_of_a_document_changes_the_v2_hash(change) -> None:
    before = Checkpoint.document_hashes(_tree_v2_document())
    assert Checkpoint.document_hashes(_changed(change))[1] != before[1]


def test_the_v2_hash_of_spliced_bytes_is_the_hash_of_the_document(monkeypatch) -> None:
    """Hashes from cached item bytes, by Rust or Python, equal those of a plain document."""
    document = _tree_v2_document()
    expected = Checkpoint.document_hashes(document)
    for kernel in (native.chunk_hashes, lambda *_: None):
        monkeypatch.setattr(native, "chunk_hashes", kernel)
        encoder = CanonicalEncoder()
        world = dict(document["world_state"])
        raws = [_canonical(item) for item in world["event_log"]]
        world["event_log"] = list(world["event_log"])
        encoder.splice(world["event_log"], raws, world)
        assert encoder.world_tree_digest(world) == expected[0]
        # A second digest reuses the chunk hashes: only a new chunk's items are read.
        repeat = CanonicalEncoder(encoder.chunk_keys)
        repeat.splice(world["event_log"], raws, world)
        monkeypatch.setattr(native, "chunk_hashes", lambda *_: pytest.fail("hashed again"))
        assert repeat.world_tree_digest(world) == expected[0]


def _v1_document(checkpoint: Checkpoint) -> dict:
    """The document as Checkpoint v1 saved it: both hashes over all of its canonical JSON."""
    document = json.loads(checkpoint.to_json())
    document["schema_version"] = 1
    document["state_hash"] = _sha256(_canonical(document["world_state"]))
    del document["checkpoint_hash"]
    document["checkpoint_hash"] = _sha256(_canonical(document))
    return document


def test_a_v1_checkpoint_loads_and_verifies_under_its_own_rule(live_checkpoint_world) -> None:
    """Old saves stay readable; what is saved from them is the current version."""
    current = Checkpoint.capture(live_checkpoint_world)
    document = _v1_document(current)
    assert document["state_hash"] != current.state_hash

    loaded = Checkpoint.from_json(json.dumps(document))
    assert (loaded.schema_version, loaded.state_hash) == (1, document["state_hash"])
    assert json.loads(loaded.to_json()) == document
    assert Checkpoint.document_hashes(document) == (
        document["state_hash"],
        document["checkpoint_hash"],
    )
    world = loaded.restore_world()
    assert world.canonical_json() == live_checkpoint_world.canonical_json()

    again = Checkpoint.capture(world)
    assert again.schema_version == 2
    assert again.state_hash == current.state_hash


def test_a_tampered_v1_or_relabelled_checkpoint_fails_closed(live_checkpoint_world) -> None:
    current = Checkpoint.capture(live_checkpoint_world)
    document = _v1_document(current)
    tampered = json.loads(json.dumps(document))
    tampered["world_state"]["resources"]["water"] = 500
    with pytest.raises(CheckpointError, match="WorldState integrity hash mismatch"):
        Checkpoint.from_dict(tampered)
    tampered = json.loads(json.dumps(document))
    tampered["runtime_state"]["added"] = 1
    with pytest.raises(CheckpointError, match="^checkpoint integrity hash mismatch"):
        Checkpoint.from_dict(tampered)
    # The hash rule follows the version, so changing the version breaks the hashes.
    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(dict(document, schema_version=2))
    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(dict(current.to_dict(), schema_version=1))


@pytest.mark.skipif(not native.available(), reason="needs the Rust EventLog")
def test_a_changed_saved_event_record_fails_closed_under_the_tree_hash(
    live_checkpoint_world,
) -> None:
    checkpoint = Checkpoint.capture(live_checkpoint_world)
    document = _with_saved_event_log(checkpoint, _canonical)
    events = document["world_state"]["event_log"]
    assert len(events)
    item = json.loads(events.raws[0])
    item["detail"] = "changed"
    events.raws[0] = _canonical(item)
    with pytest.raises(CheckpointError, match="WorldState integrity hash mismatch"):
        Checkpoint._load(document)
