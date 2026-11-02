"""The read-only rules that moved to Rust last, against the Python rules they stay compared with:
zone_environment_view and its formulas, PersonContext, DistanceSampledProximityPolicy.sample,
the step that schedules what it finds, and the error of a contact that is not an event.

Most of these are selected by the Scenario, so a Run reaches few of them. These tests make the
inputs, which is how a change of 1% in any of the coefficients is seen: every default of a contract
is left out of some document, and every formula is selected, with values on both sides of its
branches.
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from sheltersim import native
from sheltersim.contracts.operations import SupportKind
from sheltersim.contracts.policies import MODEL_DEFINITIONS
from sheltersim.contracts.population import PersonContext
from sheltersim.core.engine import TickEngine
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.domain.environment import (
    ZONE_ENVIRONMENT_STATE_KEY,
    ZoneEnvironmentView,
    zone_environment_view,
)
from sheltersim.domain.psychological_state import LIFE_STATE_PSYCHOLOGY_POLICIES
from sheltersim.domain.social_network import PROXIMITY_CONTACT_SOURCE_ID
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.sample import SamplePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.proximity import DistanceSampledProximityPolicy, ProximityPair
from sheltersim.policies.world_view import WorldView, _fields_of_person

pytestmark = pytest.mark.skipif(not native.available(), reason="only with the Rust kernels")

PROXIMITY = "distance-sampled-proximity-v1"
FORMULAS = {
    "zone_environment": ("zone-temperature-comfort-band-v1", "zone-temperature-midpoint-v1"),
    "zone_rest": ("zone-rest-multiplicative-v1", "zone-rest-limiting-v1"),
    "zone_hygiene": ("zone-hygiene-linear-v1", "zone-hygiene-quadratic-v1"),
    "zone_privacy": ("zone-privacy-threshold-v1", "zone-privacy-proportional-v1"),
}


def _exact(value: Any) -> Any:
    """Equal only with the same types and bits."""
    if value is None or type(value) is bool:
        return value
    if type(value) is float:
        return value.hex()
    if type(value) is int:
        return ["int", value]
    if isinstance(value, str):
        return [type(value).__name__, str(value)]
    if isinstance(value, tuple):
        return ["tuple", *(_exact(item) for item in value)]
    if isinstance(value, Mapping):
        return ["map", *([key, _exact(item)] for key, item in value.items())]
    if hasattr(value, "__dataclass_fields__"):
        return [type(value).__name__, *([k, _exact(v)] for k, v in value.__dict__.items())]
    raise TypeError(type(value))


# ---- Zones ----

_BOUNDS = {
    "noise_rest_efficiency_loss": (0.0, 0.9),
    "min_probability_multiplier": (0.1, 1.0),
    "max_probability_multiplier": (1.0, 2.0),
    "lighting_sleep_efficiency_loss": (0.0, 0.9),
    "ventilation_hygiene_growth_per_minute": (0.0, 0.01),
    "privacy_discontent_per_minute": (0.0, 0.01),
    "temperature_stress_per_degree_minute": (0.0, 0.01),
    "comfortable_min_temperature_c": (-10.0, 40.0),
    "comfortable_max_temperature_c": (-10.0, 50.0),
}
_INT_BOUNDS = {
    "min_rest_decision_minutes": (1, 60),
    "max_rest_decision_minutes": (1, 240),
    "max_active_modifiers_per_zone": (1, 16),
}
_BASELINE_BOUNDS = {
    "ventilation_effectiveness": (0.0, 1.0),
    "lighting_level": (0.0, 1.0),
    "privacy_level": (0.0, 1.0),
    "temperature_c": (-40.0, 60.0),
}
_LEGACY_NEUTRAL = (
    "lighting_sleep_efficiency_loss",
    "ventilation_hygiene_growth_per_minute",
    "privacy_discontent_per_minute",
    "temperature_stress_per_degree_minute",
)


def _config(rng: random.Random, *, broken: bool) -> dict[str, Any]:
    """A document with a random part of the contract's fields, each with a value it holds."""
    config: dict[str, Any] = {}
    for name, (low, high) in _BOUNDS.items():
        if rng.random() < 0.4:
            config[name] = rng.uniform(low, high)
    if config.get("comfortable_min_temperature_c", 18.0) >= config.get(
        "comfortable_max_temperature_c", 26.0
    ):
        config.pop("comfortable_min_temperature_c", None)
        config.pop("comfortable_max_temperature_c", None)
    for name, (low, high) in _INT_BOUNDS.items():
        if rng.random() < 0.3:
            config[name] = rng.randint(low, high)
    if config.get("min_rest_decision_minutes", 1) > config.get("max_rest_decision_minutes", 60):
        del config["min_rest_decision_minutes"]
    version = rng.choice(("zone-environment-v2", "zone-environment-v1", None))
    if version is not None:
        config["policy_version"] = version
    if version != "zone-environment-v1" and rng.random() < 0.5:
        config["baseline_by_zone"] = {
            "hall": {
                name: rng.uniform(low, high)
                for name, (low, high) in _BASELINE_BOUNDS.items()
                if rng.random() < 0.5
            }
        }
    if version == "zone-environment-v1" or (
        version is None and not (set(config) & {*_LEGACY_NEUTRAL, "baseline_by_zone"})
    ):
        # A legacy document holds its neutral values or leaves them out.
        for name in _LEGACY_NEUTRAL:
            if name in config:
                config[name] = 0.0
        config.pop("baseline_by_zone", None)
    if rng.random() < 0.2:
        config["schema_version"] = 1
    if rng.random() < 0.2:
        config["source_type"] = "assumption"
    if rng.random() < 0.3:
        config["source_id"] = rng.choice(("SRC-A", "b.c:d_e-f", "x" * 64))
    # An int is a float to a strict field; validation of a tick accepts it, too.
    for name in list(config):
        if name in _BOUNDS and rng.random() < 0.1:
            low, high = _BOUNDS[name]
            value = rng.randint(int(low) + 1, int(high)) if int(high) > int(low) + 1 else None
            if value is not None and name not in ("comfortable_min_temperature_c",):
                config[name] = value
    if broken:
        name = rng.choice(
            (*_BOUNDS, *_INT_BOUNDS, "schema_version", "source_type", "source_id", "other")
        )
        config[name] = rng.choice(
            (True, "x", None, 1e9, -1e9, float("nan"), float("inf"), 2, [], 0, 1, 1.0)
        )
    return config


