"""`SocialInteractionHandler`'s suppressed and failed texts, in the Python rule's order of checks.

These texts are saved: the dispatcher writes them to the event's record as `detail`. Each case
gives an event that fails one check, often together with later ones, and checks the text of the
first check that fails. With the kernels, the kernel must raise it itself, change nothing, and
match the Python rule on a copy (support/world.py `kernel_says_python_s_text`); without them, the
Python rule must raise it. A social action between two people runs every tick, so its kernel checks
in its own order on the way to an applied action and finds Python's first failing check only when
one fails (docs/rust-port/lanes/l8-texts-events.md).
"""

from __future__ import annotations

import copy
import itertools
import sys
from pathlib import Path
from typing import Any

import pytest
from support.world import kernel_says_python_s_text

from sheltersim import native
from sheltersim.core.engine import TickEngine
from sheltersim.core.world import WorldState
from sheltersim.domain.models import Activity, SupportRequestState, SupportRequestStatus
from sheltersim.domain.social_dialogue import capture_social_state_context
from sheltersim.events.handlers import SocialInteractionHandler
from sheltersim.events.models import (
    EventApplicationError,
    EventKind,
    EventLogRecord,
    EventStatus,
    EventSuppressed,
    ScheduledEvent,
)
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController
from sheltersim.experiments.timeline import _MetricSink

SOURCE = "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1"
_DROP = object()
_SEQUENCES = itertools.count(900_010)

UNKNOWN_AGENT = "typed social interaction references an unknown Agent"
NO_REASON = "typed social interaction requires a reason code"
CONTEXT = "social dialogue state context is invalid"


def _gate() -> Any:
    root = str(Path(__file__).resolve().parents[2] / "benchmarks")
    if root not in sys.path:
        sys.path.insert(0, root)
    import gate_scenarios

    return gate_scenarios


def _run(scenario: Any, minutes: int) -> WorldState:
    world = WorldState.from_scenario(scenario)
    controller = OfflineSimulationController(world, _MetricSink(MetricsRecorder()))
    engine = TickEngine(world, phase_handler=controller)
    for _ in range(minutes):
        engine.step()
    return world


@pytest.fixture
def small_world() -> WorldState:
    return _run(_gate().generated(evacuees=20, minutes=120), 5)


@pytest.fixture
def rumor_world() -> WorldState:
    """At minute 40: three claims, heard by many, no correction yet."""
    gate = _gate()
    return _run(gate.with_rumors(gate.generated(evacuees=20, minutes=120)), 40)


def _copy(world: WorldState) -> WorldState:
    """A copy to change. Without the kernels a world does not deep-copy; each test then has its
    own world, and a Python rule that raises changes nothing."""
    return copy.deepcopy(world) if native.available() else world


def _social(world: WorldState, payload: dict[str, Any], **fields: Any) -> ScheduledEvent:
    payload = {key: value for key, value in payload.items() if value is not _DROP}
    return ScheduledEvent(
        event_id=fields.pop("event_id", "social-test"),
        kind=fields.pop("kind", EventKind.ENDOGENOUS),
        due_minute=fields.pop("due_minute", world.minute_index),
        priority=0,
        sequence=900_001,
        event_type="social_interaction",
        payload=payload,
        source_id=fields.pop("source_id", "social-v1"),
        causation_id=fields.pop("causation_id", None),
        provenance=fields.pop(
            "provenance",
            {"source_id": SOURCE, "source_type": "assumption", "policy_version": "social-v1"},
        ),
    )


def _says(world: WorldState, event: ScheduledEvent, text: str) -> None:
    """The first failing check's text: the kernel's own, or the Python rule's without kernels."""
    world = _copy(world)
    handler = SocialInteractionHandler()
    if native.available():
        _, said = kernel_says_python_s_text(handler, world, event)
    else:
        with pytest.raises((EventSuppressed, EventApplicationError)) as raised:
            handler.apply(world, event)
        said = str(raised.value)
    assert said == text, (event.payload, said)


