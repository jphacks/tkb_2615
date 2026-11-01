"""social_network_v1 owned by Rust, with its rules (docs/rust-port/lanes/m3-social-network.md).

Every call of the gate and of seven days against the Python rules is
benchmarks/rule_shadow/shadow_social_network.py; here, what taints the row, each way into it that
the per-tick check must see now that it reads only the written records, what the kernels leave to
Python, and random inputs compared with the Python rules by their bits.
"""

from __future__ import annotations

import copy
import json
import random
from types import SimpleNamespace
from typing import Any

import pytest
from support.paths import BENCHMARKS

from sheltersim import native
from sheltersim.core.engine import TraceLevel
from sheltersim.core.state_mirror import owned_row_bytes, rust_validated
from sheltersim.domain import social_dialogue, social_network
from sheltersim.domain.models import ordered_pair_key
from sheltersim.domain.simulator_state import validate_simulator_extensions
from sheltersim.domain.social_network import SOCIAL_NETWORK_STATE_KEY as KEY
from sheltersim.events.handlers import EventSuppressed, SocialInteractionHandler
from sheltersim.events.models import EventKind, ScheduledEvent
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

pytestmark = pytest.mark.skipif(not native.available(), reason="needs the Rust kernels")

SOURCE_ID = "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1"
ACTIONS = {
    "reassurance_exchange": ("offer_reassurance", "emotional_check_in"),
    "family_care": ("provide_family_support", "family_care"),
    "voluntary_support": ("offer_practical_help", "mutual_aid"),
}


@pytest.fixture(scope="module")
def finished(generated_scenario_preview: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    document = copy.deepcopy(generated_scenario_preview["scenario"])
    document["duration_minutes"] = 40
    return run_scenario(
        ScenarioSpec.model_validate(document),
        tmp_path_factory.mktemp("run"),
        trace_level=TraceLevel.OFF,
    )


@pytest.fixture
def world(finished: Any) -> Any:
    return copy.deepcopy(finished)


def _mirror(world: Any) -> Any:
    return world._core.mirror(KEY)


def _rules() -> Any:
    return native.rules("social_network")


def _record(**changes: Any) -> dict[str, Any]:
    return {
        "contact_count": 1,
        "conversation_count": 0,
        "cooperation_count": 0,
        "trust": 0.5,
        "shared_message_ids": [],
        "last_contact_minute": 3,
        "last_interaction": None,
        **changes,
    }


def _accepted(world: Any) -> bool:
    return native.state("social_network").validate(world) is True


def _people(world: Any, count: int = 2) -> list[str]:
    return sorted(world.agents)[:count]


def _colocate(world: Any, ids: list[str]) -> str:
    zone = world.agents[ids[0]].zone_id
    for id_ in ids:
        world.agents[id_].zone_id = zone
    return zone


def _event(
    event_type: str, payload: dict[str, Any], minute: int, number: int = 1
) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=f"e{number}",
        kind=EventKind.ENDOGENOUS,
        due_minute=minute,
        priority=0,
        sequence=number,
        event_type=event_type,
        payload=payload,
        source_id="policy-v1",
        provenance={
            "source_id": SOURCE_ID,
            "source_type": "assumption",
            "policy_version": "policy-v1",
        },
    )


def _action(
    world: Any, kind: str, actor: str, participant: str, number: int = 1, **more: Any
) -> Any:
    action, topic = ACTIONS[kind]
    return _event(
        "social_interaction",
        {
            "interaction_type": kind,
            "agent_id": actor,
            "participant_agent_id": participant,
            "zone_id": world.agents[actor].zone_id,
            "topic_id": topic,
            "action": action,
            "reason_short": "autonomous_need_choice",
            "interaction_duration_minutes": 5,
            **more,
        },
        world.minute_index,
        number,
    )


# ---- The row ---------------------------------------------------------------------------------


def test_a_real_run_fills_the_row_and_the_save_uses_its_bytes(finished: Any) -> None:
    mirror = _mirror(finished)
    assert finished.model_state[KEY]["relationships"]
    assert mirror.verify()
    assert mirror.row_json() == native.canonical_json(finished.model_state[KEY])
    assert owned_row_bytes(finished)[1][KEY] == native.canonical_json(finished.model_state[KEY])
    assert _accepted(finished)


def test_a_run_writes_the_namespace_whole_only_when_it_starts(finished: Any) -> None:
    assert _mirror(finished).stats()["whole_writes"] <= 2


