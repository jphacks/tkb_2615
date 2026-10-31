"""information_spread_v1 owned by Rust, and its sample step in native/src/rules/information.rs.

policies/information.py stays the comparison base. benchmarks/rule_shadow compares every draw and
probability on the gate; here, on small worlds and on values no scenario reaches.
"""

from __future__ import annotations

import copy
import json
import math
import random
from types import SimpleNamespace
from typing import Any

import pytest

from sheltersim import native
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.state_mirror import rust_validated
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.information import (
    INFORMATION_SPREAD_STATE_KEY as KEY,
)
from sheltersim.domain.information import (
    create_information_runtime_state,
    validate_information_runtime_state,
)
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.phases.sample import SamplePhase
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import _MetricSink
from sheltersim.policies.alternatives import HazardInformationPolicy
from sheltersim.policies.information import (
    EvidenceAwareInformationPolicy,
    InformationSpreadPolicy,
)
from sheltersim.policies.world_view import WorldView

needs_kernels = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

MODELS = (InformationSpreadPolicy, HazardInformationPolicy, EvidenceAwareInformationPolicy)


def _kernels() -> Any:
    return native.rules("information")


def _mirror(world: WorldState) -> Any:
    return world._core.mirror(KEY)


@pytest.fixture
def rumors(
    generated_previews: Any, information_spread_scenario: Any, information_spread_config: Any
) -> Any:
    def build(duration: int = 60, **config: Any) -> ScenarioSpec:
        document = copy.deepcopy(generated_previews()["scenario"])
        events = information_spread_scenario(duration=duration, correction=True)
        document["duration_minutes"] = duration
        document["scheduled_events"] = events.model_dump(mode="json")["scheduled_events"]
        document["scheduled_events"][0]["payload"]["origin_agent_id"] = "A001"
        configs = document["model_configs"]
        configs["information_spread"] = (
            configs["information_spread"] | information_spread_config() | config
        )
        return ScenarioSpec.model_validate(document)

    return build


def _run_ticks(scenario: Any, minutes: int, check: Any = None) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
        if check is not None:
            check(world)
    return world


class _Recording(CounterRNG):
    """The Python rule's draws, as text in which a float keeps its bits."""

    def __init__(self, seed: int) -> None:
        super().__init__(seed)
        self.made: list[tuple[str, str]] = []

    def random(self, *key_parts: Any) -> float:
        value = super().random(*key_parts)
        key = json.dumps(list(key_parts), ensure_ascii=False, separators=(",", ":"))
        self.made.append((key, value.hex()))
        return value


def _controller(world: WorldState, policy: Any, neighbors: dict[str, tuple[str, ...]]) -> Any:
    calls: list[tuple[Any, ...]] = []
    return SimpleNamespace(
        world=world,
        information_policy=policy,
        zone_neighbors=neighbors,
        artifacts=SimpleNamespace(trace_level=TraceLevel.OFF),
        calls=calls,
        _schedule_information_contact=lambda *arguments: calls.append(arguments),
    )


def _python_rule(
    world: WorldState, policy: Any, neighbors: dict[str, tuple[str, ...]], seed: int
) -> tuple[Any, list[tuple[str, str]]]:
    rng = _Recording(seed)
    state = json.loads(json.dumps(world.model_state[KEY]))
    view = WorldView.from_world(world, include_state_hash=False)
    return policy.update(view, state, rng, zone_neighbors=neighbors), rng.made


