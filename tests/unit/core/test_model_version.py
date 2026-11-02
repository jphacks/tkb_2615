"""The whole-model version a Checkpoint records and how a save of another version is read.

Runs of another model version through the timeline, the Project API and the CLI are
checked in tests/integration/test_checkpoint_versions.py.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from sheltersim.contracts.policies import model_signatures
from sheltersim.core import model_version
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.model_version import (
    ENGINE_VERSION_KEY,
    RNG_ALGORITHM_KEY,
    ModelVersionStatus,
    ViewOnlyRunError,
    model_version_status,
    same_models,
    saved_point_view,
)
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState


def _rehashed(document: dict) -> dict:
    # The body hash covers the state hash, so the state hash comes first.
    document["state_hash"] = Checkpoint.document_hashes(document)[0]
    document["checkpoint_hash"] = Checkpoint.document_hashes(document)[1]
    return document


def _before_the_version_was_recorded(document: dict) -> dict:
    for key in (ENGINE_VERSION_KEY, RNG_ALGORITHM_KEY):
        document["model_versions"].pop(key)
    return _rehashed(document)


def test_model_signatures_record_the_engine_version_and_the_rng_algorithm(
    small_replay_scenario,
) -> None:
    signatures = model_signatures(small_replay_scenario().model_configs)
    assert signatures[ENGINE_VERSION_KEY] == model_version.ENGINE_VERSION
    assert signatures[RNG_ALGORITHM_KEY] == CounterRNG.algorithm_id == "sha256-keyed-v1"
    world = WorldState.from_scenario(small_replay_scenario())
    assert Checkpoint.capture(world).model_versions[ENGINE_VERSION_KEY] == (
        model_version.ENGINE_VERSION
    )


def test_a_save_without_the_engine_items_was_computed_by_engine_1(
    small_replay_scenario, monkeypatch
) -> None:
    monkeypatch.setattr(model_version, "ENGINE_VERSION", "1")
    scenario = small_replay_scenario()
    world = WorldState.from_scenario(scenario)
    installed = model_signatures(scenario.model_configs)
    unrecorded = {key: value for key, value in installed.items() if not key.startswith("engine:")}
    assert model_version_status(unrecorded, scenario.model_configs, {}).current
    assert same_models(unrecorded, installed)
    assert not same_models(unrecorded, {**installed, ENGINE_VERSION_KEY: "2"})

    document = _before_the_version_was_recorded(Checkpoint.capture(world).to_dict())
    assert Checkpoint.from_dict(document).restore_world().minute_index == 0

    monkeypatch.setattr(model_version, "ENGINE_VERSION", "2")
    status = model_version_status(unrecorded, scenario.model_configs, {})
    assert (status.recorded_version, status.installed_version) == ("1", "2")
    assert status.differences == (ENGINE_VERSION_KEY,)
    assert status.message().startswith("このRunは旧モデル（版 1）で計算されました。")


def test_a_save_of_another_version_is_refused_for_ticks_and_restored_for_viewing(
    small_replay_scenario, monkeypatch
) -> None:
    world = WorldState.from_scenario(small_replay_scenario())
    document = Checkpoint.capture(world).to_dict()
    document["model_versions"][ENGINE_VERSION_KEY] = "0"
    _rehashed(document)

    with pytest.raises(ViewOnlyRunError, match="run_model_view_only") as refused:
        Checkpoint.from_dict(deepcopy(document))
    assert refused.value.status.recorded_version == "0"

    checkpoint, (viewed, metrics) = Checkpoint._load(deepcopy(document), view=True)
    view = saved_point_view(viewed)
    assert view is not None and not view.status.current and not view.rounded
    assert viewed.state_digest() == checkpoint.state_hash
    assert metrics.snapshots == []
    with pytest.raises(ViewOnlyRunError):
        checkpoint.restore_state()
    assert saved_point_view(checkpoint.restore_view()[0]).saved_minute == 0

    # A changed document is reported as changed, not as another model version.
    document["model_versions"][ENGINE_VERSION_KEY] = "00"
    with pytest.raises(Exception, match="integrity hash mismatch"):
        Checkpoint.from_dict(deepcopy(document))
    with pytest.raises(Exception, match="integrity hash mismatch"):
        Checkpoint._load(deepcopy(document), view=True)


def test_configs_the_installed_code_cannot_sign_are_viewed_as_another_model(
    small_replay_scenario,
) -> None:
    world = WorldState.from_scenario(small_replay_scenario())
    checkpoint = Checkpoint.capture(world)
    broken = deepcopy(checkpoint.world_state)
    broken["model_configs"]["policy_registry"] = {"behavior": "no-such-model"}
    unsignable = Checkpoint(**{**checkpoint._document_view(), "world_state": broken})
    with pytest.raises((KeyError, ValueError)):
        unsignable.model_status()
    status = unsignable.model_status(view=True)
    assert not status.current and status.differences == ("model_signatures",)


def test_the_legacy_queue_migration_is_continued_today_and_viewed_after_a_bump(
    generated_scenario_with_split_queues, monkeypatch
) -> None:
    from sheltersim.domain.facility_queues import (
        FACILITY_QUEUES_LEGACY_MODE_KEY,
        FACILITY_QUEUES_STATE_KEY,
    )

    world = WorldState.from_scenario(generated_scenario_with_split_queues)
    document = Checkpoint.capture(world).to_dict()
    document["world_state"]["model_state"].pop(FACILITY_QUEUES_STATE_KEY)
    document["model_versions"].pop("simulator:facility_queues_v1")
    document = _before_the_version_was_recorded(document)
    monkeypatch.setattr(model_version, "ENGINE_VERSION", "1")

    # Unchanged for a Run of the installed version: migrated and continued.
    checkpoint = Checkpoint.from_dict(deepcopy(document))
    assert checkpoint.model_status().migration == "legacy_queue"
    restored = checkpoint.restore_world()
    assert FACILITY_QUEUES_LEGACY_MODE_KEY in restored.model_state

    # The switch that takes older shapes off the tick path (step 4, L10).
    with monkeypatch.context() as off:
        off.setattr(model_version, "LEGACY_SHAPES_TICKABLE", False)
        with pytest.raises(ViewOnlyRunError) as refused:
            Checkpoint.from_dict(deepcopy(document))
        assert refused.value.status.differences == ("legacy:facility_queues_fifo",)
        assert "以前の保存の形（facility_queues_fifo）" in refused.value.status.message()
        _, (viewed, _) = Checkpoint._load(deepcopy(document), view=True)
        assert FACILITY_QUEUES_LEGACY_MODE_KEY in viewed.model_state
        # A continued legacy Run keeps the FIFO mode in its later saves.
        with pytest.raises(ViewOnlyRunError):
            Checkpoint.capture(restored)

    # After the engine version is raised it is view-only, still migrated for viewing.
    monkeypatch.setattr(model_version, "ENGINE_VERSION", "2")
    with pytest.raises(ViewOnlyRunError):
        Checkpoint.from_dict(deepcopy(document))
    _, (viewed, _) = Checkpoint._load(deepcopy(document), view=True)
    assert viewed.model_state[FACILITY_QUEUES_LEGACY_MODE_KEY]["mode"] == (
        "pre_independent_queue_checkpoint"
    )
    assert saved_point_view(viewed).status.migration == "legacy_queue"


def test_information_state_schema_1_is_an_older_shape_only_with_the_switch_off(
    monkeypatch,
) -> None:
    installed = model_version.engine_signatures()
    recorded = dict(installed)
    state = {"information_spread_v1": {"schema_version": 1}}
    assert model_version_status(recorded, {}, state, installed).current
    monkeypatch.setattr(model_version, "LEGACY_SHAPES_TICKABLE", False)
    status = model_version_status(recorded, {}, state, installed)
    assert status.differences == ("legacy:information_schema_1",)
    assert model_version_status(
        recorded, {}, {"information_spread_v1": {"schema_version": 2}}, installed
    ).current


def test_another_policy_signature_names_the_differing_items() -> None:
    status = ModelVersionStatus(
        recorded_version="1",
        installed_version="1",
        differences=("model_suite", "policy:behavior", "policy:queue", "state:x"),
    )
    assert not status.current
    assert status.message().startswith(
        "このRunは今と異なるモデルの版（model_suite、policy:behavior、policy:queue ほか）"
    )
    assert status.to_dict()["view_only"] is True