@pytest.mark.parametrize(
    "change",
    [
        {"trust": 1},
        {"contact_count": 1.0},
        {"contact_count": True},
        {"shared_message_ids": [3]},
        {"last_interaction": 7},
        {"last_contact_minute": 2.0},
        {"more": 1},
    ],
)
def test_a_value_of_another_type_taints_and_python_still_decides(world: Any, change: dict) -> None:
    relationships = world.model_state[KEY]["relationships"]
    relationships['["x1","x2"]'] = _record(**change)
    assert _mirror(world).row_json() is None
    with pytest.raises(native.Unsupported):
        native.state("social_network").validate(world)
    assert not rust_validated(world, KEY)
    try:
        social_network.validate_social_network_state(world)
    except ValueError as error:
        assert "social_network_v1" in str(error)
    # The kernels leave a tainted row to the Python rules, which write the tree.
    first, second = _people(world)
    event = _event(
        "information_contact_update",
        {"recipient_agent_id": first, "sender_agent_id": second},
        world.minute_index,
    )
    with pytest.raises(native.Unsupported):
        _rules().observe_event(world, event, world.minute_index)
    before = world.model_state[KEY]["interaction_count"]
    social_network.observe_event(world, event, world.minute_index)
    assert world.model_state[KEY]["interaction_count"] == before + 1


@pytest.mark.parametrize(
    "write",
    [
        lambda state, record: record.__setitem__("trust", 2.0),
        lambda state, record: record.__setitem__("trust", float("nan")),
        lambda state, record: record.__setitem__("contact_count", -1),
        lambda state, record: record.__setitem__("last_contact_minute", -1),
        lambda state, record: record.update(conversation_count=-3),
        lambda state, record: state["relationships"].__setitem__('["x1","x1"]', _record()),
        lambda state, record: state["relationships"].__setitem__('["x1"]', _record()),
        lambda state, record: state["relationships"].update({"[": _record()}),
        lambda state, record: state["relationships"].setdefault('["a", "b"]', _record()),
        lambda state, record: state.__setitem__("relationships", {"[": _record()}),
        lambda state, record: state.__setitem__("schema_version", 2),
        lambda state, record: state.__setitem__("policy_version", "other"),
    ],
)
def test_every_write_the_check_of_a_tick_must_see_after_it_has_passed(
    world: Any, write: Any
) -> None:
    """The check reads only written records, so each way of writing has to reach it. What stops
    the world is the Python rule's own text, the same as with no kernel."""
    state = world.model_state[KEY]
    assert _accepted(world)
    assert _accepted(world)
    write(state, next(iter(state["relationships"].values())))
    assert not _accepted(world)
    # It stays refused: the bad record is not forgotten by having been read once.
    assert not _accepted(world)
    with pytest.raises(ValueError, match="social_network_v1") as rust_way:
        validate_simulator_extensions(world)
    with pytest.raises(ValueError, match="social_network_v1") as python_way:
        social_network.validate_social_network_state(world)
    assert str(rust_way.value) == str(python_way.value)


def test_a_bad_record_read_again_passes_once_it_is_fixed_or_gone(world: Any) -> None:
    relationships = world.model_state[KEY]["relationships"]
    key = next(iter(relationships))
    assert _accepted(world)
    relationships[key]["trust"] = 2.0
    assert not _accepted(world)
    relationships[key]["trust"] = 0.25
    assert _accepted(world)
    relationships['["x1","x1"]'] = _record()
    assert not _accepted(world)
    del relationships['["x1","x1"]']
    assert _accepted(world)


def test_more_writes_than_it_lists_still_reach_the_check(world: Any) -> None:
    record = next(iter(world.model_state[KEY]["relationships"].values()))
    assert _accepted(world)
    record["trust"] = 2.0
    for _ in range(5000):
        record["contact_count"] += 1
    assert not _accepted(world)
    record["trust"] = 0.5
    assert _accepted(world)


def test_a_restored_copy_is_checked_from_the_start(world: Any) -> None:
    world.model_state[KEY]["relationships"]['["x1","x1"]'] = _record()
    assert not _accepted(world)
    assert not _accepted(copy.deepcopy(world))


