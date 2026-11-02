"""Checkpoints of older versions: of the Checkpoint format and of the model.

A Run saved as Checkpoint v1 still loads, replays, resumes, forks and compares. Its
Checkpoints verify under the v1 hash rule (the SHA-256 of the whole document) and what
is saved after them is the current version, with the state a Run saved only in the
current version has at the same minute.

A Run computed with another model version is view-only (core/model_version.py,
docs/rust-port/lanes/l9-view-only.md): its saved points are shown as saved, a minute
between them shows the saved point before it and says so, nothing is computed again,
no Provider is called and no random value is drawn; continuing, forking, submitting
and applying an EffectProgram fail with run_model_view_only. The Run is
fixtures/saved_runs/engine-v1-small, saved before the model version was recorded.
"""

from __future__ import annotations

import argparse
import hashlib

import pytest

from sheltersim.core import model_version
from sheltersim.core.checkpoint import Checkpoint, CheckpointError
from sheltersim.core.checkpoint_cache import CanonicalEncoder
from sheltersim.core.engine import TickEngine
from sheltersim.core.model_version import (
    ViewOnlyRunError,
    ViewOnlyWorldError,
    saved_point_view,
)
from sheltersim.core.rng import CounterRNG
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.timeline import TimelineError, TimelineStore, requested_minute
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.checkpoints import CheckpointArchive
from sheltersim.storage.projects import StorageError, digest
from sheltersim.web.projects import ProjectAPI

V1_UNTIL = 60
RESUMED_UNTIL = 90


def _saved_as_v1(saved_timeline, directory, scenario, monkeypatch):
    """A Run computed with the v1 hash rule, as the earlier code saved it."""
    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "CURRENT_SCHEMA_VERSION", 1)
        patch.setattr(CanonicalEncoder, "world_tree_digest", CanonicalEncoder.digest)
        patch.setattr(CanonicalEncoder, "body_tree_digest", CanonicalEncoder.digest)
        store, timeline = saved_timeline(directory, scenario)
        timeline.advance("main", V1_UNTIL)
    return store, timeline


@pytest.fixture
def v1_run(tmp_path, saved_timeline, generated_scenario, monkeypatch):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    return _saved_as_v1(saved_timeline, tmp_path / "v1", generated_scenario, monkeypatch)


@pytest.fixture
def current_run(tmp_path, saved_timeline, generated_scenario):
    store, timeline = saved_timeline(tmp_path / "current", generated_scenario)
    timeline.advance("main", RESUMED_UNTIL)
    return store, timeline


def test_a_v1_head_is_read_and_its_hashes_are_checked(v1_run, tmp_path) -> None:
    store, _ = v1_run
    reference, head = store.load_branch("p", "r", "main")
    assert (head.schema_version, reference.computed_until) == (1, V1_UNTIL)

    document = store.checkpoint_document("p", "r", head.checkpoint_hash)
    assert document["schema_version"] == 1
    assert Checkpoint.document_hashes(document) == (head.state_hash, head.checkpoint_hash)
    fields = store.checkpoint_fields("p", "r", head.checkpoint_hash, ("state_hash",))
    assert fields["state_hash"] == head.state_hash
    assert Checkpoint.from_json(head.to_json()).checkpoint_hash == head.checkpoint_hash

    tampered = head.to_dict()
    tampered["world_state"]["resources"]["water"] = 500
    with pytest.raises(CheckpointError, match="hash mismatch"):
        Checkpoint.from_dict(tampered)

    # The archive checks the hash under the version it holds.
    directory = store._path("p", "runs", "r", "checkpoints")
    manifest = store._read(directory / f"{head.checkpoint_hash}.json")
    assert CheckpointArchive(directory).document(manifest)["schema_version"] == 1
    forged = {**manifest, "checkpoint_hash": "0" * 64}
    with pytest.raises(CheckpointError):
        CheckpointArchive(directory).document(forged)


def test_a_v1_run_replays_and_resumes_as_a_run_saved_only_in_the_current_version(
    v1_run, current_run
) -> None:
    store, timeline = v1_run
    current_store, current_timeline = current_run

    # Seeks replay from the v1 Checkpoints; a state between them is the same state.
    for minute in (0, 30, 45, V1_UNTIL):
        assert (
            timeline.seek("main", minute).world.canonical_json()
            == current_timeline.seek("main", minute).world.canonical_json()
        )

    resumed = timeline.advance("main", RESUMED_UNTIL)
    reference, head = store.load_branch("p", "r", "main")
    assert head.schema_version == Checkpoint.CURRENT_SCHEMA_VERSION
    _, same = current_store.load_branch("p", "r", "main")
    assert head.state_hash == same.state_hash
    assert head.checkpoint_hash == same.checkpoint_hash
    assert resumed.world.canonical_json() == same.restore_world().canonical_json()
    assert reference.computed_until == RESUMED_UNTIL
    # The history before the first new save is still read under the v1 rule.
    assert timeline.seek("main", V1_UNTIL).world.minute_index == V1_UNTIL
    assert timeline.frame("main", V1_UNTIL)["minute"] == V1_UNTIL


