"""The proposal view made from the update phase's view equals a view read from the world.

Runs without the Rust kernels too: the view is not a kernel, and the tests check both the
values and the audit hash, bit for bit, at every tick of Scenarios with and without
messages and beliefs.
"""

from __future__ import annotations

import copy
import struct

from sheltersim.core.engine import TraceLevel
from sheltersim.domain.information import INFORMATION_SPREAD_STATE_KEY
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.behavior import WorldView


def _same_bits(x: object, y: object) -> bool:
    if isinstance(x, float) and isinstance(y, float):
        return struct.pack("<d", x) == struct.pack("<d", y)
    return x == y and type(x) is type(y)


def _assert_same_views(made: WorldView, read: WorldView) -> None:
    assert made.state_hash == read.state_hash
    assert made.minute_index == read.minute_index
    assert made.resource_service_enabled == read.resource_service_enabled
    assert made.autonomy_enabled == read.autonomy_enabled
    assert dict(made.facilities) == dict(read.facilities)
    assert dict(made.zone_members) == dict(read.zone_members)
    assert dict(made.group_members) == dict(read.group_members)
    assert list(made.agents) == list(read.agents)
    for agent_id, agent in made.agents.items():
        other = read.agents[agent_id]
        assert agent.__dict__.keys() == other.__dict__.keys()
        for name, value in agent.__dict__.items():
            if name == "needs":
                for need in value.__dataclass_fields__:
                    assert _same_bits(getattr(value, need), getattr(other.needs, need))
            else:
                assert _same_bits(value, getattr(other, name)), (agent_id, name)


def _compared_ticks(monkeypatch) -> list[WorldView]:
    original = WorldView.after_state_update.__func__
    views: list[WorldView] = []

    def checked(cls, prior, world):
        made = original(cls, prior, world)
        _assert_same_views(made, WorldView.from_world(world))
        views.append(made)
        return made

    monkeypatch.setattr(WorldView, "after_state_update", classmethod(checked))
    return views


def test_the_proposal_view_equals_a_read_view_at_every_tick(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 240
    views = _compared_ticks(monkeypatch)
    run_scenario(ScenarioSpec.model_validate(document), tmp_path, trace_level=TraceLevel.OFF)
    assert [view.minute_index for view in views] == list(range(240))


def test_the_proposal_view_equals_a_read_view_with_messages_and_beliefs(
    generated_previews,
    information_spread_scenario,
    information_spread_config,
    tmp_path,
    monkeypatch,
) -> None:
    document = copy.deepcopy(generated_previews()["scenario"])
    events = information_spread_scenario(duration=60, correction=True)
    document["duration_minutes"] = 60
    document["scheduled_events"] = events.model_dump(mode="json")["scheduled_events"]
    document["scheduled_events"][0]["payload"]["origin_agent_id"] = "A001"
    configs = document["model_configs"]
    configs["information_spread"] = configs["information_spread"] | information_spread_config()
    views = _compared_ticks(monkeypatch)
    worlds = []
    original = WorldView.from_world.__func__

    def remember(cls, world, **options):
        worlds.append(world)
        return original(cls, world, **options)

    monkeypatch.setattr(WorldView, "from_world", classmethod(remember))
    run_scenario(ScenarioSpec.model_validate(document), tmp_path, trace_level=TraceLevel.OFF)
    assert len(views) == 60
    state = worlds[-1].model_state[INFORMATION_SPREAD_STATE_KEY]
    assert state["messages"] and any(state["beliefs"].values())


def test_a_change_after_the_update_phase_would_be_seen_only_through_a_read_view(
    generated_scenario,
) -> None:
    """The proposal view takes the fields the update phase writes from the world."""
    from sheltersim.core.world import WorldState
    from sheltersim.domain.body_state import create_body_state

    world = WorldState.from_scenario(generated_scenario)
    world.model_state.setdefault("body_state_v1", create_body_state(world))
    prior = WorldView.from_world(world)
    agent_id = next(iter(world.agents))
    agent = world.agents[agent_id]
    agent.stress = 0.5 if agent.stress != 0.5 else 0.25
    agent.trust = 0.75 if agent.trust != 0.75 else 0.25
    agent.needs.hunger = 0.5 if agent.needs.hunger != 0.5 else 0.25
    world.model_state["body_state_v1"]["agents"][agent_id]["uncertainty"] = 0.625
    _assert_same_views(WorldView.after_state_update(prior, world), WorldView.from_world(world))
    assert WorldView.after_state_update(prior, world).state_hash != prior.state_hash


def test_a_config_edited_between_ticks_is_seen_by_the_next_phases(generated_scenario) -> None:
    """Parses and routes are kept between ticks only while model_configs keeps its values."""
    from sheltersim.core.engine import TickEngine
    from sheltersim.core.world import WorldState
    from sheltersim.domain.geometry_overlay import _CONFIG_SCOPE, effective_geometry
    from sheltersim.experiments.metrics import MetricsRecorder
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.experiments.timeline import _MetricSink

    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    engine.step()
    parsed = dict(controller._parsed_configs)
    assert parsed
    engine.step()
    assert controller._parsed_configs.keys() == parsed.keys()
    routes = controller._room_routes
    environments = controller._zone_environments

    facility_id, point = next(
        iter(world.model_configs["facility_geometry"]["facility_points"].items())
    )
    moved_x = point["x"] + 0.125
    point["x"] = moved_x
    seen = []
    original = controller._run_phase

    def run_phase(phase, world_, minute, rng):
        assert _CONFIG_SCOPE.get() is controller._parsed_configs
        seen.append(effective_geometry(world_).facility_points[facility_id].x)
        original(phase, world_, minute, rng)

    controller._run_phase = run_phase
    engine.step()
    assert seen and all(value == moved_x for value in seen)
    assert controller._room_routes is not routes
    assert controller._zone_environments is not environments


def test_a_tick_with_kept_inputs_gives_the_state_of_one_with_none(generated_scenario) -> None:
    """Twenty ticks with all that the controller keeps, against dropping it at each phase."""
    from sheltersim.core.engine import TickEngine
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.metrics import MetricsRecorder
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.experiments.timeline import _MetricSink

    def run(drop: bool) -> dict:
        world = WorldState.from_scenario(generated_scenario)
        controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))

        def handler(phase, world_, minute, rng):
            if drop:
                controller._parsed_configs.clear()
                controller._model_selections.clear()
                controller._room_routes = {"routes": {}}
                controller._zone_environments = {}
                controller._view_before_proposals = None
            controller(phase, world_, minute, rng)

        engine = TickEngine(world, phase_handler=handler)
        for _ in range(120):
            engine.step()
        return world.to_dict()

    assert run(False) == run(True)