_DEMANDS = ("request_toilet", "rest", "sleep", "seek_help", "fetch_water", "collect_meal")


def _record(rng: random.Random, zones: tuple[str, ...]) -> dict[str, Any]:
    op = rng.choice(
        (
            "set_ambient_noise",
            "modify_demand_rate",
            "set_ventilation",
            "set_lighting",
            "set_privacy",
            "set_temperature",
            "something_else",
        )
    )
    unit = rng.random
    parameters: dict[str, Any] = {
        "set_ambient_noise": lambda: {"level": unit()},
        "modify_demand_rate": lambda: {
            "action": rng.choice(_DEMANDS),
            "probability_multiplier": rng.uniform(0.5, 2.0),
            "duration_multiplier": rng.uniform(0.5, 2.0),
        },
        "set_ventilation": lambda: {"effectiveness": unit()},
        "set_lighting": lambda: {"level": unit()},
        "set_privacy": lambda: {"level": unit()},
        "set_temperature": lambda: {"temperature_c": rng.uniform(-40.0, 60.0)},
        "something_else": lambda: {},
    }[op]()
    return {
        "zone_id": rng.choice(zones),
        "op": op,
        "start_minute": rng.randint(0, 3),
        # The Python rule keeps an event that started two records twice in one of its lists.
        "start_event_id": f"event-{rng.randint(0, 4)}",
        "source_id": f"source-{rng.randint(0, 2)}",
        "assumption_ids": [f"assumption-{n}" for n in range(rng.randint(0, 2))],
        "parameters": parameters,
    }


def _zone_world(rng: random.Random, *, broken: bool = False) -> Any:
    configs: dict[str, Any] = {"zone_environment": _config(rng, broken=broken)}
    selection = {domain: rng.choice(ids) for domain, ids in FORMULAS.items() if rng.random() < 0.6}
    if selection or rng.random() < 0.5:
        configs["policy_registry"] = selection
    modifiers = {f"m{n}": _record(rng, ("hall", "room")) for n in range(rng.randint(0, 9))}
    state = {"schema_version": 1, "modifiers": modifiers}
    return SimpleNamespace(model_configs=configs, model_state={ZONE_ENVIRONMENT_STATE_KEY: state})


def _rules(name: str) -> Any:
    return native.rules(name)


def _assert_zone_same(world: Any, zone: str) -> bool:
    """False when both refused."""
    try:
        python = zone_environment_view(world, zone)
    except (ValidationError, ValueError, KeyError) as error:
        with pytest.raises(native.Unsupported):
            _rules("zone_environment").view_of(world, zone)
        assert error is not None
        return False
    rust = _rules("zone_environment").view_of(world, zone)
    assert _exact(rust) == _exact(python), world.model_configs
    return True


@pytest.mark.parametrize("seed", range(6))
def test_the_conditions_of_a_zone_have_the_bits_of_the_python_rule(seed: int) -> None:
    rng = random.Random(seed)
    compared = 0
    for _ in range(400):
        world = _zone_world(rng)
        for zone in ("hall", "room", "nowhere"):
            compared += _assert_zone_same(world, zone)
    assert compared > 900