def test_the_kernel_takes_no_key_python_refuses(world: Any) -> None:
    good = native.state("social_network").key_is_good
    keys = [
        '["a","b"]',
        '["a","a"]',
        '["a"]',
        '["a","b","c"]',
        '["a", "b"]',
        '[ "a","b"]',
        '["a","b"] ',
        '["a\\"","b"]',
        '["\\u3042","b"]',
        '["あ","b"]',
        '["a\\n","b"]',
        '["a\x7f","b"]',
        '["","b"]',
        '["",""]',
        '["a",1]',
        '{"a":"b"}',
        "[",
        "",
    ]
    rng = random.Random(7)
    alphabet = '[]",\\ab \x7fあ'
    keys += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))) for _ in range(5000)]
    keys += [
        '["' + "".join(rng.choice('ab",\\') for _ in range(rng.randint(0, 8))) + '"]'
        for _ in range(5000)
    ]
    accepted = 0
    for key in keys:
        if good(key):
            accepted += 1
            assert social_network._pair_key_problem(key) is None, key
    assert accepted >= 10


def test_python_writes_into_the_row(world: Any) -> None:
    state = world.model_state[KEY]
    key = next(iter(state["relationships"]))
    state["relationships"][key]["shared_message_ids"].append("m1")
    state["interaction_count"] += 1
    assert _mirror(world).row_json() == native.canonical_json(state)
    popped = state["relationships"].pop(key)
    assert popped["shared_message_ids"][-1] == "m1"
    assert _mirror(world).row_json() == native.canonical_json(state)


# ---- The rules against Python ------------------------------------------------------------------


def _random_body(rng: random.Random, worlds: tuple[Any, Any], ids: list[str]) -> None:
    affect = {name: rng.random() for name in ("positive_affect", "negative_affect")}
    for id_ in ids:
        for name, value in affect.items():
            for each in worlds:
                each.model_state["body_state_v1"]["agents"][id_][name] = value


def _random_event(rng: random.Random, world: Any, ids: list[str], number: int) -> ScheduledEvent:
    first, second = rng.sample(ids, 2)
    kind = rng.randrange(7)
    minute = world.minute_index
    if kind == 0:
        message = rng.choice([None, f"m{rng.randrange(80)}", f"m{number}"])
        payload = {"recipient_agent_id": first, "sender_agent_id": second}
        if message is not None:
            payload["message_id"] = message
        return _event("information_contact_update", payload, minute, number)
    if kind == 1:
        return _event(
            "proximity_contact_update",
            {"first_agent_id": first, "second_agent_id": second, "observed_minute": minute},
            minute,
            number,
        )
    if kind == 2:
        return _event("information_correction", {"message_id": "m1"}, minute, number)
    interaction = (
        "reassurance_exchange",
        "family_care",
        "voluntary_support",
        "cooperation",
    )[kind - 3]
    return _event(
        "social_interaction",
        {"interaction_type": interaction, "agent_id": first, "participant_agent_id": second},
        minute,
        number,
    )


def test_observe_event_gives_the_bits_of_the_python_rule(world: Any) -> None:
    other = copy.deepcopy(world)
    rng = random.Random(11)
    ids = _people(world, 6)
    ran = 0
    for number in range(1500):
        _random_body(rng, (world, other), ids)
        event = _random_event(rng, world, ids, number + 1)
        minute = world.minute_index
        before = _rules().observe_event(world, event, minute)
        assert before is None
        ran += 1
        social_network.observe_event.__wrapped__(other, event, minute)
    assert ran == 1500
    ours, python = world.model_state[KEY], other.model_state[KEY]
    assert json.dumps(ours, sort_keys=True) == json.dumps(python, sort_keys=True)
    assert list(ours["relationships"]) == list(python["relationships"])
    assert _mirror(world).verify()
    assert _accepted(world)
    # The cap of the message IDs was reached, or the test proves little.
    assert any(len(record["shared_message_ids"]) >= 10 for record in ours["relationships"].values())