def _applies_as_python(world: WorldState, event: ScheduledEvent) -> WorldState:
    """The event applies, and with the kernels it writes what the Python rule writes."""
    handler = SocialInteractionHandler()
    if not native.available():
        handler.apply(world, event)
        return world
    world = copy.deepcopy(world)
    twin = copy.deepcopy(world)
    handler.apply(world, event)
    type(handler).apply.__wrapped__(handler, twin, event)
    assert world._canonical_text(world._canonical_data()) == twin._canonical_text(
        twin._canonical_data()
    )
    return world


def _pair(world: WorldState) -> tuple[str, str]:
    """Two people in one zone and one household."""
    first, second = sorted(world.agents)[:2]
    world.agents[second].zone_id = world.agents[first].zone_id
    _household(world, first, "h1")
    _household(world, second, "h1")
    return first, second


def _household(world: WorldState, agent_id: str, household: str) -> None:
    presence = world.model_state.get("population_presence_v1")
    if presence is not None:
        presence["people"].setdefault(agent_id, {})["household_id"] = household
    else:
        world.agents[agent_id].profile.group_id = household


def _other_zone(world: WorldState, zone: str) -> str:
    return next(name for name in sorted(world.zones) if name != zone)


def _open_request(world: WorldState, agent_id: str, *, age: int) -> str:
    for key in list(world.active_tasks):
        if world.active_tasks[key].agent_id == agent_id:
            del world.active_tasks[key]
    agent = world.agents[agent_id]
    agent.activity = Activity.SEEKING_HELP
    agent.target_id = None
    agent.support_request_id = "support-t1"
    world.support_requests["support-t1"] = SupportRequestState(
        "support-t1", agent_id, world.minute_index - age, world.minute_index + 50
    )
    return "support-t1"


def _contact(world: WorldState, sender: str, recipient: str, **fields: Any) -> str:
    """An applied information contact a minute ago, which cooperation and reactions answer."""
    event_id = fields.pop("event_id", "contact-test")
    minute = fields.pop("minute", world.minute_index - 1)
    contact = ScheduledEvent(
        event_id=event_id,
        kind=EventKind.ENDOGENOUS,
        due_minute=minute,
        priority=0,
        sequence=next(_SEQUENCES),
        event_type=fields.pop("event_type", "information_contact_update"),
        payload={"sender_agent_id": sender, "recipient_agent_id": recipient},
        source_id="SRC-TEST",
        provenance={"source_type": "assumption", "source_id": "SRC-TEST"},
    )
    world.event_log.append(
        EventLogRecord(
            event=contact, realized_minute=minute, status=fields.pop("status", EventStatus.APPLIED)
        )
    )
    return event_id


# ---- An event without a typed interaction -------------------------------------------------------


def test_an_untyped_social_event_s_texts_are_python_s(small_world: WorldState) -> None:
    world = _copy(small_world)
    owner, other = sorted(world.agents)[:2]
    request_id = _open_request(world, owner, age=0)
    base = {"agent_id": owner, "request_id": request_id, "action": "offer_reassurance"}
    ids = "social interaction requires Agent, request, and action IDs"
    unknown = "social interaction references an unknown request or Agent"
    for change, text in (
        ({"agent_id": _DROP}, ids),
        ({"agent_id": "", "request_id": "nothing"}, ids),
        ({"request_id": 5}, ids),
        ({"action": None}, ids),
        ({"action": ""}, ids),
        ({"request_id": "nothing", "action": "dance"}, unknown),
        ({"agent_id": other}, unknown),
        ({"agent_id": "nobody"}, unknown),
        ({"action": "dance"}, "social interaction action is not allowed"),
        ({"interaction_type": None, "action": "dance"}, "social interaction action is not allowed"),
    ):
        _says(world, _social(world, {**base, **change}), text)


# ---- A social action between two people ---------------------------------------------------------