def _assert_same_as_python(
    world: WorldState, policy: Any, neighbors: dict[str, tuple[str, ...]], seed: int
) -> Any:
    """Every draw, probability and event, and then the namespace, as the Python rule makes them."""
    python, python_draws = _python_rule(world, policy, neighbors, seed)
    controller = _controller(world, policy, neighbors)
    minute, rng = world.minute_index, CounterRNG(seed)

    planned = _kernels().planned_contacts(controller, minute, rng)
    draws = sorted((key, value.hex()) for key, value in planned["draws"])
    assert draws == sorted(python_draws)
    contacts = sorted(
        (tuple(item) for item in planned["contacts"]),
        key=lambda item: (item[1], item[0], item[4] or ""),
    )
    assert json.dumps(contacts) == json.dumps(
        [
            (
                item.sender_agent_id,
                item.recipient_agent_id,
                item.contact_basis,
                item.contact_minutes,
                item.message_id,
                item.belief,
                item.transmission_probability,
                item.transmission_selected,
                item.message_source_id,
                item.causation_id,
            )
            for item in python.contacts
        ]
    )
    assert planned["policy_version"] == python.policy_version
    if isinstance(policy, EvidenceAwareInformationPolicy):
        forget = -math.expm1(-policy.config.forgetting_rate_per_minute)
        assert planned["forget_probability"].hex() == forget.hex()

    _kernels().sample_contacts(controller, minute, rng)
    assert json.dumps(controller.calls) == json.dumps(
        [
            (
                minute,
                item.message_id,
                item.sender_agent_id,
                item.recipient_agent_id,
                item.belief.value,
                item.contact_basis,
                item.contact_minutes,
                item.probability,
                item.message_source_id,
                item.causation_id,
            )
            for item in python.proposals
        ]
    )
    state = world.model_state[KEY]
    assert json.dumps(state, sort_keys=True) == json.dumps(python.runtime_state, sort_keys=True)
    # Contacts in the rule's order too: nothing depends on it, and it costs nothing to keep.
    assert list(state["contacts"]) == list(python.runtime_state["contacts"])
    assert _mirror(world).verify()
    return python


def _belief(message: str, belief: str, **fields: Any) -> dict[str, Any]:
    corrected = belief in ("verified", "rejected")
    return {
        "message_id": message,
        "belief": belief,
        "source": "SRC",
        "last_contact": 0,
        "last_event_id": f"event-{message}",
        "exposed": True,
        "belief_strength": 0.5,
        "share_propensity": 0.0 if belief == "unknown" else 0.5,
        "correction_exposed": corrected,
        "correction_accepted": corrected,
        "ever_misbelieved": False,
        "ever_forgotten": belief == "unknown",
        "last_correction_accepted": corrected,
    } | fields


def _message(message: str, origin: str, zone: str, time: int = 0) -> dict[str, Any]:
    return {
        "id": message,
        "truth_status": "false",
        "source_id": f"source-{message}",
        "zone_id": zone,
        "time": time,
        "origin_event_id": f"event-{message}",
        "origin_agent_id": origin,
    }


def _world_with_claims(scenario: Any, *, believers: int = 3) -> WorldState:
    world = WorldState.from_scenario(scenario)
    state = create_information_runtime_state(set(world.agents))
    agents = sorted(world.agents)
    zone = world.agents[agents[0]].zone_id
    state["messages"]["m1"] = _message("m1", agents[0], zone)
    state["messages"]["m2"] = _message("m2", agents[0], zone, time=1)
    for agent_id in agents[:believers]:
        state["beliefs"][agent_id]["m1"] = _belief("m1", "heard")
    world.model_state[KEY] = state
    return world


# ---- The row ------------------------------------------------------------------------------------


@needs_kernels
def test_a_row_writes_the_bytes_of_the_tree_after_real_ticks(rumors: Any) -> None:
    def check(world: WorldState) -> None:
        mirror = _mirror(world)
        assert mirror.tainted is None
        assert mirror.verify()

    world = _run_ticks(rumors(duration=60), 60, check)
    state = world.model_state[KEY]
    assert state["messages"] and any(state["beliefs"].values())
    assert _mirror(world).row_json() == native.canonical_json(state)