def test_the_proposal_view_equals_a_read_view_with_full_traces(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    """A trace takes the other update paths, which write nothing more to the world."""
    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 20
    views = _compared_ticks(monkeypatch)
    run_scenario(ScenarioSpec.model_validate(document), tmp_path, trace_level=TraceLevel.FULL)
    assert len(views) == 20


def test_requests_of_one_kind_get_the_reasons_they_get_alone(
    generated_scenario, tmp_path, monkeypatch
) -> None:
    from sheltersim.domain.staff import support_wait_reason_codes
    from sheltersim.experiments import metrics

    document = generated_scenario.model_dump(mode="json")
    document["duration_minutes"] = 360
    compared = []

    def checked(world, request_id, *, minute=None, staff_memo=None):
        shared = support_wait_reason_codes(world, request_id, minute=minute, staff_memo=staff_memo)
        assert shared == support_wait_reason_codes(world, request_id, minute=minute)
        compared.append(shared)
        return shared

    monkeypatch.setattr(metrics, "support_wait_reason_codes", checked)
    run_scenario(ScenarioSpec.model_validate(document), tmp_path, trace_level=TraceLevel.OFF)
    assert compared


def test_swapped_configs_do_not_pile_up_in_the_controller(generated_scenario) -> None:
    """Copy-and-swap handlers replace model_configs; only the current object's parses stay."""
    from copy import deepcopy

    from sheltersim.core.engine import TickEngine
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.metrics import MetricsRecorder
    from sheltersim.experiments.runner import OfflineSimulationController
    from sheltersim.experiments.timeline import _MetricSink

    world = WorldState.from_scenario(generated_scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    engine.step()
    parsed = len(controller._parsed_configs)
    for _ in range(3):
        world.model_configs = deepcopy(world.model_configs)
        engine.step()
    assert len(controller._parsed_configs) == parsed
    assert [known for known, _ in controller._model_selections] == [world.model_configs]
    assert controller._model_selections[0][0] is world.model_configs