ACTIONS = {
    "reassurance_exchange": ("offer_reassurance", "emotional_check_in"),
    "family_care": ("provide_family_support", "family_care"),
    "voluntary_support": ("offer_practical_help", "mutual_aid"),
}
FIELDS = "autonomous social action payload fields do not match"
APART = "social action participants are no longer co-located"
TOPIC = "social action does not match its typed topic"
REASON = "social action reason code is not allowed"
DURATION = "social action duration must be within 1..1440 minutes"
HOUSEHOLD = "family support target is no longer in the same household"
NEED = "the selected person no longer has a higher immediate need"


def _action(world: WorldState, kind: str, actor: str, participant: str, **change: Any) -> Any:
    action, topic = ACTIONS[kind]
    payload = {
        "interaction_type": kind,
        "agent_id": actor,
        "participant_agent_id": participant,
        "zone_id": world.agents[actor].zone_id,
        "topic_id": topic,
        "action": action,
        "reason_short": "autonomous_need_choice",
        "interaction_duration_minutes": 5,
    }
    return _social(world, {**payload, **change})


def _context(world: WorldState, ids: list[str], **change: Any) -> dict[str, Any]:
    """A captured context, with some participant values replaced."""
    context = copy.deepcopy(capture_social_state_context(world, ids))
    for path, value in change.items():
        person, *keys = path.split(".")
        target = context["participants"][ids[int(person)]]
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return context


def test_a_social_action_s_texts_come_in_python_s_order(small_world: WorldState) -> None:
    world = _copy(small_world)
    actor, participant = _pair(world)
    zone = world.agents[actor].zone_id
    both = _context(world, [actor, participant])
    alone = _context(world, [actor])

    def changed(**change: Any) -> dict[str, Any]:
        return _context(world, [actor, participant], **change)

    stale = {**both, "captured_minute": world.minute_index + 1}
    cases: list[tuple[dict[str, Any], str]] = [
        # The reason code and the Agent come before everything else.
        ({"reason_short": None, "agent_id": "nobody"}, NO_REASON),
        ({"reason_short": _DROP, "extra": 1}, NO_REASON),
        ({"agent_id": "nobody"}, UNKNOWN_AGENT),
        ({"agent_id": "nobody", "extra": 1}, UNKNOWN_AGENT),
        (
            {"agent_id": "nobody", "conversation_state_context": {"schema_version": 2}},
            UNKNOWN_AGENT,
        ),
        ({"agent_id": 5, "action": "other"}, UNKNOWN_AGENT),
        ({"agent_id": _DROP}, UNKNOWN_AGENT),
        # Then the dialogue context.
        ({"conversation_state_context": {"schema_version": 2}, "extra": 1}, CONTEXT),
        ({"conversation_state_context": "x", "participant_agent_id": "nobody"}, CONTEXT),
        ({"conversation_state_context": None}, CONTEXT),
        ({"conversation_state_context": [1]}, CONTEXT),
        ({"conversation_state_context": stale}, CONTEXT),
        ({"conversation_state_context": {**both, "schema_version": True}, "action": "x"}, TOPIC),
        ({"conversation_state_context": {**both, "schema_version": 1.0}, "action": "x"}, TOPIC),
        ({"conversation_state_context": {**both, "schema_version": "1"}}, CONTEXT),
        ({"conversation_state_context": {**both, "captured_minute": 1.0}}, CONTEXT),
        ({"conversation_state_context": {**both, "captured_minute": -1}}, CONTEXT),
        ({"conversation_state_context": {**both, "participants": []}}, CONTEXT),
        ({"conversation_state_context": {**both, "extra": 1}}, CONTEXT),
        ({"conversation_state_context": alone}, CONTEXT),
        ({"conversation_state_context": changed(**{"0.stress": 2.0})}, CONTEXT),
        ({"conversation_state_context": changed(**{"0.stress": True})}, CONTEXT),
        ({"conversation_state_context": changed(**{"0.stress": "0.5"})}, CONTEXT),
        ({"conversation_state_context": changed(**{"1.trust": None})}, CONTEXT),
        ({"conversation_state_context": changed(**{"1.needs": {}})}, CONTEXT),
        ({"conversation_state_context": changed(**{"1.needs.hunger": 1.5})}, CONTEXT),
        ({"conversation_state_context": changed(**{"1.needs.hunger": 2})}, CONTEXT),
        ({"conversation_state_context": changed(**{"0.affect": {"x": 0.1}})}, CONTEXT),
        ({"conversation_state_context": changed(**{"0.affect": 0.5})}, CONTEXT),
        # A context Python accepts on its slow path goes on to the later checks.
        ({"conversation_state_context": changed(**{"0.stress": 1}), "reason_short": "x"}, REASON),
        (
            {
                "conversation_state_context": changed(**{"1.needs.hunger": 0}),
                "interaction_duration_minutes": 0,
            },
            DURATION,
        ),
        ({"conversation_state_context": changed(**{"0.affect": None}), "action": "x"}, TOPIC),
        # Its participants: the actor, and the other person only if that is a str.
        ({"conversation_state_context": alone, "participant_agent_id": 5}, APART),
        ({"conversation_state_context": alone, "participant_agent_id": actor}, APART),
        ({"conversation_state_context": both, "participant_agent_id": 5}, CONTEXT),
        # Then the payload's fields.
        ({"extra": 1, "action": "other"}, FIELDS),
        ({"topic_id": _DROP}, FIELDS),
        ({"interaction_duration_minutes": _DROP, "reason_short": "x"}, FIELDS),
        ({"participant_agent_id": _DROP}, FIELDS),
        ({"conversation_state_context": both, "zone_id": _DROP}, FIELDS),
        # Then the two people.
        ({"participant_agent_id": "nobody", "action": "other"}, APART),
        ({"participant_agent_id": actor, "reason_short": "x"}, APART),
        ({"participant_agent_id": 5}, APART),
        ({"zone_id": _other_zone(world, zone), "action": "other"}, APART),
        ({"zone_id": None}, APART),
        # Then the action and topic, the reason code and the duration.
        ({"action": "other", "reason_short": "x"}, TOPIC),
        ({"topic_id": "mutual_aid", "interaction_duration_minutes": 0}, TOPIC),
        ({"action": 5}, TOPIC),
        ({"action": None}, TOPIC),
        ({"reason_short": "x", "interaction_duration_minutes": 0}, REASON),
        ({"reason_short": 5}, REASON),
        ({"interaction_duration_minutes": 0}, DURATION),
        ({"interaction_duration_minutes": 1441}, DURATION),
        ({"interaction_duration_minutes": True}, DURATION),
        ({"interaction_duration_minutes": 5.0}, DURATION),
        ({"interaction_duration_minutes": "5"}, DURATION),
        ({"interaction_duration_minutes": -(2**70)}, DURATION),
    ]
    for change, text in cases:
        _says(world, _action(world, "reassurance_exchange", actor, participant, **change), text)
    _applies_as_python(world, _action(world, "reassurance_exchange", actor, participant))
    _applies_as_python(
        world,
        _action(world, "reassurance_exchange", actor, participant, conversation_state_context=both),
    )


