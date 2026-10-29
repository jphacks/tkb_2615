"""Seeking a minute between saved Checkpoints, and a fork there, are exact.

On the shared generated Scenario, so the replay from the saved Checkpoint walks
people on the floor plan, as the Project screen's seek does. Until #137 this ran
on scenario_100_agents, which has neither a floor plan nor facility awareness.
"""

import copy
import json

import pytest

from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.movement import MOVEMENT_STATE_KEY
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import TimelineError, TimelineStore, _MetricSink
from sheltersim.storage.projects import ProjectStore, digest
from sheltersim.web.projects import ProjectAPI

CHECKPOINT_INTERVAL = 20
# Between the Checkpoints at 20 and 40, while people walk on the floor plan.
SEEK_MINUTE = 28
COMPUTED_UNTIL = 45


def test_non_checkpoint_seek_and_noop_fork_are_exact(tmp_path, generated_scenario, saved_timeline):
    scenario = generated_scenario
    store, timeline = saved_timeline(tmp_path, scenario)
    timeline.advance("main", COMPUTED_UNTIL, checkpoint_interval=CHECKPOINT_INTERVAL)
    reference = WorldState.from_scenario(scenario)
    TickEngine(
        reference,
        phase_handler=OfflineSimulationController(reference, _MetricSink(MetricsRecorder())),
    ).run(steps=SEEK_MINUTE)
    assert reference.model_state[MOVEMENT_STATE_KEY]["journeys"]
    assert timeline.seek("main", SEEK_MINUTE).world.canonical_json() == reference.canonical_json()
    parent_before = store.load_branch("p", "r", "main")[1].to_json()
    timeline.fork("main", SEEK_MINUTE, "child")
    child = timeline.advance("child", COMPUTED_UNTIL)
    assert (
        child.world.canonical_json() == timeline.seek("main", COMPUTED_UNTIL).world.canonical_json()
    )
    assert store.load_branch("p", "r", "main")[1].to_json() == parent_before
    with pytest.raises(TimelineError, match="computed range"):
        timeline.seek("main", COMPUTED_UNTIL + 1)


@pytest.mark.parametrize(("minute", "with_program"), [(30, False), (40, False), (40, True)])
def test_fork_saves_the_child_of_the_source_checkpoint(
    tmp_path, generated_scenario, add_toilets_program, minute, with_program
):
    """A fork records the source's hashes and saves the child of its Checkpoint (#46).

    The expected child is built step by step from a separate seek, as forks were
    before #46 reused one Checkpoint for all of it.
    """
    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = generated_scenario
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    api = ProjectAPI(store, recover=False)
    try:
        api.post(
            ["p", "runs"],
            {
                "run_id": "r",
                "scenario_id": scenario.id,
                "scenario_version": 1,
                "execution_mode": "offline",
            },
        )
        api.submit("p", "r", "main", COMPUTED_UNTIL)
        api.wait_idle(timeout=120)
        timeline = TimelineStore(store, "p", "r")
        parent_before = store.load_branch("p", "r", "main")[1].to_json()
        source = timeline.seek("main", minute)
        source_checkpoint = source.checkpoint()
        expected = BranchRuntime.from_checkpoint(
            source_checkpoint, branch_id="child", parent_branch_id="main"
        )
        expected.runtime_state["fork_origin"] = {
            "branch_id": "main",
            "minute": minute,
            "state_hash": digest(source.world.to_dict()),
            "checkpoint_hash": source_checkpoint.checkpoint_hash,
        }
        request = {"minute": minute, "child_id": "child", "approved": True}
        if with_program:
            program = add_toilets_program(start_minute=minute)
            expected.runtime_state.pop("replay_source", None)
            expected.apply_effect_program(program)
            request["program"] = program.model_dump(mode="json")
            request["approved_program_hash"] = digest(program.model_dump(mode="json"))
        else:
            head = store.load_branch("p", "r", "main")[1]
            expected.runtime_state["replay_source"] = copy.deepcopy(head.runtime_state["llm"])

        result = api.post(["p", "runs", "r", "branches", "main", "fork"], request)
    finally:
        api.close()

    _, saved = store.load_branch("p", "r", "child")
    assert saved.to_json() == expected.checkpoint().to_json()
    assert ("replay_source" in saved.runtime_state) is not with_program
    assert result == {
        "branch_id": "child",
        "minute": minute,
        "state_hash": digest(expected.world.to_dict()),
    }
    assert store.load_branch("p", "r", "main")[1].to_json() == parent_before


def test_a_forked_branch_index_inherits_the_rows_a_fresh_ingest_derives(
    tmp_path, generated_scenario, saved_timeline
):
    """Draft for #223: rows copied from the parent equal the rows derived from scratch."""
    import sqlite3

    from sheltersim.storage.history_index import HistoryIndex

    store, timeline = saved_timeline(tmp_path, generated_scenario)
    timeline.advance("main", COMPUTED_UNTIL, checkpoint_interval=CHECKPOINT_INTERVAL)
    timeline.fork("main", SEEK_MINUTE, "child")
    _, head = store.load_branch("p", "r", "child")
    index = HistoryIndex(store, "p", "r")
    index.ingest("fresh", head)
    db = sqlite3.connect(index.path)

    def rows(branch):
        tables = {}
        for table in ("records", "entities", "tokens", "generations"):
            found = db.execute(f"SELECT * FROM {table} WHERE branch=?", (branch,)).fetchall()
            tables[table] = sorted(row[1:] for row in found)
        heads = db.execute("SELECT minute,counts FROM heads WHERE branch=?", (branch,))
        tables["heads"] = heads.fetchall()
        return tables

    inherited, fresh = rows("child"), rows("fresh")
    assert inherited == fresh
    assert len(inherited["records"]) > 0


def test_frames_read_while_a_save_is_indexed_match_the_frames_after(
    tmp_path, monkeypatch, saved_timeline
):
    """#259: a save moves the branch pointer before it indexes the new head; a frame
    read in between is the same as one read after the save."""
    from sheltersim.storage.history_index import HistoryIndex

    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    store, timeline = saved_timeline(tmp_path)
    timeline.advance("main", 30)
    minutes = (0, 15, 30, 45, 60)
    during = {}
    original = HistoryIndex.ingest

    def ingest(self, branch, checkpoint, **options):
        if checkpoint.simulation_minute == 60:
            assert store.branch_reference("p", "r", "main").computed_until == 60
            reader = TimelineStore(store, "p", "r")
            during.update({minute: reader.frame("main", minute) for minute in minutes})
        return original(self, branch, checkpoint, **options)

    monkeypatch.setattr(HistoryIndex, "ingest", ingest)
    timeline.advance("main", 60)
    monkeypatch.setattr(HistoryIndex, "ingest", original)
    assert set(during) == set(minutes)
    for minute in minutes:
        assert during[minute] == TimelineStore(store, "p", "r").frame("main", minute)