def test_every_formula_is_compared_on_both_sides_of_its_branches() -> None:
    """All 16 selections, on both sides of each constant, against the Python rule."""
    below, above = 10.0, 30.0
    for temperature_model in FORMULAS["zone_environment"]:
        for rest_model in FORMULAS["zone_rest"]:
            for hygiene_model in FORMULAS["zone_hygiene"]:
                for privacy_model in FORMULAS["zone_privacy"]:
                    for temperature in (below, 20.0, above):
                        for privacy in (0.1, 0.4, 0.5, 0.9):
                            for noise in (0.0, 0.3, 1.0):
                                world = SimpleNamespace(
                                    model_configs={
                                        "zone_environment": {
                                            "noise_rest_efficiency_loss": 0.9,
                                            "lighting_sleep_efficiency_loss": 0.9,
                                        },
                                        "policy_registry": {
                                            "zone_environment": temperature_model,
                                            "zone_rest": rest_model,
                                            "zone_hygiene": hygiene_model,
                                            "zone_privacy": privacy_model,
                                        },
                                    },
                                    model_state={
                                        ZONE_ENVIRONMENT_STATE_KEY: {
                                            "schema_version": 1,
                                            "modifiers": {
                                                "a": _condition(
                                                    "set_temperature", "temperature_c", temperature
                                                ),
                                                "b": _condition("set_privacy", "level", privacy),
                                                "c": _condition(
                                                    "set_ambient_noise", "level", noise
                                                ),
                                                "d": _condition("set_lighting", "level", 1.0),
                                                "e": _condition(
                                                    "set_ventilation", "effectiveness", 0.25
                                                ),
                                            },
                                        }
                                    },
                                )
                                assert _assert_zone_same(world, "hall")


def _condition(op: str, name: str, value: float) -> dict[str, Any]:
    return {
        "zone_id": "hall",
        "op": op,
        "start_minute": 0,
        "start_event_id": f"start-{op}",
        "source_id": "SRC",
        "assumption_ids": ["ASM"],
        "parameters": {name: value},
    }


def test_a_rate_multiplier_is_clamped_where_the_python_rule_clamps_it() -> None:
    for action in ("rest", "seek_help"):
        for factor in (0.5, 2.0):
            modifiers = {
                f"m{n}": {
                    "zone_id": "hall",
                    "op": "modify_demand_rate",
                    "start_minute": n,
                    "start_event_id": f"e{n}",
                    "source_id": "SRC",
                    "assumption_ids": [],
                    "parameters": {
                        "action": action,
                        "probability_multiplier": factor,
                        "duration_multiplier": factor if action == "rest" else 1.0,
                    },
                }
                for n in range(4)
            }
            world = SimpleNamespace(
                model_configs={"zone_environment": {}},
                model_state={ZONE_ENVIRONMENT_STATE_KEY: {"modifiers": modifiers}},
            )
            assert _assert_zone_same(world, "hall")


@pytest.mark.parametrize("seed", range(4))
def test_a_config_the_contract_refuses_is_left_to_it(seed: int) -> None:
    rng = random.Random(1000 + seed)
    outcomes = {True: 0, False: 0}
    for _ in range(500):
        outcomes[_assert_zone_same(_zone_world(rng, broken=True), "hall")] += 1
    assert outcomes[False] > 50
    assert outcomes[True] > 10


def _bare_world(config: dict[str, Any]) -> Any:
    return SimpleNamespace(
        model_configs={"zone_environment": config},
        model_state={ZONE_ENVIRONMENT_STATE_KEY: {"modifiers": {}}},
    )


def _just_below(bound: float) -> float:
    return bound - (abs(bound) * 0.005 or 1e-9)


def _just_above(bound: float) -> float:
    return bound + (abs(bound) * 0.005 or 1e-9)


def test_each_bound_of_the_contract_is_where_the_contract_puts_it() -> None:
    # Each with the fields it is ordered against at their widest.
    room = {
        "min_probability_multiplier": {"max_probability_multiplier": 2.0},
        "max_probability_multiplier": {"min_probability_multiplier": 0.1},
        "comfortable_min_temperature_c": {"comfortable_max_temperature_c": 50.0},
        "comfortable_max_temperature_c": {"comfortable_min_temperature_c": -10.0},
    }
    accepted = refused = 0
    for name, (low, high) in _BOUNDS.items():
        for value in (low, high, _just_below(low), _just_above(high)):
            config = {name: value, "policy_version": "zone-environment-v2", **room.get(name, {})}
            if _assert_zone_same(_bare_world(config), "hall"):
                accepted += 1
            else:
                refused += 1
    for name, (low, high) in _INT_BOUNDS.items():
        for value in (low, high, low - 1, high + 1):
            config = {
                name: value,
                "policy_version": "zone-environment-v2",
                **(
                    {"max_rest_decision_minutes": 240}
                    if name == "min_rest_decision_minutes"
                    else {"min_rest_decision_minutes": 1}
                    if name == "max_rest_decision_minutes"
                    else {}
                ),
            }
            if _assert_zone_same(_bare_world(config), "hall"):
                accepted += 1
            else:
                refused += 1
    for name, (low, high) in _BASELINE_BOUNDS.items():
        for value in (low, high, _just_below(low), _just_above(high)):
            config = {"baseline_by_zone": {"hall": {name: value}}}
            if _assert_zone_same(_bare_world(config), "hall"):
                accepted += 1
            else:
                refused += 1
    assert accepted >= 30
    assert refused >= 30


