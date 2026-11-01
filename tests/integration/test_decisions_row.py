"""What rules/decisions.rs records of a decision: the rows of `_record_outcome` in Python.

The Python step stays the reference (docs/rust-port/lanes/j-decisions.md). The bits over whole
scenarios are compared by benchmarks/rule_shadow; here, on values no scenario reaches.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.population_lifecycle import current_household_id
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.propose import ProposePhase, _agent_view_snapshot
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _IndexedMetricSink, _MetricSink
from sheltersim.policies.behavior import ActionProposal, WorldView
from sheltersim.storage.history_index import HistoryIndex

pytestmark = pytest.mark.skipif(not native.available(), reason="only with the Rust kernels")

STEPS = ("_propose_due", "_observed_state", "_record_outcome")


def _rules() -> Any:
    return native.rules("decisions")


def _without_kernels(monkeypatch: pytest.MonkeyPatch) -> None:
    for step in STEPS:
        monkeypatch.delattr(OfflineSimulationController, step)


def _exact(value: Any) -> Any:
    """Equal only with the same types, the same bits, and the same order of each dict's keys."""
    if type(value) is float:
        return value.hex()
    if isinstance(value, list):
        return [_exact(item) for item in value]
    if isinstance(value, dict):
        return [[key, _exact(item)] for key, item in value.items()]
    return [type(value).__name__, value]


def _indexed_controller(scenario: Any) -> tuple[WorldState, OfflineSimulationController, Any]:
    world = WorldState.from_scenario(scenario)
    sink = _IndexedMetricSink(MetricsRecorder(), world)
    return world, OfflineSimulationController(world, sink), sink


def _step_until_due(world: WorldState, controller: Any, minimum: int = 1) -> tuple[Any, Any]:
    """A world about to propose, with its view and rows: the tick stops before they are applied."""
    engine = TickEngine(world, phase_handler=controller)
    while True:
        engine.step()
        view = native.rules("world_view").view_for_proposals(controller, world, world.minute_index)
        rows = _rules().propose_rows(controller, view, CounterRNG(world.seed))
        if len(rows) >= minimum:
            if hasattr(controller.artifacts, "observations"):
                controller.artifacts.observations.clear()
            return view, rows


def _record(
    controller: Any, row: Any, observed: Any, *, target: Any = None, committed: bool = True
) -> None:
    _rules().record_outcome(
        controller,
        row,
        target_id=target,
        next_decision_at=row.next_decision_at,
        committed=committed,
        observed_state=observed,
    )