@needs_kernels
@pytest.mark.parametrize(
    ("write", "reason"),
    [
        (lambda state: state["query_pressure"].update({"A001": 1}), "not a float"),
        (lambda state: state["counters"].update({"contact_attempts": 1.0}), "not an int"),
        (lambda state: state["contacts"].update({"k": []}), "not a dict"),
        (lambda state: state["messages"].update({"m": {"id": "m"}}), "other fields"),
        (lambda state: state.update({"more": 1}), "no field"),
    ],
)
def test_a_value_the_row_cant_hold_taints_the_namespace(
    generated_scenario: Any, write: Any, reason: str
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    write(world.model_state[KEY])
    assert reason in _mirror(world).tainted
    assert not rust_validated(world, KEY)


@needs_kernels
def test_single_writes_from_python_reach_the_row(generated_scenario: Any) -> None:
    world = _world_with_claims(generated_scenario)
    state = world.model_state[KEY]
    first, second = sorted(world.agents)[:2]
    state["beliefs"][first]["m2"] = _belief("m2", "verified")
    state["beliefs"][second]["m1"]["share_propensity"] = 0.25
    state["query_pressure"][first] = 0.75
    state["counters"]["contact_deliveries"] += 1
    state["support_requests"].append(
        {"request_id": "r", "agent_id": first, "minute": 0, "message_ids": ["m1"]}
    )
    state["support_requests"][:] = list(state["support_requests"])
    del state["messages"]["m2"], state["beliefs"][first]["m2"]
    assert _mirror(world).tainted is None
    assert _mirror(world).verify()


# ---- The kernel runs, and writes values in place ------------------------------------------------


@needs_kernels
def test_ticks_never_write_the_whole_namespace(rumors: Any) -> None:
    seen: dict[int, tuple[Any, Any, int]] = {}

    def check(world: WorldState) -> None:
        mirror = _mirror(world)
        first = seen.setdefault(
            id(world._core), (world._core, world.model_state[KEY], mirror.stats()["whole_writes"])
        )
        assert world.model_state[KEY] is first[1]
        assert mirror.stats()["whole_writes"] == first[2]

    world = _run_ticks(rumors(duration=60), 60, check)
    assert world.model_state[KEY]["counters"]["contact_attempts"] > 0
    assert world.model_state[KEY]["counters"]["contact_deliveries"] > 0


@needs_kernels
def test_a_tick_keeps_nodes_for_later_writes_from_python(generated_scenario: Any) -> None:
    """The contact records are reused under new keys, and the table must know the new keys."""
    world = _run_ticks(generated_scenario, 5)
    state = world.model_state[KEY]
    assert _mirror(world).stats()["live_nodes"] > len(state["contacts"])
    for key, record in state["contacts"].items():
        record["consecutive_minutes"] = record["consecutive_minutes"]
        record["last_seen_minute"] = record["last_seen_minute"] + 0
        state["contacts"][key]["consecutive_minutes"] += 1
    assert _mirror(world).verify()
    first = next(iter(state["contacts"]))
    removed = state["contacts"].pop(first)
    removed["consecutive_minutes"] = 99
    assert _mirror(world).tainted is None
    assert _mirror(world).verify()


@needs_kernels
def test_fewer_contacts_release_the_nodes_left_over(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    neighbors = {zone: () for zone in world.zones}
    policy = InformationSpreadPolicy(InformationSpreadConfig(enabled=True))
    state = world.model_state[KEY]
    agents = sorted(world.agents)
    left_over = {}
    for index, agent in enumerate(agents):
        key = json.dumps([agent, agents[index - 1]], separators=(",", ":"))
        state["contacts"][key] = {"last_seen_minute": 0, "consecutive_minutes": 1}
        left_over[key] = state["contacts"][key]
    none = InformationSpreadPolicy(InformationSpreadConfig(enabled=True, max_contacts_per_agent=0))
    nodes = _mirror(world).stats()["live_nodes"]
    _kernels().sample_contacts(_controller(world, none, neighbors), 0, CounterRNG(1))
    assert state["contacts"] == {}
    assert _mirror(world).stats()["live_nodes"] == nodes - len(left_over)
    next(iter(left_over.values()))["consecutive_minutes"] = 7
    assert _mirror(world).tainted is None
    assert _mirror(world).verify()
    _assert_same_as_python(world, policy, neighbors, 1)


@needs_kernels
@pytest.mark.parametrize("model", ["evidence-aware-rumor-v1", "sparse-information-contact-v1"])
def test_the_rust_step_and_the_python_step_make_the_same_world(
    rumors: Any, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    scenario = rumors(duration=90)
    document = scenario.model_dump(mode="json")
    document["model_configs"]["policy_registry"]["information"] = model
    # A suite fixes its members, and this selection replaces a member.
    document["model_configs"]["policy_registry"].pop("suite_id", None)
    scenario = ScenarioSpec.model_validate(document)
    fast = _run_ticks(scenario, 90)
    monkeypatch.setattr(
        OfflineSimulationController,
        "_sample_information_contacts",
        SamplePhase._sample_information_contacts,
    )
    slow = _run_ticks(scenario, 90)
    assert slow.canonical_json() == fast.canonical_json()
    assert [record.event.event_id for record in slow.event_log.records] == [
        record.event.event_id for record in fast.event_log.records
    ]
    # The Python step wrote the namespace whole, and the row followed.
    assert _mirror(slow).stats()["whole_writes"] > 1
    assert _mirror(slow).verify()


# ---- Identity of the rule with the Python rule --------------------------------------------------


@needs_kernels
@pytest.mark.parametrize("model", MODELS)
def test_claims_pass_as_the_python_rule_passes_them(generated_scenario: Any, model: Any) -> None:
    config = InformationSpreadConfig(
        enabled=True,
        minimum_contact_minutes=1,
        contact_duration_scale_minutes=1,
        base_transmission_probability=0.5,
        forgetting_rate_per_minute=0.2,
    )
    proposals = 0
    for seed in range(8):
        world = _world_with_claims(generated_scenario, believers=5)
        neighbors = {zone: tuple(sorted(set(world.zones) - {zone})) for zone in world.zones}
        python = _assert_same_as_python(world, model(config), neighbors, seed)
        assert python.contacts
        proposals += len(python.proposals)
    assert proposals > 0


@needs_kernels
def test_random_states_have_the_python_rules_bits(generated_scenario: Any) -> None:
    """Models, settings, beliefs, zones, households and last tick's contacts no gate has."""
    draw = random.Random(20261031)
    transfers = forgotten = 0
    for _ in range(60):
        world = WorldState.from_scenario(generated_scenario)
        agents = sorted(world.agents)
        zones = sorted(world.zones)
        minute = draw.randrange(0, 200)
        world.minute_index = minute
        for agent_id in agents:
            agent = world.agents[agent_id]
            agent.zone_id = draw.choice(zones)
            agent.trust = draw.choice((0.0, 1.0, draw.random()))
            agent.stress = draw.choice((0.0, 1.0, draw.random()))
        state = create_information_runtime_state(set(agents))
        messages = [f"m{index}" for index in range(draw.randrange(0, 5))]
        for message in messages:
            state["messages"][message] = _message(
                message, draw.choice(agents), draw.choice(zones), draw.randrange(0, 3)
            )
        for agent_id in agents:
            state["query_pressure"][agent_id] = draw.choice((0.0, 1.0, draw.random()))
            for message in messages:
                if draw.random() < 0.5:
                    status = draw.choice(("unknown", "heard", "verified", "rejected"))
                    state["beliefs"][agent_id][message] = _belief(
                        message,
                        status,
                        share_propensity=0.0 if status == "unknown" else draw.random(),
                    )
        for _ in range(draw.randrange(0, 80)):
            first, second = draw.sample(agents, 2)
            key = json.dumps([first, second], ensure_ascii=False, separators=(",", ":"))
            state["contacts"][key] = {
                "last_seen_minute": draw.choice((max(minute - 1, 0), minute, 0)),
                "consecutive_minutes": draw.randrange(1, 12),
            }
        state["counters"]["contact_attempts"] = draw.randrange(0, 100)
        world.model_state[KEY] = state
        config = InformationSpreadConfig(
            enabled=True,
            max_contacts_per_agent=draw.randrange(0, 6),
            minimum_contact_minutes=draw.randrange(1, 4),
            contact_duration_scale_minutes=draw.randrange(1, 8),
            base_transmission_probability=draw.choice((0.0, 1.0, draw.random())),
            correction_transmission_multiplier=draw.random() * 2,
            trust_sensitivity=draw.choice((0, 1, draw.random())),
            family_contact_multiplier=draw.random() * 2,
            adjacent_zone_contact_multiplier=draw.random() * 2,
            query_pressure_decay_per_minute=draw.choice((0.0, 0.25, draw.random() / 4)),
            distress_sharing_sensitivity=draw.choice((0.0, 1.0, draw.random())),
            share_propensity_decay_per_minute=draw.choice((0.0, 0.25, draw.random() / 4)),
            forgetting_rate_per_minute=draw.choice((0.0, 0.1, draw.random())),
        )
        neighbors = {
            zone: tuple(draw.sample(zones, draw.randrange(0, len(zones) + 1)))
            for zone in zones
            if draw.random() < 0.8
        }
        policy = draw.choice(MODELS)(config)
        python = _assert_same_as_python(world, policy, neighbors, draw.randrange(0, 2**62))
        transfers += sum(item.transmission_probability > 0.0 for item in python.contacts)
        forgotten += sum(
            record["belief"] == "unknown"
            for records in python.runtime_state["beliefs"].values()
            for record in records.values()
        )
    assert transfers > 100 and forgotten > 100


@needs_kernels
def test_a_probability_has_the_bits_of_the_python_rule_and_one_percent_shows() -> None:
    draw = random.Random(5)
    for _ in range(2000):
        config = InformationSpreadConfig(
            enabled=True,
            contact_duration_scale_minutes=draw.randrange(1, 30),
            base_transmission_probability=draw.random(),
            correction_transmission_multiplier=draw.random() * 2,
            trust_sensitivity=draw.random(),
            family_contact_multiplier=draw.random() * 2,
            adjacent_zone_contact_multiplier=draw.random() * 2,
        )
        trust = draw.random()
        basis = draw.choice(("family", "same_zone", "adjacent_zone"))
        minutes = draw.randrange(1, 40)
        correction = draw.random() < 0.5
        hazard = draw.random() < 0.5
        policy = (HazardInformationPolicy if hazard else InformationSpreadPolicy)(config)
        python = policy._transmission_probability(trust, basis, minutes, correction=correction)
        rust = _kernels().transmission_probability_of(
            hazard,
            trust,
            basis == "family",
            basis == "adjacent_zone",
            minutes,
            correction,
            float(config.contact_duration_scale_minutes),
            config.base_transmission_probability,
            config.correction_transmission_multiplier,
            config.trust_sensitivity,
            config.family_contact_multiplier,
            config.adjacent_zone_contact_multiplier,
        )
        assert rust.hex() == python.hex()
        assert python == 0.0 or (rust * 1.01).hex() != python.hex()


# ---- What the kernel leaves to the Python rule --------------------------------------------------


@needs_kernels
def test_what_the_kernel_does_not_handle_changes_nothing(generated_scenario: Any) -> None:
    world = _world_with_claims(generated_scenario)
    policy = InformationSpreadPolicy(InformationSpreadConfig(enabled=True))
    neighbors = {zone: () for zone in world.zones}
    before = world.canonical_json()

    class Installed(InformationSpreadPolicy):
        pass

    class OtherRNG(CounterRNG):
        pass

    traced = _controller(world, policy, neighbors)
    traced.artifacts.trace_level = TraceLevel.SUMMARY
    disabled = InformationSpreadPolicy(InformationSpreadConfig(enabled=False))
    for controller, rng, reason in (
        (traced, CounterRNG(1), "trace"),
        (_controller(world, Installed(policy.config), neighbors), CounterRNG(1), "model"),
        (_controller(world, policy, neighbors), OtherRNG(1), "CounterRNG"),
        (_controller(world, disabled, neighbors), CounterRNG(1), "enabled"),
    ):
        with pytest.raises(native.Unsupported, match=reason):
            _kernels().sample_contacts(controller, world.minute_index, rng)
    with pytest.raises(native.Unsupported, match="another minute"):
        _kernels().sample_contacts(_controller(world, policy, neighbors), 5, CounterRNG(1))
    assert world.canonical_json() == before


@needs_kernels
def test_no_model_is_nothing_to_do(generated_scenario: Any) -> None:
    world = WorldState.from_scenario(generated_scenario)
    before = world.canonical_json()
    assert _kernels().sample_contacts(_controller(world, None, {}), 0, CounterRNG(1)) is None
    assert world.canonical_json() == before


def _unparsable_states() -> list[Any]:
    def belief(**fields: Any) -> Any:
        return lambda state, first: state["beliefs"][first]["m1"].update(fields)

    return [
        belief(belief="doubted"),
        belief(source="not an id"),
        belief(belief_strength=1.5),
        belief(share_propensity=float("nan")),
        belief(belief="unknown"),
        belief(correction_accepted=True),
        belief(last_correction_accepted=True),
        belief(exposed=False),
        belief(last_contact=-1),
        lambda state, first: state["messages"]["m1"].update(truth_status="perhaps"),
        lambda state, first: state["messages"]["m1"].update(time=-1),
        lambda state, first: state["messages"]["m1"].update(source_id="-x"),
        lambda state, first: state["beliefs"][first].update({"m9": _belief("m9", "heard")}),
    ]


@needs_kernels
@pytest.mark.parametrize("corrupt", _unparsable_states())
def test_a_state_the_python_rule_may_raise_on_goes_to_it(
    generated_scenario: Any, corrupt: Any
) -> None:
    world = _world_with_claims(generated_scenario)
    corrupt(world.model_state[KEY], sorted(world.agents)[0])
    assert _mirror(world).tainted is None
    before = json.dumps(world.model_state[KEY])
    policy = InformationSpreadPolicy(InformationSpreadConfig(enabled=True))
    with pytest.raises(native.Unsupported):
        _kernels().sample_contacts(
            _controller(world, policy, {zone: () for zone in world.zones}), 0, CounterRNG(1)
        )
    assert json.dumps(world.model_state[KEY]) == before
    assert not rust_validated(world, KEY)


@needs_kernels
def test_a_tainted_namespace_runs_the_python_step(generated_scenario: Any) -> None:
    world = _run_ticks(generated_scenario, 2)
    state = world.model_state[KEY]
    state["query_pressure"][sorted(state["query_pressure"])[0]] = 0
    controller = _controller(world, None, {})
    controller.information_policy = InformationSpreadPolicy(InformationSpreadConfig(enabled=True))
    with pytest.raises(native.Unsupported, match="no row"):
        _kernels().sample_contacts(controller, world.minute_index, CounterRNG(1))


# ---- The validator ------------------------------------------------------------------------------


def _corruptions() -> list[Any]:
    def belief(**fields: Any) -> Any:
        return lambda state, agents: state["beliefs"][agents[0]]["m1"].update(fields)

    def contact(key: Any, **record: Any) -> Any:
        fields = {"last_seen_minute": 0, "consecutive_minutes": 1} | record
        return lambda state, agents: state["contacts"].__setitem__(
            json.dumps(key(agents), separators=(",", ":")), fields
        )

    def request(**fields: Any) -> Any:
        def change(state: dict[str, Any], agents: list[str]) -> None:
            state["support_requests"].append(
                {"request_id": "r", "agent_id": agents[0], "minute": 0, "message_ids": ["m1"]}
                | fields
            )
            state["counters"]["support_requests"] += 1

        return change

    return [
        lambda state, agents: state.__setitem__("schema_version", 3),
        lambda state, agents: state["messages"]["m1"].update(id="other"),
        lambda state, agents: state["messages"]["m1"].update(zone_id="nowhere"),
        lambda state, agents: state["messages"]["m1"].update(origin_agent_id="nobody"),
        lambda state, agents: state["messages"]["m1"].update(time=10**7),
        lambda state, agents: state["beliefs"].__setitem__("nobody", {}),
        lambda state, agents: state["beliefs"].pop(agents[0]),
        belief(message_id="m2"),
        belief(last_contact=10**7),
        belief(belief="doubted"),
        contact(lambda agents: ["nobody", agents[0]]),
        contact(lambda agents: [agents[0], agents[0]]),
        contact(lambda agents: agents[:2], last_seen_minute=10**7),
        contact(lambda agents: agents[:2], consecutive_minutes=0),
        contact(lambda agents: agents[:2], consecutive_minutes=2),
        lambda state, agents: state["contacts"].__setitem__(
            "not json", {"last_seen_minute": 0, "consecutive_minutes": 1}
        ),
        lambda state, agents: state["contacts"].__setitem__(
            json.dumps(agents[:2]), {"last_seen_minute": 0, "consecutive_minutes": 1}
        ),
        lambda state, agents: state["query_pressure"].pop(agents[0]),
        lambda state, agents: state["query_pressure"].__setitem__(agents[0], 1.5),
        lambda state, agents: state["query_pressure"].__setitem__("nobody", 0.0),
        request(request_id=""),
        request(agent_id="nobody"),
        request(minute=10**7),
        request(message_ids=[]),
        request(message_ids=["m1", "m1"]),
        request(message_ids=["m9"]),
        lambda state, agents: state["counters"].update(support_requests=1),
        lambda state, agents: state["counters"].update(contact_attempts=-1),
    ]


@needs_kernels
@pytest.mark.parametrize("corrupt", _corruptions())
def test_the_validator_never_takes_what_the_python_rule_turns_down(
    generated_scenario: Any, corrupt: Any
) -> None:
    world = _world_with_claims(generated_scenario)
    agents = sorted(world.agents)
    arguments = {
        "agent_ids": set(world.agents),
        "zone_ids": set(world.zones),
        "duration_minutes": world.duration_minutes,
    }
    assert rust_validated(world, KEY)
    validate_information_runtime_state(world.model_state[KEY], **arguments)

    plain = json.loads(json.dumps(world.model_state[KEY]))
    corrupt(plain, agents)
    with pytest.raises(ValueError) as expected:
        validate_information_runtime_state(plain, **arguments)

    corrupt(world.model_state[KEY], agents)
    assert not rust_validated(world, KEY)
    with pytest.raises(ValueError) as raised:
        validate_information_runtime_state(world.model_state[KEY], **arguments)
    assert str(raised.value) == str(expected.value)


@needs_kernels
def test_the_validator_takes_what_the_python_rule_passes(rumors: Any) -> None:
    def check(world: WorldState) -> None:
        assert rust_validated(world, KEY)
        validate_information_runtime_state(
            world.model_state[KEY],
            agent_ids=set(world.agents),
            zone_ids=set(world.zones),
            duration_minutes=world.duration_minutes,
        )

    _run_ticks(rumors(duration=40), 40, check)


def test_a_corrupted_namespace_fails_the_world_with_the_exact_error(
    generated_scenario: Any,
) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[KEY]["schema_version"] = 99
    with pytest.raises(StateInvariantError) as raised:
        world.validate(validate_event_history=False)
    assert str(raised.value) == (
        "invalid information-spread state: unsupported information-spread runtime state schema"
    )
