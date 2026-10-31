"""Compare the kernels of social_network_v1 and of the social action with their Python rules.

- observe_event: the Python rule on a plain copy of the counters and of the records it touches,
  then the kernel on the world; JSON text with sorted keys, so a float keeps its bits. Copying only
  those records keeps a seven-day run possible. The row and the tree are compared every 500 calls.
- The namespace's validator: whenever the kernel accepts, the Python rules must accept too.
- capture_context and validate_context: what the rule returns, or that it refused.
- The handler's apply: the Python rule on stand-ins of the two people; then the three values it
  writes, by their bits, on the objects; the rows are compared with the objects every 500 calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, pure_shadow, values_in

_FIELDS = ("first_agent_id", "second_agent_id")
SOCIAL_ACTIONS = {"reassurance_exchange", "family_care", "voluntary_support"}


def _touched_keys(event: Any) -> list[str]:
    from sheltersim.domain.models import ordered_pair_key

    payload = event.payload
    if event.event_type == "information_contact_update":
        pair = (payload.get("recipient_agent_id"), payload.get("sender_agent_id"))
        return [ordered_pair_key(*pair)] if all(isinstance(id_, str) for id_ in pair) else []
    if event.event_type == "proximity_contact_update":
        first, second = (payload.get(name) for name in _FIELDS)
    elif event.event_type == "social_interaction":
        first, second = payload.get("agent_id"), payload.get("participant_agent_id")
    else:
        return []
    if not isinstance(first, str) or not isinstance(second, str):
        return []
    return [ordered_pair_key(first, second), ordered_pair_key(second, first)]


def _person_copy(agent: Any) -> SimpleNamespace:
    needs = agent.needs
    return SimpleNamespace(
        id=agent.id,
        zone_id=agent.zone_id,
        profile=agent.profile,
        stress=agent.stress,
        trust=agent.trust,
        needs=SimpleNamespace(
            toilet_urgency=needs.toilet_urgency,
            hunger=needs.hunger,
            fatigue=needs.fatigue,
            hydration_need=needs.hydration_need,
        ),
    )


def _written(person: Any) -> list[str]:
    return [person.stress.hex(), person.trust.hex(), person.needs.fatigue.hex()]


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    recorder = Recorder(output, "social_network")
    unsupported = native.Unsupported
    rules = native.rules("social_network")
    dialogue = native.rules("social_dialogue")
    interaction = native.rules("social_interaction")
    state = native.state("social_network")
    observe_kernel = rules.observe_event
    validate_kernel = state.validate
    apply_kernel = interaction.apply
    calls = [0]

    def observe(world: Any, event: Any, minute: Any) -> Any:
        from sheltersim.domain import social_network as module

        key = module.SOCIAL_NETWORK_STATE_KEY
        recorder.seen[f"observe.calls.{event.event_type}"] += 1
        network = world.model_state.get(key)
        keys = _touched_keys(event)
        relationships = network["relationships"] if isinstance(network, dict) else {}
        plain = json.loads(
            json.dumps(
                {
                    **network,
                    "relationships": {k: relationships[k] for k in keys if k in relationships},
                }
            )
        )
        table = dict(world.model_state)
        table[key] = plain
        expected_error = None
        try:
            module.observe_event.__wrapped__(SimpleNamespace(model_state=table), event, minute)
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            expected_error = raised
        try:
            result = observe_kernel(world, event, minute)
        except unsupported as reason:
            recorder.reasons[f"observe: {reason}"] += 1
            raise
        recorder.seen["observe.kernel_ran"] += 1
        if expected_error is not None:
            recorder.differ(f"observe: the kernel took what Python turned down: {expected_error}")
            return result
        now = world.model_state[key]
        current = {
            **now,
            "relationships": {
                k: now["relationships"][k] for k in keys if k in now["relationships"]
            },
        }
        if json.dumps(plain, sort_keys=True) != json.dumps(current, sort_keys=True):
            recorder.differ(f"observe minute {minute}: {keys}: {plain!r:.300} != {current!r:.300}")
        else:
            recorder.seen["observe.values_compared"] += values_in(plain)
        calls[0] += 1
        if calls[0] % 500 == 0:
            recorder.seen["observe.row_checks"] += 1
            if not world._core.mirror(key).verify():
                recorder.differ(f"observe minute {minute}: the row and the tree")
        return result

    def validate(world: Any) -> bool:
        from sheltersim.domain import social_network as module

        recorder.seen["validate.calls"] += 1
        try:
            answer = validate_kernel(world)
        except unsupported as reason:
            recorder.reasons[f"validate: {reason}"] += 1
            raise
        recorder.seen[f"validate.{bool(answer)}"] += 1
        if answer:
            try:
                module.validate_social_network_state(world)
            except ValueError as raised:
                recorder.differ(f"validate: the kernel took what Python turned down: {raised}")
        return answer

    def apply(handler: Any, world: Any, event: Any) -> Any:
        from sheltersim.events.handlers import SocialInteractionHandler

        payload = event.payload
        kind = payload.get("interaction_type") or "plain"
        recorder.seen[f"apply.calls.{kind}"] += 1
        if kind not in SOCIAL_ACTIONS:
            # The Python rule of these writes the namespaces it reads: it runs once, after this.
            try:
                return apply_kernel(handler, world, event)
            except unsupported as reason:
                recorder.reasons[f"apply: {kind}: {reason}"] += 1
                raise
        ids = [payload.get("agent_id"), payload.get("participant_agent_id")]
        copies = {
            id_: _person_copy(world.agents[id_])
            for id_ in ids
            if isinstance(id_, str) and id_ in world.agents
        }
        stand_in = SimpleNamespace(
            agents=copies,
            model_state=world.model_state,
            minute_index=world.minute_index,
        )
        python_error = None
        try:
            SocialInteractionHandler.apply.__wrapped__(handler, stand_in, event)
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            python_error = raised
        try:
            result = apply_kernel(handler, world, event)
        except unsupported as reason:
            detail = f"{reason}" if python_error is None else f"Python raises: {reason}"
            recorder.reasons[f"apply: {kind}: {detail}"] += 1
            raise
        recorder.seen[f"apply.kernel_ran.{kind}"] += 1
        if python_error is not None:
            recorder.differ(f"apply: the kernel took what Python turned down: {python_error}")
            return result
        for id_, copy in copies.items():
            person = world.agents[id_]
            if _written(copy) != _written(person):
                recorder.differ(
                    f"apply minute {world.minute_index} {id_}: {_written(copy)} != "
                    f"{_written(person)}"
                )
            else:
                recorder.seen["apply.values_compared"] += 3
        calls[0] += 1
        if calls[0] % 500 == 0:
            recorder.seen["apply.row_checks"] += 1
            problem = world._core.entities().verify(world)
            if problem is not None:
                recorder.differ(f"apply minute {world.minute_index}: {problem}")
        return result

    rules.observe_event = observe
    state.validate = validate
    interaction.apply = apply

    module_of: list[Any] = []

    def dialogue_rule(name: str) -> Any:
        def rule() -> Any:
            if not module_of:
                from sheltersim.domain import social_dialogue

                module_of.append(social_dialogue)
            return getattr(module_of[0], name).__wrapped__

        return rule

    dialogue.capture_context = pure_shadow(
        recorder,
        "capture_context",
        dialogue.capture_context,
        dialogue_rule("capture_social_state_context"),
        unsupported,
    )
    dialogue.validate_context = pure_shadow(
        recorder,
        "validate_context",
        dialogue.validate_context,
        dialogue_rule("validate_social_state_context"),
        unsupported,
    )
