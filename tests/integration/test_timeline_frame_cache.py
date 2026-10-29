"""Frames cached by the checkpoint writer must equal reconstructed frames.

On a Run of the shared generated Scenario, so each frame takes people's positions
from the floor plan, as the Project screen's frames do. Until #137 this ran on
scenario_100_agents, whose frames place people at the zones' display_x and display_y.

That the cache stays within the storage capacity is checked in
tests/unit/storage/test_frames.py.
"""

import gzip
import json

import pytest

from sheltersim.inspection.frames import world_frame
from sheltersim.storage.frames import FrameCache


def reconstructed(timeline, branch_id, minute):
    reference = timeline.store.branch_reference("p", "r", branch_id)
    world = timeline.seek(branch_id, minute).world
    return world_frame(
        world, run_id="r", branch_id=branch_id, computed_until=reference.computed_until
    )


def assert_on_the_floor_plan(frame):
    """Every person has a floor-plan position, and someone is walking on a passage."""
    assert frame["geometry"] is not None
    assert {agent["position_kind"] for agent in frame["agents"]} == {"physical"}
    assert any(agent["edge_id"] for agent in frame["agents"])


def files(store):
    """Saved files; SQLite's shared-memory and log files beside the history index are
    opened by every reader of its write-ahead log and are not saved data."""
    return sorted(
        (str(p), p.stat().st_mtime_ns)
        for p in store.root.rglob("*")
        if p.is_file() and not p.name.endswith(("-shm", "-wal"))
    )


def test_saved_boundaries_are_served_from_the_writer_cache(
    tmp_path, monkeypatch, saved_timeline, generated_scenario
):
    store, timeline = saved_timeline(tmp_path, generated_scenario)
    timeline.advance("main", 60)
    expected = {minute: reconstructed(timeline, "main", minute) for minute in (0, 30, 60)}
    assert_on_the_floor_plan(expected[30])
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


def test_unsaved_minutes_and_unusable_entries_fall_back_to_reconstruction(
    tmp_path, saved_timeline, generated_scenario
):
    store, timeline = saved_timeline(tmp_path, generated_scenario)
    timeline.advance("main", 30)
    reference = store.branch_reference("p", "r", "main")
    unsaved = reconstructed(timeline, "main", 17)
    assert_on_the_floor_plan(unsaved)
    assert timeline.frame("main", 17) == unsaved
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


def test_fork_point_frame_is_cached_for_the_child(
    tmp_path, monkeypatch, saved_timeline, generated_scenario
):
    _, timeline = saved_timeline(tmp_path, generated_scenario)
    timeline.advance("main", 30)
    timeline.fork("main", 13, "child")
    expected = reconstructed(timeline, "child", 13)
    assert_on_the_floor_plan(expected)
    monkeypatch.setattr(timeline, "seek", lambda *_: pytest.fail("child fork frame not cached"))
    assert timeline.frame("child", 13) == expected
    assert expected["branch_id"] == "child"


def test_cache_write_failure_keeps_the_durable_checkpoint(
    tmp_path, monkeypatch, saved_timeline, generated_scenario
):
    store, timeline = saved_timeline(tmp_path, generated_scenario)

    def full(*_args, **_kwargs):
        raise OSError("injected cache write failure")

    monkeypatch.setattr(FrameCache, "put", full)
    assert timeline.advance("main", 30).world.minute_index == 30
    assert store.branch_reference("p", "r", "main").computed_until == 30
    monkeypatch.undo()
    expected = reconstructed(timeline, "main", 30)
    assert_on_the_floor_plan(expected)
    assert timeline.frame("main", 30) == expected
