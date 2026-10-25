"""Frames cached by the checkpoint writer must equal reconstructed frames."""

import gzip
import json

import pytest

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.inspection.frames import world_frame
from sheltersim.storage.frames import FrameCache
from sheltersim.storage.projects import ProjectStore, digest


def saved_timeline(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return store, timeline


def reconstructed(timeline, branch_id, minute):
    reference = timeline.store.branch_reference("p", "r", branch_id)
    world = timeline.seek(branch_id, minute).world
    return world_frame(world, run_id="r", branch_id=branch_id,
                       computed_until=reference.computed_until)


def files(store):
    return sorted((str(p), p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file())


def test_saved_boundaries_are_served_from_the_writer_cache(tmp_path, monkeypatch):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 60)
    expected = {minute: reconstructed(timeline, "main", minute) for minute in (0, 30, 60)}
    before = files(store)

    def no_reconstruction(*_args, **_kwargs):
        raise AssertionError("a cached boundary must not restore the checkpoint")

    monkeypatch.setattr(timeline, "seek", no_reconstruction)
    for minute, frame in expected.items():
        served = timeline.frame("main", minute)
        assert served == frame
        # An older boundary reports the branch's current progress, not its own.
        assert served["computed_until"] == 60
    assert files(store) == before


def test_unsaved_minutes_and_unusable_entries_fall_back_to_reconstruction(tmp_path):
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    reference = store.branch_reference("p", "r", "main")
    assert timeline.frame("main", 17) == reconstructed(timeline, "main", 17)
    path = FrameCache(store, "p", "r")._file(reference.checkpoint_hash)
    expected = reconstructed(timeline, "main", 30)
    document = json.loads(gzip.decompress(path.read_bytes()))
    document["frame"]["kpis"]["population"] = -1
    path.write_bytes(gzip.compress(json.dumps(document).encode()))
    assert timeline.frame("main", 30) == expected
    path.write_bytes(b"not gzip")
    assert timeline.frame("main", 30) == expected
    path.unlink()
    assert timeline.frame("main", 30) == expected
    assert not path.exists()


def test_fork_point_frame_is_cached_for_the_child(tmp_path, monkeypatch):
    _, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    timeline.fork("main", 13, "child")
    expected = reconstructed(timeline, "child", 13)
    monkeypatch.setattr(timeline, "seek", lambda *_: pytest.fail("child fork frame not cached"))
    assert timeline.frame("child", 13) == expected
    assert expected["branch_id"] == "child"


def test_cache_write_failure_keeps_the_durable_checkpoint(tmp_path, monkeypatch):
    store, timeline = saved_timeline(tmp_path)

    def full(*_args, **_kwargs):
        raise OSError("injected cache write failure")

    monkeypatch.setattr(FrameCache, "put", full)
    assert timeline.advance("main", 30).world.minute_index == 30
    assert store.branch_reference("p", "r", "main").computed_until == 30
    monkeypatch.undo()
    assert timeline.frame("main", 30) == reconstructed(timeline, "main", 30)


def test_cache_respects_storage_capacity(tmp_path):
    store, timeline = saved_timeline(tmp_path)
    reference = store.branch_reference("p", "r", "main")
    cache = FrameCache(store, "p", "r")
    cache._file(reference.checkpoint_hash).unlink()
    store.max_bytes = store.usage()["bytes"] + 1
    assert cache.put(reference.checkpoint_hash, {"minute": 0}) is False
    assert cache.get(reference.checkpoint_hash) is None