def test_a_v1_checkpoint_forks_and_compares(v1_run, current_run) -> None:
    store, timeline = v1_run
    _, saved = store.load_branch("p", "r", "main")

    with pytest.raises(TimelineError, match="source state changed"):
        timeline.fork("main", V1_UNTIL, "stale", expected_state_hash="0" * 64)

    # A preview hashes the world under the current rule, not the v1 hash the head holds.
    source = timeline.seek("main", V1_UNTIL).world
    expected = source.state_digest()
    assert expected != saved.state_hash
    child = timeline.fork("main", V1_UNTIL, "child", expected_state_hash=expected)
    assert child.runtime_state["fork_origin"]["checkpoint_hash"] == saved.checkpoint_hash
    assert child.world.canonical_json() == source.canonical_json()
    _, child_head = store.load_branch("p", "r", "child")
    assert child_head.schema_version == Checkpoint.CURRENT_SCHEMA_VERSION
    assert child_head.state_hash == expected

    timeline.advance("child", V1_UNTIL + 10)
    assert timeline.seek("child", V1_UNTIL).world.canonical_json() == source.canonical_json()

    api = ProjectAPI(store, recover=False)
    try:
        comparison = api.workspace.comparison("p", "r", ["main", "child"], V1_UNTIL)
        assert comparison["shared_origin"] is True
        assert comparison["common_range"] == [V1_UNTIL, V1_UNTIL]
        replay = api.get(
            ["p", "runs", "r", "replay-frames"],
            {"branch_ids": ["main"], "start": [str(V1_UNTIL)], "end": [str(V1_UNTIL)]},
        )
        assert replay["samples"][0]["minute"] == V1_UNTIL
    finally:
        api.close()