def test_a_run_gives_the_rows_and_world_of_python(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    TickEngine(world, phase_handler=controller).run(steps=90)
    rows = list(sink.observations)
    assert any(type(row) is bytes for row in rows)
    assert all(type(row) is bytes for row in rows if type(row) is not dict)

    with monkeypatch.context() as patch:
        _without_kernels(patch)
        other, python, other_sink = _indexed_controller(generated_scenario)
        TickEngine(other, phase_handler=python).run(steps=90)
    assert all(type(row) is dict for row in other_sink.observations)
    assert [_exact(json.loads(row) if type(row) is bytes else row) for row in rows] == [
        _exact(row) for row in other_sink.observations
    ]
    assert world.canonical_json() == other.canonical_json()


def test_a_tick_makes_no_proposal_object(
    generated_scenario: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    made: list[Any] = []
    propose = OfflineSimulationController._propose_due

    def keep(self: Any, view: Any, rng: Any) -> Any:
        rows = propose(self, view, rng)
        made.extend(rows)
        return rows

    monkeypatch.setattr(OfflineSimulationController, "_propose_due", keep)
    TickEngine(world, phase_handler=controller).run(steps=20)
    assert made
    assert not any(row.has_object for row in made)
    assert all(type(row) is bytes for row in sink.observations if type(row) is not dict)


def test_a_row_answers_as_its_proposal_and_makes_it_once(generated_scenario: Any) -> None:
    world, controller, _sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    python = controller.decisions.propose_due(view, CounterRNG(world.seed))
    row = rows[0]
    proposal = python[0]
    assert not row.has_object
    for name in (
        "agent_id",
        "action",
        "next_decision_at",
        "target_id",
        "minute_index",
        "observed_state_hash",
    ):
        assert getattr(row, name) == getattr(proposal, name)
    assert row.action is proposal.action
    assert not row.has_object
    assert row.candidates == proposal.candidates
    assert row.has_object
    made = row.materialize()
    assert type(made) is ActionProposal and made == proposal
    assert row.materialize() is made
    assert row.with_outcome("X", 10**6) == proposal.with_outcome("X", 10**6)
    with pytest.raises(AttributeError):
        row.nothing_of_the_kind  # noqa: B018


def test_an_observed_row_reads_as_the_snapshot(generated_scenario: Any) -> None:
    world, controller, _sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    observed = _rules().observed_row(controller, view, rows[0].agent_id)
    expected = _agent_view_snapshot(view.agents[rows[0].agent_id])
    assert _exact(dict(observed)) == _exact(expected)
    assert observed["agent_id"] == rows[0].agent_id


def test_a_sink_in_python_gets_a_dict_and_a_silent_one_nothing(generated_scenario: Any) -> None:
    class Other(_IndexedMetricSink):
        pass

    world = WorldState.from_scenario(generated_scenario)
    other = Other(MetricsRecorder(), world)
    controller = OfflineSimulationController(world, other)
    view, rows = _step_until_due(world, controller)
    assert type(_rules().observed_row(controller, view, rows[0].agent_id)) is dict
    with pytest.raises(native.Unsupported, match="sink"):
        _record(controller, rows[0], {})
    assert other.observations == []

    silent = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    assert type(_rules().observed_row(silent, view, rows[0].agent_id)) is not dict
    _record(silent, rows[0], None, committed=False)
    assert world.model_state["agent_dynamics_v1"]["agents"][rows[0].agent_id]["last_action"] == (
        rows[0].action.value
    )


def test_a_view_python_made_is_left_to_python(generated_scenario: Any) -> None:
    world, controller, _sink = _indexed_controller(generated_scenario)
    _step_until_due(world, controller)
    view = WorldView.from_world(world)
    assert "_rust_view" not in view.__dict__
    with pytest.raises(native.Unsupported, match="Python's"):
        _rules().propose_rows(controller, view, CounterRNG(1))
    with pytest.raises(native.Unsupported, match="Python's"):
        _rules().observed_row(controller, view, "A001")


def test_what_the_kernel_refuses_appends_nothing(generated_scenario: Any) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    observed = _rules().observed_row(controller, view, row.agent_id)
    version = row.policy_version
    with pytest.raises(native.Unsupported, match="Python object"):
        _record(controller, row.materialize(), observed)
    with pytest.raises(native.Unsupported, match="Python object"):
        _record(controller, row, dict(observed))
    with pytest.raises(native.Unsupported):
        _rules().record_outcome(
            controller,
            row,
            target_id=None,
            next_decision_at=row.minute_index,
            committed=True,
            observed_state=observed,
        )
    with pytest.raises(native.Unsupported):
        _rules().record_outcome(
            controller,
            row,
            target_id=None,
            next_decision_at=row.next_decision_at,
            committed=1,
            observed_state=observed,
        )
    sink.policy_sources[version] = 1
    with pytest.raises(native.Unsupported):
        _record(controller, row, observed)
    del sink.policy_sources[version]
    world.agents[row.agent_id].needs.fatigue = 1
    with pytest.raises(native.Unsupported):
        _record(controller, row, observed)
    assert sink.observations == []


def test_the_python_step_takes_rows_with_the_same_outcome_and_error(
    generated_scenario: Any,
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    observed = _rules().observed_row(controller, view, row.agent_id)
    with pytest.raises(ValueError, match="invalid decision timing"):
        ProposePhase._record_outcome(
            controller,
            row,
            target_id=None,
            next_decision_at=row.minute_index,
            committed=True,
            observed_state=observed,
        )
    ProposePhase._record_outcome(
        controller,
        row,
        target_id=None,
        next_decision_at=row.next_decision_at,
        committed=True,
        observed_state=observed,
    )
    assert type(sink.observations[-1]) is dict
    assert _exact(sink.observations[-1]["observed_state"]) == _exact(dict(observed))


@pytest.mark.parametrize(
    "presence",
    [
        {"people": {"@": {"household_id": "H"}}},
        {"people": {"@": {"household_id": None}}},
        {"people": {"@": {}}},
        {"people": {}},
        {},
        {"people": {"@": "not a dict"}},
        "not a dict",
    ],
)
def test_the_household_is_the_python_rules(generated_scenario: Any, presence: Any) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    text = json.dumps(presence).replace("@", row.agent_id)
    world.model_state["population_presence_v1"] = json.loads(text)
    _record(controller, row, _rules().observed_row(controller, view, row.agent_id))
    assert json.loads(sink.observations[-1])["household_id"] == current_household_id(
        world, row.agent_id
    )


@pytest.mark.parametrize(
    "presence", [{"people": ["a list"]}, {"people": {"@": {"household_id": 3}}}]
)
def test_a_household_only_python_can_read_is_left_to_it(
    generated_scenario: Any, presence: Any
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    text = json.dumps(presence).replace("@", row.agent_id)
    world.model_state["population_presence_v1"] = json.loads(text)
    with pytest.raises(native.Unsupported):
        _record(controller, row, _rules().observed_row(controller, view, row.agent_id))
    assert sink.observations == []


@pytest.mark.parametrize(("size", "status"), [(9000, "recorded"), (20000, "omitted_size_limit")])
def test_a_long_observed_state_is_measured_by_python(
    generated_scenario: Any, size: int, status: str
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    _step_until_due(world, controller)
    agent = next(iter(world.agents.values()))
    agent.target_id = "x" * size
    view = native.rules("world_view").view_for_proposals(controller, world, world.minute_index)
    observed = _rules().observed_row(controller, view, agent.id)
    rows = _rules().propose_rows(controller, view, CounterRNG(world.seed))
    assert rows
    row = rows[0]
    long_view = _rules().observed_row(controller, view, agent.id)
    with pytest.raises(native.Unsupported, match="long"):
        _record(controller, row, long_view)
    assert sink.observations == []
    ProposePhase._record_outcome(
        controller,
        row,
        target_id=None,
        next_decision_at=row.next_decision_at,
        committed=True,
        observed_state=observed,
    )
    assert sink.observations[-1]["observed_state_status"] == status


def test_a_lifecycle_kernel_refusal_after_the_row_runs_the_python_rule_once(
    generated_scenario: Any,
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    observed = _rules().observed_row(controller, view, row.agent_id)
    world.id_counters["event"] = 2**62 + 1
    before = len(world.event_log)
    _record(controller, row, observed)
    assert len(sink.observations) == 1 and type(sink.observations[0]) is bytes
    assert world.id_counters["event"] > 2**62 + 1
    assert len(world.event_log) > before
    assert row.has_object


def test_a_memory_kernel_refusal_after_the_row_runs_the_python_rule_once(
    generated_scenario: Any,
) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    observed = _rules().observed_row(controller, view, row.agent_id)
    record = world.model_state["agent_dynamics_v1"]["agents"][row.agent_id]
    record["last_decision_minute"] = "not a minute"
    assert world._core.mirror("agent_dynamics_v1").tainted is not None
    _record(controller, row, observed)
    assert len(sink.observations) == 1
    assert record["last_decision_minute"] == row.minute_index


def test_an_uncommitted_decision_opens_nothing(generated_scenario: Any) -> None:
    world, controller, sink = _indexed_controller(generated_scenario)
    view, rows = _step_until_due(world, controller)
    row = rows[0]
    observed = _rules().observed_row(controller, view, row.agent_id)
    before = len(world.event_log)
    _record(controller, row, observed, committed=False)
    assert len(world.event_log) == before and len(sink.observations) == 1


def test_a_trace_keeps_the_documents_of_python(
    generated_scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = copy.deepcopy(generated_scenario.model_dump(mode="json"))
    document["duration_minutes"] = 60
    scenario = ScenarioSpec.model_validate(document)
    for level in (TraceLevel.SUMMARY, TraceLevel.FULL):
        run_scenario(scenario, tmp_path / f"rust-{level.value}", trace_level=level)
        with monkeypatch.context() as patch:
            _without_kernels(patch)
            run_scenario(scenario, tmp_path / f"python-{level.value}", trace_level=level)
        rust = (tmp_path / f"rust-{level.value}" / "decisions.jsonl").read_bytes()
        assert (
            rust and rust == (tmp_path / f"python-{level.value}" / "decisions.jsonl").read_bytes()
        )


def test_an_index_reads_a_row_of_bytes_as_its_dict(
    generated_scenario: Any,
    tmp_path: Path,
    saved_timeline: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def records(store_path: Path) -> list[Any]:
        store, timeline = saved_timeline(store_path, generated_scenario)
        timeline.advance("main", 30, checkpoint_interval=15)
        index = HistoryIndex(store, "p", "r")
        with index.connect() as db:
            return [
                tuple(row)
                for row in db.execute(
                    "SELECT kind, key, minute, event_id, cause, source, event_type, status, payload"
                    " FROM records ORDER BY kind, key"
                )
            ]

    rust = records(tmp_path / "rust")
    with monkeypatch.context() as patch:
        _without_kernels(patch)
        python = records(tmp_path / "python")
    assert rust and any(row[0] == "actions" for row in rust)
    assert rust == python