def test_observe_event_computes_trust_when_the_body_has_no_record(world: Any) -> None:
    other = copy.deepcopy(world)
    first, second = _people(world)
    for each in (world, other):
        for key in (ordered_pair_key(first, second), ordered_pair_key(second, first)):
            each.model_state["body_state_v1"]["relationships"].pop(key, None)
            each.model_state[KEY]["relationships"].pop(key, None)
    for number, kind in enumerate(
        [
            "cooperation",
            "voluntary_support",
            "family_care",
            "reassurance_exchange",
            "information_reaction",
        ]
    ):
        event = _event(
            "social_interaction",
            {"interaction_type": kind, "agent_id": first, "participant_agent_id": second},
            world.minute_index,
            number + 1,
        )
        _rules().observe_event(world, event, world.minute_index)
        social_network.observe_event.__wrapped__(other, event, world.minute_index)
        assert json.dumps(world.model_state[KEY], sort_keys=True) == json.dumps(
            other.model_state[KEY], sort_keys=True
        )
    record = world.model_state[KEY]["relationships"][ordered_pair_key(first, second)]
    assert record["cooperation_count"] == 1 and record["last_interaction"] == "information_reaction"
    assert record["trust"] is not None
    # A 1% change of the transfer shows.
    third = copy.deepcopy(other)
    config = third.model_state["body_state_v1"]["config"]
    config["social_action_affect_transfer"] *= 1.01
    event = _event(
        "social_interaction",
        {"interaction_type": "family_care", "agent_id": first, "participant_agent_id": second},
        third.minute_index,
    )
    for each in (third, other):
        for key in list(each.model_state["body_state_v1"]["relationships"]):
            del each.model_state["body_state_v1"]["relationships"][key]
    social_network.observe_event.__wrapped__(other, event, other.minute_index)
    _rules().observe_event(third, event, third.minute_index)
    assert json.dumps(third.model_state[KEY], sort_keys=True) != json.dumps(
        other.model_state[KEY], sort_keys=True
    )


def test_the_message_list_keeps_its_last_64_in_row_and_tree(world: Any) -> None:
    first, second = _people(world)
    key = ordered_pair_key(first, second)
    for number in range(70):
        event = _event(
            "information_contact_update",
            {"recipient_agent_id": first, "sender_agent_id": second, "message_id": f"n{number}"},
            world.minute_index,
            number + 1,
        )
        _rules().observe_event(world, event, world.minute_index)
    ids = world.model_state[KEY]["relationships"][key]["shared_message_ids"]
    assert ids[0] == "n6" and ids[-1] == "n69" and len(ids) == 64
    assert _mirror(world).verify()


@pytest.mark.parametrize(
    "payload",
    [
        {"recipient_agent_id": "a", "sender_agent_id": "a"},
        {"recipient_agent_id": "a"},
        {"recipient_agent_id": 3, "sender_agent_id": "a"},
        {"recipient_agent_id": "a", "sender_agent_id": "b", "message_id": 4},
        {"recipient_agent_id": "あ", "sender_agent_id": "b"},
    ],
)
def test_observe_event_leaves_odd_payloads_to_python_with_nothing_written(
    world: Any, payload: dict
) -> None:
    event = SimpleNamespace(event_type="information_contact_update", payload=payload)
    before = _mirror(world).row_json()
    with pytest.raises(native.Unsupported):
        _rules().observe_event(world, event, world.minute_index)
    assert _mirror(world).row_json() == before
    social_network.observe_event(world, event, world.minute_index)
    assert _mirror(world).verify()