def test_a_social_action_whose_participant_moved_is_suppressed_first(
    small_world: WorldState,
) -> None:
    world = _copy(small_world)
    actor, participant = _pair(world)
    world.agents[participant].zone_id = _other_zone(world, world.agents[actor].zone_id)
    for kind in ACTIONS:
        _says(world, _action(world, kind, actor, participant, reason_short="x"), APART)


def test_family_care_and_voluntary_support_texts_come_after_the_duration(
    small_world: WorldState,
) -> None:
    world = _copy(small_world)
    actor, participant = _pair(world)
    _household(world, participant, "h2")
    _says(world, _action(world, "family_care", actor, participant), HOUSEHOLD)
    event = _action(world, "family_care", actor, participant, interaction_duration_minutes=0)
    _says(world, event, DURATION)
    world.agents[actor].stress = 0.9
    world.agents[participant].stress = 0.1
    for name in ("toilet_urgency", "hunger", "fatigue", "hydration_need"):
        setattr(world.agents[participant].needs, name, 0.1)
    _says(world, _action(world, "voluntary_support", actor, participant), NEED)
    event = _action(world, "voluntary_support", actor, participant, action="x")
    _says(world, event, TOPIC)


# ---- Complaint responses ------------------------------------------------------------------------


def test_complaint_response_texts_are_python_s(small_world: WorldState) -> None:
    world = _copy(small_world)
    owner, other = sorted(world.agents)[:2]
    request_id = _open_request(world, owner, age=4)
    world.agents[owner].stress = 0.5
    zone = world.agents[owner].zone_id
    base = {
        "interaction_type": "complaint_response",
        "agent_id": owner,
        "request_id": request_id,
        "zone_id": zone,
        "stress_threshold": 0.1,
        "complaint_wait_minutes": 3,
        "action": "acknowledge_complaint",
        "reason_short": "long_wait",
    }
    fields = "complaint response payload fields do not match"
    thresholds = "complaint response thresholds are invalid"
    stale = "complaint response context is no longer current"
    calm = "complaint response stress candidate is no longer current"
    reason = "complaint response reason code is not allowed"
    action = "complaint response action is not allowed"
    cases: list[tuple[dict[str, Any], dict[str, Any], str]] = [
        ({"agent_id": "nobody", "extra": 1}, {}, UNKNOWN_AGENT),
        ({"extra": 1, "stress_threshold": 2}, {}, fields),
        ({"complaint_wait_minutes": _DROP}, {}, fields),
        ({"participant_agent_id": other}, {}, fields),
        ({"stress_threshold": 2, "request_id": "nothing"}, {}, thresholds),
        ({"stress_threshold": -0.1}, {}, thresholds),
        ({"stress_threshold": True}, {}, thresholds),
        ({"stress_threshold": "0.5"}, {}, thresholds),
        ({"stress_threshold": None}, {}, thresholds),
        ({"complaint_wait_minutes": 0}, {}, thresholds),
        ({"complaint_wait_minutes": 2.5}, {}, thresholds),
        ({"complaint_wait_minutes": True}, {}, thresholds),
        ({"request_id": "nothing", "reason_short": "x"}, {}, stale),
        ({"request_id": 5}, {}, stale),
        ({"agent_id": other}, {}, stale),
        ({"zone_id": _other_zone(world, zone)}, {}, stale),
        ({}, {"causation_id": "other"}, stale),
        ({"complaint_wait_minutes": 10}, {}, stale),
        ({"stress_threshold": 0.9, "reason_short": "x"}, {}, calm),
        ({"stress_threshold": 1}, {}, calm),
        ({"reason_short": "x", "action": "dance"}, {}, reason),
        ({"reason_short": 5}, {}, reason),
        ({"action": "dance"}, {}, action),
        ({"action": None}, {}, action),
    ]
    for change, event_fields, text in cases:
        event_fields = {"causation_id": request_id, **event_fields}
        _says(world, _social(world, {**base, **change}, **event_fields), text)
    world.support_requests[request_id].status = SupportRequestStatus.RESOLVED
    _says(world, _social(world, base, causation_id=request_id), stale)