@pytest.mark.parametrize(
    "config",
    [
        {"policy_version": "zone-environment-v1", "lighting_sleep_efficiency_loss": 0.1},
        {"policy_version": "zone-environment-v1", "privacy_discontent_per_minute": 0.001},
        {"policy_version": "zone-environment-v1", "baseline_by_zone": {"hall": {}}},
        {
            "policy_version": "zone-environment-v2",
            "min_probability_multiplier": 1.0,
            "max_probability_multiplier": 1.0,
        },
        {"comfortable_min_temperature_c": 20.0, "comfortable_max_temperature_c": 20.0},
        {"min_rest_decision_minutes": 9, "max_rest_decision_minutes": 8},
        {"policy_version": "zone-environment-v3"},
        {"source_type": "measured"},
        {"source_id": " spaced"},
        {"baseline_by_zone": {"hall": {"other": 1.0}}},
        {"baseline_by_zone": []},
        # Which documents are legacy, by what they leave out.
        {"noise_rest_efficiency_loss": 0.25},
        {"lighting_sleep_efficiency_loss": 0.25},
        {"baseline_by_zone": {}},
        {"policy_version": "zone-environment-v1"},
        {"policy_version": "zone-environment-v2"},
    ],
)
def test_the_orderings_and_versions_of_the_contract(config: dict[str, Any]) -> None:
    try:
        zone_environment_view(_bare_world(config), "hall")
    except ValidationError:
        with pytest.raises(native.Unsupported):
            _rules("zone_environment").view_of(_bare_world(config), "hall")
        return
    if config.get("source_id") == " spaced":
        # The contract strips it. No canonical document holds it, so the kernel leaves it.
        with pytest.raises(native.Unsupported):
            _rules("zone_environment").view_of(_bare_world(config), "hall")
        return
    assert _assert_zone_same(_bare_world(config), "hall")


def test_a_world_without_the_policy_has_the_neutral_view() -> None:
    for world in (
        SimpleNamespace(model_configs={}, model_state={}),
        SimpleNamespace(model_configs={"zone_environment": {}}, model_state={}),
        SimpleNamespace(
            model_configs={}, model_state={ZONE_ENVIRONMENT_STATE_KEY: {"modifiers": {}}}
        ),
    ):
        assert _exact(_rules("zone_environment").view_of(world, "hall")) == _exact(
            ZoneEnvironmentView()
        )


def test_the_source_of_each_selected_formula_is_the_catalogs() -> None:
    for domain, ids in FORMULAS.items():
        for model_id in ids:
            assert MODEL_DEFINITIONS[model_id]["domain"] == domain
            world = SimpleNamespace(
                model_configs={
                    "zone_environment": {"baseline_by_zone": {}},
                    "policy_registry": {domain: model_id},
                },
                model_state={ZONE_ENVIRONMENT_STATE_KEY: {"modifiers": {}}},
            )
            view = _rules("zone_environment").view_of(world, "hall")
            assert (
                MODEL_DEFINITIONS[model_id]["assumption_source_id"] in view.condition_assumption_ids
            )
            assert _assert_zone_same(world, "hall")


def test_a_formula_this_module_does_not_know_is_left_to_python() -> None:
    world = SimpleNamespace(
        model_configs={
            "zone_environment": {},
            "policy_registry": {"zone_rest": "someone-elses-v1"},
        },
        model_state={ZONE_ENVIRONMENT_STATE_KEY: {"modifiers": {}}},
    )
    with pytest.raises(native.Unsupported, match="another module"):
        _rules("zone_environment").view_of(world, "hall")


def _controller(world: WorldState) -> OfflineSimulationController:
    return OfflineSimulationController(world, _MetricSink(MetricsRecorder()))


def _view_fields(view: WorldView) -> Any:
    return _exact(view.agents), _exact(view.facilities)


def test_a_view_reads_the_selected_formulas_and_sees_each_change(generated_scenario: Any) -> None:
    document = copy.deepcopy(generated_scenario.model_dump(mode="json"))
    configs = document["model_configs"]
    configs["policy_registry"].pop("suite_id", None)
    configs["policy_registry"].update(
        {
            "zone_environment": "zone-temperature-midpoint-v1",
            "zone_rest": "zone-rest-limiting-v1",
            "zone_hygiene": "zone-hygiene-quadratic-v1",
            "zone_privacy": "zone-privacy-proportional-v1",
        }
    )
    world = WorldState.from_scenario(type(generated_scenario).model_validate(document))
    controller = _controller(world)
    rules = _rules("world_view")
    made = rules.view_after_movement(controller, world)
    assert _view_fields(made) == _view_fields(WorldView.from_world(world, include_state_hash=False))
    agent = next(iter(made.agents.values()))
    assert agent.privacy_discontent_per_minute > 0.0

    state = world.model_state[ZONE_ENVIRONMENT_STATE_KEY]
    state["modifiers"]["m"] = _condition("set_privacy", "level", 0.125) | {"zone_id": agent.zone_id}
    made = rules.view_after_movement(controller, world)
    assert made.agents[agent.id].privacy_level == 0.125
    assert _view_fields(made) == _view_fields(WorldView.from_world(world, include_state_hash=False))

    # What the runner does when the configs changed in place.
    world.model_configs["zone_environment"]["privacy_discontent_per_minute"] = 0.005
    controller._zone_environments = {}
    made = rules.view_after_movement(controller, world)
    assert made.agents[agent.id].privacy_discontent_per_minute == (1.0 - 0.125) * 0.005
    assert _view_fields(made) == _view_fields(WorldView.from_world(world, include_state_hash=False))

    # A view of Python's replaces what is kept, and the next view is still right.
    WorldView.from_world(world, person_cache={}, environment_cache=controller._zone_environments)
    state["modifiers"]["m"]["parameters"]["level"] = 0.25
    python = WorldView.from_world(
        world, person_cache={}, environment_cache=controller._zone_environments
    )
    assert python.agents[agent.id].privacy_level == 0.25
    made = rules.view_after_movement(controller, world)
    assert made.agents[agent.id].privacy_level == 0.25