def test_at_the_bound_the_python_rule_says_so(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _people(world)
    event = _event(
        "information_contact_update",
        {"recipient_agent_id": first, "sender_agent_id": second},
        world.minute_index,
    )
    relationships = world.model_state[KEY]["relationships"]
    relationships.clear()
    relationships.update({f'["p{index}","q"]': _record() for index in range(100_000)})
    before = _mirror(world).row_json()
    with pytest.raises(native.Unsupported):
        _rules().observe_event(world, event, world.minute_index)
    assert _mirror(world).row_json() == before
    with pytest.raises(ValueError, match="reached the configured relationship bound"):
        social_network.observe_event(world, event, world.minute_index)


def test_record_observed_contacts_comes_through_the_social_step_without_python(
    finished: Any,
) -> None:
    """The step writes the row itself: forwards are the population's and the first tick's."""
    assert _mirror(finished).stats()["forwards"] < 50


# ---- The state context of a social event -------------------------------------------------------


def test_capture_gives_what_the_python_rule_gives(world: Any) -> None:
    first, second = _people(world)
    kernel = native.rules("social_dialogue").capture_context
    python = social_dialogue.capture_social_state_context.__wrapped__
    for ids in ((first, second), [second, first], (first,), (first, first)):
        assert json.dumps(kernel(world, ids)) == json.dumps(python(world, ids))
    del world.model_state["body_state_v1"]["agents"][first]["hygiene_need"]
    assert json.dumps(kernel(world, (first, second))) == json.dumps(python(world, (first, second)))
    assert kernel(world, (first, second))["participants"][first]["affect"] is None


@pytest.mark.parametrize(
    ("ids", "text"),
    [
        ((), "one or two"),
        (("a", "b", "c"), "one or two"),
        (("nobody",), "one or two"),
    ],
)
def test_capture_leaves_a_refusal_to_python(world: Any, ids: tuple, text: str) -> None:
    with pytest.raises(native.Unsupported):
        native.rules("social_dialogue").capture_context(world, ids)
    with pytest.raises(ValueError, match=text):
        social_dialogue.capture_social_state_context(world, ids)


def test_a_body_value_out_of_bounds_is_python_s_to_refuse(world: Any) -> None:
    first, second = _people(world)
    world.model_state["body_state_v1"]["agents"][first]["discontent"] = 1.5
    with pytest.raises(native.Unsupported):
        native.rules("social_dialogue").capture_context(world, (first, second))
    with pytest.raises(ValueError, match="finite and within 0..1"):
        social_dialogue.capture_social_state_context(world, (first, second))


def _context(world: Any, ids: tuple[str, str]) -> dict[str, Any]:
    return social_dialogue.capture_social_state_context(world, ids)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda c, i: c.__setitem__("schema_version", 2),
        lambda c, i: c.__setitem__("captured_minute", -1),
        lambda c, i: c.__setitem__("captured_minute", 10**6),
        lambda c, i: c.__setitem__("captured_minute", True),
        lambda c, i: c.__setitem__("more", 1),
        lambda c, i: c["participants"].pop(i),
        lambda c, i: c["participants"].__setitem__("other", c["participants"].pop(i)),
        lambda c, i: c["participants"][i].__setitem__("stress", 1.5),
        lambda c, i: c["participants"][i].__setitem__("trust", float("nan")),
        lambda c, i: c["participants"][i].__setitem__("stress", True),
        lambda c, i: c["participants"][i]["needs"].__setitem__("hunger", -0.5),
        lambda c, i: c["participants"][i]["needs"].pop("hunger"),
        lambda c, i: c["participants"][i].__setitem__("affect", {"positive_affect": 0.5}),
        lambda c, i: c["participants"][i].pop("affect"),
    ],
)
def test_validation_of_a_context_takes_nothing_python_refuses(world: Any, spoil: Any) -> None:
    ids = tuple(_people(world))
    context = _context(world, ids)
    kernel = native.rules("social_dialogue").validate_context
    assert kernel(context, ids, world.minute_index) is None
    assert kernel(_event("x", {"c": context}, 0).payload["c"], ids, world.minute_index) is None
    spoil(context, ids[0])
    with pytest.raises(native.Unsupported):
        kernel(context, ids, world.minute_index)
    with pytest.raises(ValueError):
        social_dialogue.validate_social_state_context(context, ids, world.minute_index)


# ---- The social action of the handler ----------------------------------------------------------


def _values(world: Any, ids: list[str]) -> list[str]:
    return [
        value.hex()
        for id_ in ids
        for value in (
            world.agents[id_].stress,
            world.agents[id_].trust,
            world.agents[id_].needs.fatigue,
        )
    ]


def _apply_kernel(world: Any, event: Any) -> None:
    native.rules("social_interaction").apply(SocialInteractionHandler(), world, event)


def _apply_python(world: Any, event: Any) -> None:
    SocialInteractionHandler.apply.__wrapped__(SocialInteractionHandler(), world, event)


@pytest.mark.parametrize("kind", ["reassurance_exchange", "voluntary_support", "family_care"])
def test_a_social_action_gives_the_bits_of_the_python_rule(world: Any, kind: str) -> None:
    ids = _people(world)
    _colocate(world, ids)
    actor, participant = ids
    presence = world.model_state.get("population_presence_v1")
    if presence is not None:
        for id_ in ids:
            presence["people"].setdefault(id_, {})["household_id"] = "h1"
    else:
        for id_ in ids:
            world.agents[id_].profile.group_id = "h1"
    other = copy.deepcopy(world)
    rng = random.Random(3)
    ran = 0
    for number in range(400):
        stress, trust, fatigue = rng.random(), rng.random(), rng.random() * 0.2
        positive, negative = rng.random(), rng.random()
        for each in (world, other):
            each.agents[actor].stress = fatigue
            each.agents[actor].trust = trust
            each.agents[actor].needs.fatigue = fatigue
            each.agents[actor].needs.hunger = 0.0
            each.agents[actor].needs.toilet_urgency = 0.0
            each.agents[actor].needs.hydration_need = 0.0
            each.agents[participant].stress = max(stress, 0.6)
            each.agents[participant].trust = trust
            record = each.model_state["body_state_v1"]["agents"][actor]
            record["positive_affect"], record["negative_affect"] = positive, negative
        event = _action(world, kind, actor, participant, number + 1)
        _apply_kernel(world, event)
        _apply_python(other, event)
        ran += 1
        assert _values(world, ids) == _values(other, ids)
    assert ran == 400
    assert world._core.entities().verify(world) is None