# ---- Cooperation --------------------------------------------------------------------------------


def test_cooperation_texts_are_python_s(small_world: WorldState) -> None:
    world = _copy(small_world)
    actor, participant = _pair(world)
    world.agents[actor].stress = 0.5
    world.agents[participant].stress = 0.6
    zone = world.agents[actor].zone_id
    trigger = _contact(world, actor, participant)
    reverse = _contact(world, participant, actor, event_id="contact-reverse")
    early = _contact(world, actor, participant, event_id="contact-early", minute=0)
    failed = _contact(
        world, actor, participant, event_id="contact-failed", status=EventStatus.FAILED
    )
    base = {
        "interaction_type": "cooperation",
        "agent_id": actor,
        "participant_agent_id": participant,
        "zone_id": zone,
        "stress_threshold": 0.1,
        "relationship": "same_group",
        "trigger_event_id": trigger,
        "action": "offer_mutual_support",
        "reason_short": "shared_group_contact",
    }
    fields = "cooperation payload fields do not match"
    threshold = "cooperation stress threshold is invalid"
    two = "cooperation requires two different known Agents"
    changed = "cooperation participants or relationship changed"
    contact = "cooperation does not match an applied local contact"
    calm = "cooperation stress candidate is no longer current"
    reason = "cooperation reason code is not allowed"
    action = "cooperation action is not allowed"
    cases: list[tuple[dict[str, Any], dict[str, Any], str]] = [
        ({"extra": 1, "stress_threshold": 2}, {}, fields),
        ({"relationship": _DROP}, {}, fields),
        ({"stress_threshold": 1.5, "participant_agent_id": "nobody"}, {}, threshold),
        ({"stress_threshold": None}, {}, threshold),
        ({"stress_threshold": False}, {}, threshold),
        ({"participant_agent_id": "nobody", "zone_id": "x"}, {}, two),
        ({"participant_agent_id": actor}, {}, two),
        ({"participant_agent_id": 5}, {}, two),
        ({"zone_id": "x", "trigger_event_id": "other"}, {}, changed),
        ({"relationship": "friends"}, {}, changed),
        ({"relationship": None}, {}, changed),
        ({}, {"causation_id": "other"}, changed),
        ({"trigger_event_id": 5}, {}, changed),
        ({"trigger_event_id": "other", "reason_short": "x"}, {"causation_id": "other"}, contact),
        ({"trigger_event_id": reverse}, {"causation_id": reverse}, contact),
        ({"trigger_event_id": early}, {"causation_id": early}, contact),
        ({"trigger_event_id": failed}, {"causation_id": failed}, contact),
        ({"stress_threshold": 0.95, "reason_short": "x"}, {}, calm),
        ({"reason_short": "x", "action": "dance"}, {}, reason),
        ({"reason_short": None}, {}, NO_REASON),
        ({"action": "dance"}, {}, action),
        ({"action": 5}, {}, action),
    ]
    for change, event_fields, text in cases:
        payload = {**base, **change}
        event_fields = {"causation_id": payload.get("trigger_event_id"), **event_fields}
        if not isinstance(event_fields["causation_id"], str):
            event_fields["causation_id"] = trigger
        _says(world, _social(world, payload, **event_fields), text)
    world.agents[participant].zone_id = _other_zone(world, zone)
    _says(world, _social(world, base, causation_id=trigger), changed)
    world.agents[participant].zone_id = zone
    _household(world, participant, "h2")
    _says(world, _social(world, base, causation_id=trigger), changed)
    _household(world, participant, "h1")
    _applies_as_python(world, _social(world, base, causation_id=trigger))