def test_a_bad_modifier_is_the_python_rules_to_raise_on(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    agent = next(iter(world.agents.values()))
    world.model_state[ZONE_ENVIRONMENT_STATE_KEY]["modifiers"]["m"] = {
        "zone_id": agent.zone_id,
        "start_minute": 0,
    }
    with pytest.raises(native.Unsupported, match="zone modifier"):
        _rules("world_view").view_after_movement(controller, world)
    with pytest.raises(KeyError):
        WorldView.from_world(world, include_state_hash=False)


@pytest.mark.parametrize("model", sorted(LIFE_STATE_PSYCHOLOGY_POLICIES) + ["causal-stress-v1"])
def test_only_a_life_state_psychology_reads_beliefs_in_false_claims(
    generated_scenario: Any, model: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    registry = world.model_configs["policy_registry"]
    world.model_configs["policy_registry"] = {
        "schema_version": registry["schema_version"],
        "psychology": model,
    }
    message = {
        "id": "m",
        "source_id": "SRC",
        "zone_id": "z",
        "truth_status": "false",
        "time": 0,
        "origin_event_id": "e",
        "origin_agent_id": "A001",
    }
    state = world.model_state["information_spread_v1"]
    state["messages"]["m"] = message
    state["beliefs"].setdefault("A001", {})["m"] = {
        "message_id": "m",
        "belief": "heard",
        "source": "SRC",
        "last_contact": 0,
        "last_event_id": "event-m",
        "exposed": True,
        "belief_strength": 0.75,
        "share_propensity": 0.5,
        "correction_exposed": False,
        "correction_accepted": False,
        "ever_misbelieved": False,
        "ever_forgotten": False,
        "last_correction_accepted": False,
    }
    python = WorldView.from_world(world, include_state_hash=False)
    made = _rules("world_view").view_after_movement(controller, world)
    pressure = made.agents["A001"].misinformation_belief_pressure
    assert pressure.hex() == python.agents["A001"].misinformation_belief_pressure.hex()
    assert (pressure > 0.0) == (model in LIFE_STATE_PSYCHOLOGY_POLICIES)


def test_a_selection_the_contract_refuses_is_the_python_rules_to_raise_on(
    generated_scenario: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    world.model_configs["policy_registry"].pop("suite_id", None)
    world.model_configs["policy_registry"]["psychology"] = "someone-elses-v1"
    with pytest.raises(native.Unsupported, match="Python rule"):
        _rules("world_view").view_after_movement(controller, world)
    with pytest.raises(ValueError, match="psychology"):
        WorldView.from_world(world, include_state_hash=False)


# ---- People ----

_IDS = ("A001", "a" * 64, "a" * 65, "-a", "a b", "é", "abc\n", "", "x.y:z_w-1", 3, None)
_BOOLS = (True, False, 0, 1, 2, 0.0, 1.0, 0.5, "yes", "NO", "oFf", "t", "maybe", "", None, [])
_MINUTES = (None, 0, 1, 1439, 1440, 1441, -1, True, 1.0, "3")
_GOOD_IDS = ("A001", "a" * 64, "x.y:z_w-1")


def _ids(rng: random.Random, limit: int, *, broken: bool) -> Any:
    size = rng.randint(0, limit + 1 if broken and rng.random() < 0.2 else limit)
    pool = _IDS if broken else _GOOD_IDS
    made = [rng.choice(pool) for _ in range(size)]
    if broken and rng.random() < 0.1:
        return rng.choice(("A001", None, 3, {"a": 1}))
    return tuple(made) if rng.random() < 0.1 else made


def _person(rng: random.Random, *, broken: bool) -> dict[str, Any]:
    choose = (
        (lambda good, every: rng.choice(every))
        if broken
        else (lambda good, every: rng.choice(good))
    )
    makers = {
        "household_id": lambda: choose((None, *_GOOD_IDS), _IDS),
        "parent_ids": lambda: _ids(rng, 4, broken=broken),
        "caregiver_ids": lambda: _ids(rng, 16, broken=broken),
        "care_for_ids": lambda: _ids(rng, 16, broken=broken),
        "partner_ids": lambda: _ids(rng, 16, broken=broken),
        "relative_ids": lambda: _ids(rng, 32, broken=broken),
        "requires_review": lambda: choose((True, False), _BOOLS),
        "review_reasons": lambda: rng.choice(
            (["any text at all"], [], ["x"] * 32, ["x"] * 33, [3], "text")
            if broken
            else ([], ["why"])
        ),
        "roles": lambda: rng.choice(
            (["parent"], ["child", "alone"], ["other"], [1], ["partner"] * 40)
            if broken
            else ([], ["parent"], ["child", "caregiver"], ["dependent", "partner", "alone"])
        ),
        "medical_support": lambda: choose((None, True, False), _BOOLS),
        "guardian_status": lambda: choose(
            (None, "accompanied", "unaccompanied", "not_applicable"),
            (None, "other", 1, "accompanied"),
        ),
        "daily_rhythm_id": lambda: choose((None, *_GOOD_IDS), _IDS),
        "medication_support": lambda: choose((None, True, False), _BOOLS),
        "care_support_needs": lambda: rng.choice(
            (["hygiene_support", "hygiene_support"], ["nothing"], list(get_args(SupportKind)), [1])
            if broken
            else ([], list(get_args(SupportKind))[:8], ["language_access"])
        ),
        "pet_count": lambda: choose((None, 0, 20), (None, 0, 20, 21, -1, True, 1.5)),
        "information_access": lambda: None if rng.random() < 0.5 else _ids(rng, 3, broken=broken),
        "usual_wake_minute": lambda: choose((None, 0, 1439), _MINUTES),
        "usual_sleep_minute": lambda: choose((None, 0, 1439), _MINUTES),
        "required_sleep_minutes": lambda: choose((None, 0, 1440), _MINUTES),
        "source_ids": lambda: _ids(rng, 3, broken=broken),
    }
    record = {name: make() for name, make in makers.items() if rng.random() < 0.3}
    if broken and rng.random() < 0.05:
        record["other_field"] = 1
    return record


@pytest.mark.parametrize("seed", range(4))
def test_a_person_context_is_taken_and_read_as_the_contract_takes_and_reads_it(seed: int) -> None:
    rng = random.Random(seed)
    fields_of = _rules("person_context").fields_of
    accepted = refused = 0
    for number in range(4000):
        record = _person(rng, broken=number % 2 == 1)
        try:
            context = PersonContext.model_validate(record)
        except ValidationError:
            refused += 1
            with pytest.raises(native.Unsupported):
                fields_of(record)
            continue
        accepted += 1
        assert _exact(fields_of(record)) == _exact(_fields_of_person(context)), record
    assert accepted > 1000
    assert refused > 500


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("parent_ids", 4),
        ("caregiver_ids", 16),
        ("care_for_ids", 16),
        ("partner_ids", 16),
        ("relative_ids", 32),
        ("review_reasons", 32),
        ("care_support_needs", 8),
    ],
)
def test_the_length_of_each_list_is_where_the_contract_puts_it(field: str, limit: int) -> None:
    fields_of = _rules("person_context").fields_of
    kinds = list(get_args(SupportKind))
    make = (
        (lambda size: kinds[:size])
        if field == "care_support_needs"
        else (lambda size: ["A"] * size)
    )
    PersonContext.model_validate({field: make(limit)})
    fields_of({field: make(limit)})
    with pytest.raises(ValidationError):
        PersonContext.model_validate({field: make(limit + 1)})
    with pytest.raises(native.Unsupported):
        fields_of({field: make(limit + 1)})


def test_each_literal_of_the_contract_is_taken() -> None:
    fields_of = _rules("person_context").fields_of
    for kind in get_args(SupportKind):
        assert fields_of({"care_support_needs": [kind]})["personal_context_present"] is True
    for role in ("parent", "child", "caregiver", "dependent", "partner", "alone"):
        assert fields_of({"roles": [role]})["roles"] == (role,)
    for status in ("accompanied", "unaccompanied", "not_applicable"):
        PersonContext.model_validate({"guardian_status": status})
        fields_of({"guardian_status": status})
    for not_a_record in (None, 1, "x", [], ()):
        with pytest.raises(native.Unsupported):
            fields_of(not_a_record)


def test_a_view_keeps_a_context_while_it_stays_equal(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    controller = _controller(world)
    people = world.model_configs["population_context"]["people"]
    rules = _rules("world_view")
    assert rules.view_after_movement(controller, world).agents["A001"].roles == ("alone",)
    # The kernel does not fill Python's cache: `from_world` parses what it needs for itself.
    assert controller._person_contexts == {}
    people["A001"] = {"roles": ["child"], "parent_ids": ["A002"]}
    first = rules.view_after_movement(controller, world)
    assert first.agents["A001"].roles == ("child",)
    assert first.agents["A001"].parent_ids == ("A002",)
    # In place, as the Python rule's copy sees it.
    people["A001"]["parent_ids"].append("A003")
    second = rules.view_after_movement(controller, world)
    assert second.agents["A001"].parent_ids == ("A002", "A003")
    assert _view_fields(second) == _view_fields(
        WorldView.from_world(world, include_state_hash=False)
    )
    del people["A001"]
    third = rules.view_after_movement(controller, world)
    assert third.agents["A001"].personal_context_present is False
    assert _view_fields(third) == _view_fields(
        WorldView.from_world(world, include_state_hash=False)
    )


# ---- Proximity ----


def _proximity_world(scenario_with: Any, **options: Any) -> WorldState:
    scenario = scenario_with(duration_days=1 / 24, social_proximity_policy_id=PROXIMITY, **options)
    return WorldState.from_scenario(scenario)


def _pair_fields(pair: ProximityPair) -> tuple[Any, ...]:
    return (
        pair.distance_m,
        pair.first_agent_id,
        pair.second_agent_id,
        pair.zone_id,
        pair.first_point,
        pair.second_point,
    )


def _assert_sample_same(world: WorldState, config: dict[str, Any]) -> list[Any]:
    python = [_pair_fields(pair) for pair in DistanceSampledProximityPolicy(config).sample(world)]
    rust = _rules("proximity").sample_of(world, config)
    assert json.dumps(rust) == json.dumps(python)
    return rust


@pytest.mark.parametrize("radius", [0.25, 1.5, 2, 10.0])
def test_the_pairs_of_a_sample_are_the_python_rules_from_the_row(
    generated_scenario_with: Any, radius: float
) -> None:
    world = _proximity_world(generated_scenario_with)
    positions = world.model_state["movement_v1"]["positions"]
    zones = sorted(world.zones)
    rng = random.Random(7)
    found = 0
    for limit in (1, 5, 10_000):
        for _ in range(20):
            for agent_id in positions:
                record = positions[agent_id]
                if record["point"] is None:
                    continue
                record["point"]["x"] = float(rng.randint(-8, 8)) * radius / 4
                record["point"]["y"] = rng.uniform(-3.0, 3.0)
                record["zone_id"] = rng.choice(zones[:2])
            config = {"max_distance_m": radius, "max_pairs_per_sample": limit}
            found += len(_assert_sample_same(world, config))
    assert world._core.mirror("movement_v1").tainted is None
    assert found > 100


@pytest.mark.parametrize(
    "change",
    [
        lambda record, world: record["point"].__setitem__("x", 1),
        lambda record, world: record["point"].__setitem__("x", True),
        lambda record, world: record["point"].__setitem__("x", "1"),
        lambda record, world: record["point"].__setitem__("x", float("nan")),
        lambda record, world: record["point"].__setitem__("y", float("inf")),
        lambda record, world: record["point"].pop("y"),
        lambda record, world: record["point"].__setitem__("z", 0.0),
        lambda record, world: record.__setitem__("point", None),
        lambda record, world: record.__setitem__("point", [1.0, 2.0]),
        lambda record, world: record.__setitem__("position_kind", "edge"),
        lambda record, world: record.pop("position_kind"),
        lambda record, world: record.__setitem__("zone_id", "nowhere"),
        lambda record, world: record.__setitem__("zone_id", None),
        lambda record, world: record.__setitem__("zone_id", 3),
        lambda record, world: record.clear(),
    ],
)
def test_what_the_sampler_skips_it_skips_from_the_tree_of_a_tainted_row(
    generated_scenario_with: Any, change: Any
) -> None:
    world = _proximity_world(generated_scenario_with)
    positions = world.model_state["movement_v1"]["positions"]
    placed = [record for record in positions.values() if record["point"] is not None]
    assert len(placed) >= 3
    for record in placed:
        record["point"]["x"], record["point"]["y"] = 1.0, 1.0
        record["zone_id"] = placed[0]["zone_id"]
    config = {"max_distance_m": 1.5, "max_pairs_per_sample": 10_000}
    before = _assert_sample_same(world, config)
    change(placed[0], world)
    after = _assert_sample_same(world, config)
    assert before
    assert len(after) <= len(before)


def test_a_person_who_is_gone_and_a_position_that_is_no_dict_are_skipped(
    generated_scenario_with: Any,
) -> None:
    world = _proximity_world(generated_scenario_with)
    positions = world.model_state["movement_v1"]["positions"]
    first = next(record for record in positions.values() if record["point"] is not None)
    positions["nobody"] = copy.deepcopy(first)
    config = {"max_distance_m": 10.0, "max_pairs_per_sample": 10_000}
    pairs = _assert_sample_same(world, config)
    assert all("nobody" not in pair[1:3] for pair in pairs)
    positions["nobody"] = 7
    _assert_sample_same(world, config)


def test_a_coordinate_too_large_for_a_cell_is_left_to_python(generated_scenario_with: Any) -> None:
    world = _proximity_world(generated_scenario_with)
    positions = world.model_state["movement_v1"]["positions"]
    record = next(record for record in positions.values() if record["point"] is not None)
    record["point"]["x"] = 1e300
    config = {"max_distance_m": 1.5, "max_pairs_per_sample": 10}
    with pytest.raises(native.Unsupported, match="does not fit"):
        _rules("proximity").sample_of(world, config)
    record["point"]["x"] = 10**400
    with pytest.raises(native.Unsupported, match="too large"):
        _rules("proximity").sample_of(world, config)
    with pytest.raises(OverflowError):
        DistanceSampledProximityPolicy(config).sample(world)


def _stand_in(world: WorldState, config: dict[str, Any] | None = None) -> Any:
    config = config or {
        "schema_version": 1,
        "enabled": True,
        "policy_version": PROXIMITY,
        "sample_interval_minutes": 15,
        "max_distance_m": 1.5,
        "max_pairs_per_sample": 500,
    }
    return SimpleNamespace(
        world=world,
        proximity_contact_config=config,
        proximity_policy=DistanceSampledProximityPolicy(config),
    )


def _new_events(world: WorldState, seen: set[str]) -> list[dict[str, Any]]:
    return sorted(
        (event.to_dict() for event in world.event_queue.pending() if event.event_id not in seen),
        key=lambda event: event["sequence"],
    )


def _run_step_both_ways(
    world: WorldState, minute: int, config: dict[str, Any] | None = None
) -> Any:
    """The Python rule on a copy, the kernel on the world: same events, same counter."""
    copied = WorldState.from_dict(world.to_dict())
    seen = {event.event_id for event in world.event_queue.pending()}
    SamplePhase._sample_proximity_contacts(_stand_in(copied, config), minute)
    _rules("proximity").sample_contacts(_stand_in(world, config), minute)
    python, rust = _new_events(copied, seen), _new_events(world, seen)
    assert json.dumps(rust) == json.dumps(python)
    assert world.id_counters == copied.id_counters
    return rust


def test_the_step_schedules_the_events_of_the_python_rule(generated_scenario_with: Any) -> None:
    world = _proximity_world(generated_scenario_with)
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(5):
        engine.step()
    positions = world.model_state["movement_v1"]["positions"]
    zone = next(record["zone_id"] for record in positions.values() if record["point"] is not None)
    for number, record in enumerate(positions.values()):
        if record["point"] is not None:
            record["zone_id"] = zone
            record["point"]["x"] = 0.125 * number
            record["point"]["y"] = 0.0
    events = _run_step_both_ways(world, 30)
    assert len(events) > 3
    first = events[0]
    assert first["event_type"] == "proximity_contact_update"
    assert first["priority"] == 40 and first["kind"] == "endogenous"
    assert first["source_id"] == PROXIMITY_CONTACT_SOURCE_ID
    assert first["payload"]["contact_minutes"] == 0
    assert first["provenance"]["observed_minute"] == 31


def test_a_minute_off_the_interval_or_at_the_end_schedules_nothing(
    generated_scenario_with: Any,
) -> None:
    world = _proximity_world(generated_scenario_with)
    assert _run_step_both_ways(world, 7) == []
    assert _run_step_both_ways(world, world.duration_minutes - 1) == []
    every_minute = {
        "schema_version": 1,
        "enabled": True,
        "policy_version": PROXIMITY,
        "sample_interval_minutes": 1,
        "max_distance_m": 10.0,
        "max_pairs_per_sample": 2,
    }
    assert _run_step_both_ways(world, world.duration_minutes - 1, every_minute) == []
    config = {
        "schema_version": 1,
        "enabled": True,
        "policy_version": PROXIMITY,
        "sample_interval_minutes": 7,
        "max_distance_m": 10.0,
        "max_pairs_per_sample": 2,
    }
    assert len(_run_step_both_ways(world, 7, config)) == 2


def test_a_policy_this_kernel_does_not_know_runs_in_python(generated_scenario_with: Any) -> None:
    class Other(DistanceSampledProximityPolicy):
        def sample(self, world: WorldState) -> list[ProximityPair]:
            return []

    world = _proximity_world(generated_scenario_with)
    stand_in = _stand_in(world)
    stand_in.proximity_policy = Other(stand_in.proximity_contact_config)
    with pytest.raises(native.Unsupported, match="another proximity policy"):
        _rules("proximity").sample_contacts(stand_in, 0)
    stand_in.proximity_contact_config = None
    assert _rules("proximity").sample_contacts(stand_in, 0) is None


def test_the_ticks_run_the_kernel_rather_than_the_python_rule(
    generated_scenario_with: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_called(self: Any, world: Any) -> list[Any]:
        raise AssertionError("the Python rule ran")

    monkeypatch.setattr(DistanceSampledProximityPolicy, "sample", not_called)
    world = _proximity_world(generated_scenario_with)
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(16):
        engine.step()
    assert any(
        event.event_type == "proximity_contact_update"
        for event in (*world.event_queue.pending(), *(r.event for r in world.event_log.records))
    )


# ---- An information contact that is not an event ----


def test_a_contact_that_is_not_an_event_raises_as_the_python_step_does(
    information_spread_scenario: Any,
) -> None:
    """A negative counter is the one input left that the row's parse does not close."""
    world = WorldState.from_scenario(information_spread_scenario(duration=120, correction=False))
    controller = _controller(world)
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(119):
        engine.step()
        copied = WorldState.from_dict(world.to_dict())
        counter = copied.id_counters["event"]
        minute = world.minute_index
        try:
            copied.id_counters["event"] = -5
            _controller(copied)._sample_information_contacts(minute, CounterRNG(11))
            continue
        except ValueError as error:
            expected = str(error)
        taken = copied.id_counters["event"]
        assert expected == "event sequence must be a non-negative integer"
        assert taken == -4
        world.id_counters["event"] = -5
        with pytest.raises(ValueError) as raised:
            _rules("information").sample_contacts(controller, minute, CounterRNG(11))
        assert str(raised.value) == expected
        assert world.id_counters["event"] == taken
        assert counter >= 0
        return
    raise AssertionError("no minute had a contact to schedule")