def test_a_changed_chunk_of_a_v1_run_is_refused(v1_run) -> None:
    store, _ = v1_run
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    packs = store._path("p", "runs", "r", "checkpoints", "packs")
    for pack in packs.iterdir():
        data = bytearray(pack.read_bytes())
        data[len(data) // 2] ^= 0xFF
        pack.write_bytes(bytes(data))
    with pytest.raises(StorageError):
        TimelineStore(store, "p", "r").store.load_checkpoint("p", "r", head)


# Runs of another model version. The fixture Run saved main at 0, 30 and 60 and child
# (forked at 30) at 30 and 60; its Scenario is 90 minutes long.
VIEW_ONLY = "run_model_view_only"


def _saved_files(store) -> dict[str, str]:
    run = store._path("p", "runs", "r")
    return {
        str(path.relative_to(run)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("checkpoints", "branches")
        for path in sorted((run / folder).rglob("*"))
        if path.is_file() and path.name != "worker-lock"
    } | {"run.json": hashlib.sha256((run / "run.json").read_bytes()).hexdigest()}


@pytest.fixture
def no_computation(monkeypatch):
    """Fail on a tick, a random draw or a Provider call."""

    def refused(*_args, **_kwargs):
        raise AssertionError("a view computed, drew a random value or called a Provider")

    monkeypatch.setattr(TickEngine, "__init__", refused)
    monkeypatch.setattr(CounterRNG, "random", refused)
    monkeypatch.setattr(LLMRouter, "complete", refused)


def test_a_run_saved_before_the_model_version_was_recorded_is_of_engine_1(
    engine_v1_run, monkeypatch
) -> None:
    """With engine 1 installed it continues; its new saves record the version."""
    monkeypatch.setattr(model_version, "ENGINE_VERSION", "1")
    store, timeline = engine_v1_run
    _, head = store.load_branch("p", "r", "main")
    assert not any(key.startswith("engine:") for key in head.model_versions)
    status = timeline.model_status("main")
    assert status.current and status.recorded_version == "1"

    frame = timeline.frame("main", 45)
    assert frame["minute"] == 45
    assert "saved_point_rounding" not in frame and "model_view" not in frame
    assert [requested_minute(world) for world in timeline.iter_worlds("main", 44, 46)] == [
        44,
        45,
        46,
    ]
    timeline.advance("main", 90)
    _, head = store.load_branch("p", "r", "main")
    assert head.model_versions["engine:model"] == "1"
    assert head.model_versions["engine:rng_algorithm"] == "sha256-keyed-v1"
    assert timeline.model_status("main").current


def test_an_older_model_run_is_shown_at_its_saved_points(
    engine_v1_run, newer_engine, no_computation
) -> None:
    store, timeline = engine_v1_run
    saved = _saved_files(store)
    status = timeline.model_status("main")
    assert not status.current
    assert (status.recorded_version, status.installed_version) == ("1", newer_engine)
    assert status.differences == ("engine:model",)
    assert "旧モデル（版 1）" in status.message() and "閲覧のみ" in status.message()

    at_save = timeline.frame("main", 30)
    assert at_save["minute"] == 30 and "saved_point_rounding" not in at_save
    assert at_save["model_view"]["view_only"] is True
    assert at_save["model_view"]["message"] == status.message()

    between = timeline.frame("main", 45)
    assert between["minute"] == 30
    assert between["saved_point_rounding"]["requested_minute"] == 45
    assert between["saved_point_rounding"]["saved_minute"] == 30
    assert "45分" in between["saved_point_rounding"]["message"]
    assert between["state_hash"] == at_save["state_hash"]

    # One world per requested minute: the saved point at or before it, never computed.
    seen = [
        (requested_minute(world), world.minute_index, saved_point_view(world).rounded)
        for world in timeline.iter_worlds("main", 28, 31)
    ]
    assert seen == [(28, 0, True), (29, 0, True), (30, 30, False), (31, 30, True)]

    view = timeline.view_at("child", 50)
    assert (view.world.minute_index, view.view.saved_minute) == (30, 30)
    assert view.view.rounding()["requested_minute"] == 50
    reference = store.branch_reference("p", "r", "child")
    _, head = store.load_view_checkpoint("p", "r", reference.checkpoint_hash)
    assert head[0].minute_index == 60
    # Viewing changes no saved file.
    assert _saved_files(store) == saved


def test_an_older_model_run_is_read_through_the_project_api(
    engine_v1_run, newer_engine, no_computation
) -> None:
    store, _ = engine_v1_run
    saved = _saved_files(store)
    api = ProjectAPI(store, recover=False)
    try:
        frame = api.get(["p", "runs", "r", "branches", "main", "timeline"], {"minute": ["45"]})
        assert frame["minute"] == 30 and frame["saved_point_rounding"]["saved_minute"] == 30

        replay = api.get(
            ["p", "runs", "r", "replay-frames"],
            {"branch_ids": ["main,child"], "start": ["59"], "end": ["60"]},
        )
        first, last = replay["samples"]
        assert [f["minute"] for f in first["frames"]] == [30, 30]
        assert all(f["saved_point_rounding"]["requested_minute"] == 59 for f in first["frames"])
        assert [f["minute"] for f in last["frames"]] == [60, 60]
        assert all("saved_point_rounding" not in f for f in last["frames"])

        comparison = api.get(
            ["p", "runs", "r", "compare"], {"branch_ids": ["main,child"], "minute": ["45"]}
        )
        assert [f["saved_point_rounding"]["saved_minute"] for f in comparison["frames"]] == [
            30,
            30,
        ]

        from sheltersim.contracts.workspace import HistoryQuery
        from sheltersim.web.project_queries import ProjectQueries

        people = ProjectQueries(store).history(
            "p", "r", "main", HistoryQuery(kind="people", agent_id="A001", start=29, end=31)
        )
        assert [(row["minute"], row["exact"]) for row in people["items"]] == [
            (29, False),
            (30, True),
            (31, False),
        ]
        assert people["items"][2]["saved_point_rounding"]["saved_minute"] == 30
        assert people["coverage"] == "saved_points_of_an_older_model_run"

        from sheltersim.web.pdf_report import collect_report

        report = collect_report(api, "p", "r", ["main", "child"], 60)
        assert [branch["minute"] for branch in report["branches"]] == [60, 60]
        with pytest.raises(StorageError, match="pdf_saved_point_required") as between:
            collect_report(api, "p", "r", ["main"], 45)
        assert "30分" in str(between.value)

        compatibility = api.get(["p", "runs", "r", "branches", "main", "compatibility"], {})
        assert compatibility["continuation_model_state_compatible"] is False
        assert compatibility["model_view"]["view_only"] is True
        assert {
            item["component"]: (item["recorded"], item["installed"])
            for item in compatibility["differences"]
        } == {"engine:model": ("1", newer_engine)}
    finally:
        api.close()
    assert _saved_files(store) == saved


def _refused(call) -> str:
    with pytest.raises(StorageError) as refused:
        call()
    assert refused.value.code == VIEW_ONLY
    assert "閲覧のみ" in str(refused.value)
    return str(refused.value)


def test_continuing_forking_submitting_or_applying_to_an_older_model_run_fails(
    engine_v1_run, newer_engine, no_computation, add_toilets_program, tmp_path
) -> None:
    store, timeline = engine_v1_run
    saved = _saved_files(store)
    _refused(lambda: timeline.advance("main", 90))
    _refused(lambda: timeline.fork("main", 30, "new-child"))
    _refused(lambda: timeline.seek("main", 45))
    program = add_toilets_program(start_minute=31)
    _refused(
        lambda: timeline.fork(
            "main",
            30,
            "new-child",
            program=program,
            approved_program_hash=digest(program.model_dump(mode="json")),
        )
    )

    api = ProjectAPI(store, recover=False)
    try:
        _refused(
            lambda: api.post(
                ["p", "runs", "r", "resume"], {"branch_id": "main", "target_minute": 90}
            )
        )
        _refused(
            lambda: api.post(
                ["p", "runs", "r", "branches", "main", "fork"],
                {
                    "minute": 30,
                    "child_id": "new-child",
                    "approved": True,
                    "program": program.model_dump(mode="json"),
                    "approved_program_hash": digest(program.model_dump(mode="json")),
                },
            )
        )
        _refused(
            lambda: api.workspace.intervention(
                "p", "r", "main", {"minute": 30, "program": program.model_dump(mode="json")}
            )
        )
        assert store.run("p", "r")["status"] != "running"
    finally:
        api.close()

    from sheltersim.storage.commands import execute

    root = store._path("p").parent
    for action, extra in (("resume", {}), ("fork", {"child_id": "cli-child"})):
        arguments = argparse.Namespace(
            root=root,
            project_action=action,
            project_id="p",
            run_id="r",
            branch_id="main",
            minute=30 if action == "fork" else 90,
            **extra,
        )
        _refused(lambda arguments=arguments: execute(arguments))
    assert (
        execute(
            argparse.Namespace(
                root=root,
                project_action="frame",
                project_id="p",
                run_id="r",
                branch_id="main",
                minute=45,
            )
        )["saved_point_rounding"]["saved_minute"]
        == 30
    )
    assert _saved_files(store) == saved
    assert not store._path("p", "runs", "r", "branches", "new-child").exists()


def test_a_view_can_not_be_ticked_continued_or_saved(
    engine_v1_run, newer_engine, monkeypatch
) -> None:
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.experiments.timeline import _MetricSink

    _, timeline = engine_v1_run
    view = timeline.view_at("main", 30)
    for attempt in (
        lambda: TickEngine(view.world),
        lambda: OfflineSimulationController(view.world, _MetricSink(view.metrics)),
        lambda: BranchRuntime(
            branch_id="again", parent_branch_id=None, world=view.world, metrics=view.metrics
        ),
        lambda: Checkpoint.capture(view.world, metrics=view.metrics),
    ):
        with pytest.raises(ViewOnlyRunError, match=VIEW_ONLY):
            attempt()

    # A view of a Run of the installed model can't be ticked either.
    monkeypatch.setattr(model_version, "ENGINE_VERSION", "1")
    current = timeline.view_at("main", 45)
    assert current.world.minute_index == 45 and not current.view.rounded
    with pytest.raises(ViewOnlyWorldError):
        TickEngine(current.world)


def test_a_run_computed_with_an_older_engine_version_is_view_only(
    tmp_path, saved_timeline, small_replay_scenario, monkeypatch
) -> None:
    """A Run whose Checkpoints record an engine version older than the installed one."""
    installed = model_version.ENGINE_VERSION
    with monkeypatch.context() as older:
        older.setattr(model_version, "ENGINE_VERSION", "0")
        store, timeline = saved_timeline(tmp_path / "older", small_replay_scenario(90))
        timeline.advance("main", 60)
    status = timeline.model_status("main")
    assert (status.recorded_version, status.installed_version) == ("0", installed)
    assert "旧モデル（版 0）" in status.message()
    assert timeline.frame("main", 45)["saved_point_rounding"]["saved_minute"] == 30
    _refused(lambda: timeline.advance("main", 90))
    head = store.branch_reference("p", "r", "main").checkpoint_hash
    _refused(lambda: store.load_checkpoint("p", "r", head))
    with pytest.raises(ViewOnlyRunError):
        Checkpoint.from_dict(store.checkpoint_document("p", "r", head))