def test_a_1_percent_change_of_the_transfer_shows_in_the_action(world: Any) -> None:
    ids = _people(world)
    _colocate(world, ids)
    other = copy.deepcopy(world)
    other.model_state["body_state_v1"]["config"]["social_action_affect_transfer"] *= 1.01
    record = world.model_state["body_state_v1"]["agents"][ids[0]]
    record["positive_affect"], record["negative_affect"] = 0.9, 0.1
    record = other.model_state["body_state_v1"]["agents"][ids[0]]
    record["positive_affect"], record["negative_affect"] = 0.9, 0.1
    for each in (world, other):
        each.agents[ids[1]].stress = 0.5
    event = _action(world, "reassurance_exchange", *ids)
    _apply_kernel(world, event)
    _apply_python(other, event)
    assert _values(world, ids) != _values(other, ids)


def test_a_suppressed_action_is_python_s_to_say_with_nothing_written(world: Any) -> None:
    ids = _people(world)
    zone = _colocate(world, ids)
    event = _action(world, "reassurance_exchange", *ids)
    others = [name for name in world.zones if name != zone]
    world.agents[ids[1]].zone_id = others[0]
    before = _values(world, ids)
    with pytest.raises(native.Unsupported):
        _apply_kernel(world, event)
    assert _values(world, ids) == before
    with pytest.raises(EventSuppressed, match="no longer co-located"):
        SocialInteractionHandler().apply(world, event)


def test_a_helper_who_needs_more_than_the_target_is_suppressed_by_python(world: Any) -> None:
    ids = _people(world)
    _colocate(world, ids)
    world.agents[ids[0]].stress = 0.9
    world.agents[ids[1]].stress = 0.1
    for name in ("toilet_urgency", "hunger", "fatigue", "hydration_need"):
        setattr(world.agents[ids[1]].needs, name, 0.1)
    event = _action(world, "voluntary_support", *ids)
    with pytest.raises(native.Unsupported):
        _apply_kernel(world, event)
    with pytest.raises(EventSuppressed, match="higher immediate need"):
        SocialInteractionHandler().apply(world, event)


def test_a_different_household_is_suppressed_by_python(world: Any) -> None:
    ids = _people(world)
    _colocate(world, ids)
    presence = world.model_state.get("population_presence_v1")
    if presence is not None:
        presence["people"].setdefault(ids[0], {})["household_id"] = "h1"
        presence["people"].setdefault(ids[1], {})["household_id"] = "h2"
    else:
        world.agents[ids[0]].profile.group_id = "h1"
        world.agents[ids[1]].profile.group_id = "h2"
    event = _action(world, "family_care", *ids)
    with pytest.raises(native.Unsupported):
        _apply_kernel(world, event)
    with pytest.raises(EventSuppressed, match="same household"):
        SocialInteractionHandler().apply(world, event)


@pytest.mark.parametrize(
    "more",
    [
        {"interaction_duration_minutes": 0},
        {"interaction_duration_minutes": True},
        {"reason_short": "other"},
        {"action": "other"},
        {"extra": 1},
        {"conversation_state_context": {"schema_version": 2}},
    ],
)
def test_a_bad_payload_is_python_s_to_refuse(world: Any, more: dict) -> None:
    ids = _people(world)
    _colocate(world, ids)
    event = _action(world, "reassurance_exchange", *ids, **more)
    before = _values(world, ids)
    with pytest.raises(native.Unsupported):
        _apply_kernel(world, event)
    assert _values(world, ids) == before
    with pytest.raises(Exception, match="social"):
        SocialInteractionHandler().apply(world, event)


def test_the_social_shadow_ignores_key_order_only_in_a_run_that_reverses_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(BENCHMARKS / "rule_shadow"))
    from social_propagation_shadow import _text

    first, second = {"a": 1, "b": 2}, {"b": 2, "a": 1}
    monkeypatch.delenv("SHELTERSIM_GATE_REVERSE", raising=False)
    assert _text(first) != _text(second)
    monkeypatch.setenv("SHELTERSIM_GATE_REVERSE", "phase")
    assert _text(first) == _text(second)
    assert _text({"a": 1.0}) != _text({"a": 1})