# ---- Reactions to a heard claim -----------------------------------------------------------------


def _hearing(world: WorldState, message_id: str) -> tuple[str, str]:
    """A person who heard the claim and has not settled it, and another in the same zone."""
    state = world.model_state["information_spread_v1"]
    listener = next(
        agent
        for agent in sorted(state["beliefs"])
        if state["beliefs"][agent].get(message_id, {}).get("belief") == "heard"
    )
    speaker = next(agent for agent in sorted(world.agents) if agent != listener)
    world.agents[speaker].zone_id = world.agents[listener].zone_id
    return listener, speaker


def test_information_reaction_texts_are_python_s(rumor_world: WorldState) -> None:
    world = _copy(rumor_world)
    state = world.model_state["information_spread_v1"]
    assert state["messages"]["claim-false"]["truth_status"] == "false"
    listener, speaker = _hearing(world, "claim-false")
    zone = world.agents[listener].zone_id
    trigger = _contact(world, speaker, listener)
    reverse = _contact(world, listener, speaker, event_id="contact-reverse")
    early = _contact(world, speaker, listener, event_id="contact-early", minute=0)
    other_type = _contact(
        world, speaker, listener, event_id="contact-other", event_type="proximity_contact"
    )
    settled = next(
        (
            message
            for message, belief in sorted(state["beliefs"][listener].items())
            if message != "claim-false"
        ),
        None,
    )
    base = {
        "interaction_type": "information_reaction",
        "agent_id": listener,
        "participant_agent_id": speaker,
        "zone_id": zone,
        "message_id": "claim-false",
        "trigger_event_id": trigger,
        "action": "seek_claim_verification",
        "reason_short": "heard_through_contact",
    }
    fields = "information reaction payload fields do not match"
    two = "information reaction requires two known Agents"
    local = "information reaction contact is no longer local"
    contact = "information reaction does not match an applied contact"
    current = "information claim is no longer current"
    settled_text = "information claim has already been verified or rejected"
    action = "information reaction action is not allowed"
    reason = "information reaction reason code is not allowed"
    cases: list[tuple[dict[str, Any], dict[str, Any], str]] = [
        ({"extra": 1, "participant_agent_id": "nobody"}, {}, fields),
        ({"message_id": _DROP}, {}, fields),
        ({"participant_agent_id": "nobody", "zone_id": "x"}, {}, two),
        ({"participant_agent_id": listener}, {}, two),
        ({"participant_agent_id": None}, {}, two),
        ({"zone_id": "x", "trigger_event_id": "other"}, {}, local),
        ({}, {"causation_id": "other"}, local),
        ({"trigger_event_id": "other", "action": "x"}, {"causation_id": "other"}, contact),
        ({"trigger_event_id": reverse}, {"causation_id": reverse}, contact),
        ({"trigger_event_id": early}, {"causation_id": early}, contact),
        ({"trigger_event_id": other_type}, {"causation_id": other_type}, contact),
        ({"message_id": "nothing", "action": "x"}, {}, current),
        ({"message_id": 5}, {}, current),
        ({"action": "x", "reason_short": "x"}, {}, action),
        ({"action": None}, {}, action),
        ({"reason_short": "x"}, {}, reason),
        ({"reason_short": 5}, {}, reason),
    ]
    if settled is not None:
        cases.append(({"message_id": settled, "action": "x"}, {}, settled_text))
    for change, event_fields, text in cases:
        event_fields = {"causation_id": trigger, **event_fields}
        _says(world, _social(world, {**base, **change}, **event_fields), text)
    belief = state["beliefs"][listener]["claim-false"]
    belief["belief"] = "verified"
    _says(world, _social(world, base, causation_id=trigger), settled_text)
    belief["belief"] = "heard"
    world.agents[speaker].zone_id = _other_zone(world, zone)
    _says(world, _social(world, base, causation_id=trigger), local)
    world.agents[speaker].zone_id = zone
    _applies_as_python(world, _social(world, base, causation_id=trigger))


def test_a_reaction_without_the_information_state_is_suppressed(small_world: WorldState) -> None:
    world = _copy(small_world)
    world.model_state.pop("information_spread_v1", None)
    listener, speaker = sorted(world.agents)[:2]
    world.agents[speaker].zone_id = world.agents[listener].zone_id
    trigger = _contact(world, speaker, listener)
    payload = {
        "interaction_type": "information_reaction",
        "agent_id": listener,
        "participant_agent_id": speaker,
        "zone_id": world.agents[listener].zone_id,
        "message_id": "claim-false",
        "trigger_event_id": trigger,
        "action": "x",
        "reason_short": "x",
    }
    event = _social(world, payload, causation_id=trigger)
    _says(world, event, "information state is no longer enabled")


# ---- Another type -------------------------------------------------------------------------------


def test_another_interaction_type_is_failed_after_the_common_checks(
    small_world: WorldState,
) -> None:
    world = _copy(small_world)
    actor = sorted(world.agents)[0]
    not_allowed = "typed social interaction type is not allowed"
    for interaction, change, text in (
        ("dance", {}, not_allowed),
        (5, {}, not_allowed),
        (2.5, {}, not_allowed),
        (True, {}, not_allowed),
        ("dance", {"agent_id": "nobody"}, UNKNOWN_AGENT),
        ("dance", {"reason_short": None}, NO_REASON),
        ("dance", {"conversation_state_context": {}}, CONTEXT),
        ("Cooperation", {}, not_allowed),
    ):
        payload = {
            "interaction_type": interaction,
            "agent_id": actor,
            "reason_short": "x",
            **change,
        }
        _says(world, _social(world, payload), text)
