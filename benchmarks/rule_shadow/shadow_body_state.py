"""Compare record_social_event's kernel with its Python rule, and rows_valid with the Python rules.

The namespace is too large to copy at each event (history 8 MB, thousands of relationships), so the
Python rule runs on a plain copy of what an event can touch: every body record and the relationships
of the people it names, with an empty history. What it leaves there must be the same JSON text as
what the kernel left in the tree, and the row must hold the same values. VERIFY_EVERY-th events also
compare the whole row with the whole tree.

At each rows_valid that says yes the Python rules run too: a yes they turn down is a difference. A
no they pass is counted with its reason: it only costs time. Each PROBE_EVERY-th yes also breaks a
restored copy with a value 1% beyond a bound, in a record and in a relationship that was accepted
before, and with a key naming nobody. The kernel must say no to each.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from shadow_kit import Recorder, text, values_in

KEY = "body_state_v1"
VERIFY_EVERY = 400
PROBE_EVERY = 997
MOVEMENT = "movement_v1"
INFORMATION = "information_spread_v1"
LIMIT_MARGIN = 8
PEOPLE_FIELDS = (
    "first_agent_id",
    "second_agent_id",
    "sender_agent_id",
    "recipient_agent_id",
    "agent_id",
    "participant_agent_id",
)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _sorted_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def prepare(output: str) -> None:
    from sheltersim import native

    if not native.available():
        return
    from sheltersim.domain.models import ordered_pair_key

    rules = native.rules("body_state")
    state_module = native.state("body_state")
    recorder = Recorder(output, "body_state")
    unsupported = native.Unsupported
    kernel = rules.record_social_event
    rows_kernel = state_module.rows_valid
    module: list[Any] = []

    def body_state() -> Any:
        if not module:
            from sheltersim.domain import body_state as loaded

            module.append(loaded)
        return module[0]

    def on_copy(world: Any, event: Any) -> tuple[Any, list[str], list[str]]:
        state = world.model_state[KEY]
        payload = event.payload
        people = [payload[name] for name in PEOPLE_FIELDS if isinstance(payload.get(name), str)]
        keys = [
            ordered_pair_key(first, second)
            for first in people
            for second in people
            if first != second
        ]
        relationships = state["relationships"]
        table: dict[str, Any] = {
            KEY: {
                "config": _plain(state["config"]),
                "agents": _plain(state["agents"]),
                "relationships": {
                    key: _plain(relationships[key]) for key in keys if key in relationships
                },
                "history": [],
            }
        }
        for name in (MOVEMENT, INFORMATION):
            if name in world.model_state:
                table[name] = world.model_state[name]
        standin = SimpleNamespace(
            model_state=table, agents=world.agents, model_configs=world.model_configs
        )
        return standin, sorted(set(people)), sorted(set(keys))

    def compare(world: Any, event: Any, standin: Any, keys: list[str], before: int) -> None:
        label = f"{event.event_type} {event.event_id}"
        state = world.model_state[KEY]
        plain = standin.model_state[KEY]
        recorder.seen[f"event.{event.event_type}"] += 1
        if text(plain["agents"]) != text(state["agents"]):
            recorder.differ(f"{label}: the body records")
        recorder.seen["values_compared"] += values_in(plain["agents"])
        wanted = {key: state["relationships"][key] for key in keys if key in state["relationships"]}
        if text(plain["relationships"]) != text(wanted):
            recorder.differ(f"{label}: the relationships")
        recorder.seen["values_compared"] += values_in(plain["relationships"])
        added = plain["history"]
        history = state["history"]
        if len(history) != min(before + len(added), body_state().MAX_SOCIAL_HISTORY):
            recorder.differ(f"{label}: {len(history)} history items after {before}")
        tail = list(history[-len(added) :]) if added else []
        if text(added) != text(tail):
            recorder.differ(f"{label}: the history {text(added):.300} {text(tail):.300}")
        recorder.seen["values_compared"] += values_in(added)
        recorder.seen["history_compared"] += len(added)
        people = list(state["agents"])
        row_people, row_relationships, row_tail, row_count = state_module.row_parts(
            world, people, keys, len(added)
        )
        if _sorted_text(row_people) != _sorted_text(state["agents"]):
            recorder.differ(f"{label}: the row's body records")
        if _sorted_text(row_relationships) != _sorted_text(wanted):
            recorder.differ(f"{label}: the row's relationships")
        if row_count != len(history):
            recorder.differ(f"{label}: the row has {row_count} history items")
        for raw, item in zip(row_tail, tail, strict=True):
            if raw.encode() != native.canonical_json(item):
                recorder.differ(f"{label}: the row's history item")
        recorder.seen["row_values_compared"] += values_in(row_people) + values_in(row_relationships)
        if recorder.seen["kernel_ran"] % VERIFY_EVERY == 1:
            recorder.seen["whole_row_compared"] += 1
            if not world._core.mirror(KEY).verify():
                recorder.differ(f"{label}: the row differs from the tree")

    def shadow(world: Any, event: Any, minute: Any) -> None:
        recorder.seen["calls"] += 1
        state = world.model_state.get(KEY)
        if state is None:
            kernel(world, event, minute)
            recorder.seen["no_namespace"] += 1
            return
        relationships = state["relationships"]
        near_limit = len(relationships) >= body_state().MAX_SOCIAL_RELATIONSHIPS - LIMIT_MARGIN
        standin, _people, keys = on_copy(world, event)
        error = None
        try:
            body_state().record_social_event.__wrapped__(standin, event, minute)
        except Exception as raised:  # noqa: BLE001 - only compared with what the kernel does
            error = raised
        before = len(state["history"])
        try:
            kernel(world, event, minute)
        except unsupported as reason:
            recorder.reasons[f"{event.event_type}: {reason}"] += 1
            raise
        recorder.seen["kernel_ran"] += 1
        if error is not None:
            recorder.differ(f"{event.event_id}: the kernel took what Python turned down: {error}")
        elif near_limit:
            # The copy holds a few relationships, so its count is not near the limit.
            recorder.seen["near_the_limit"] += 1
        else:
            compare(world, event, standin, keys, before)

    probing = [False]

    def probe(world: Any) -> None:
        try:
            document = world.to_dict()
        except (ValueError, TypeError, KeyError):
            recorder.seen["rows.probe_no_document"] += 1
            return
        state_of = lambda copied: copied.model_state[KEY]  # noqa: E731
        from sheltersim.core.world import WorldState

        def restored() -> Any:
            copied = WorldState.from_dict(copy.deepcopy(document))
            return copied if rows_kernel(copied) is True else None

        def attempt(label: str, write: Any) -> None:
            copied = restored()
            if copied is None:
                recorder.seen["rows.probe_copy_refused"] += 1
                return
            if not write(state_of(copied)):
                recorder.seen["rows.probe_nothing_to_break"] += 1
                return
            recorder.seen["rows.probes"] += 1
            try:
                accepted = rows_kernel(copied) is True
            except unsupported:
                accepted = False
            if accepted:
                recorder.differ(f"rows_valid took {label}")

        def record_out_of_range(state: Any) -> bool:
            next(iter(state["agents"].values()))["reassurance"] = 1.01
            return True

        def trust_out_of_range(state: Any) -> bool:
            record = next(iter(state["relationships"].values()), None)
            if record is None:
                return False
            record["trust"] = 1.01
            return True

        def unknown_key(state: Any) -> bool:
            state["relationships"]['["nobody","here"]'] = {
                "contact_count": 0,
                "trust": 0.5,
                "contact_minutes": 0,
                "last_contact_minute": None,
                "last_interaction": None,
                "topic_ids": [],
            }
            return True

        attempt("a record 1% beyond its bound", record_out_of_range)
        attempt("a relationship 1% beyond its bound", trust_out_of_range)
        attempt("a key naming nobody", unknown_key)

    def rows_shadow(world: Any) -> bool:
        accepted = rows_kernel(world)
        if probing[0]:
            return accepted
        recorder.seen["rows.calls"] += 1
        loaded = body_state()
        kept = loaded._ROWS_VALID
        loaded._ROWS_VALID = None
        try:
            loaded.validate_body_state(world, validate_event_history=False)
            error = None
        except (ValueError, KeyError, TypeError) as raised:
            error = raised
        finally:
            loaded._ROWS_VALID = kept
        if accepted is not True:
            recorder.seen["rows.refused"] += 1
            if error is None:
                recorder.reasons["rows_valid: said no to what Python passes"] += 1
            return accepted
        recorder.seen["rows.accepted"] += 1
        if error is not None:
            recorder.differ(f"rows_valid took what Python turned down: {error}")
        state = world.model_state[KEY]
        recorder.seen["rows.relationships_read_by_python"] += len(state["relationships"])
        if recorder.seen["rows.accepted"] % PROBE_EVERY == 1:
            probing[0] = True
            try:
                probe(world)
            finally:
                probing[0] = False
        return accepted

    rules.record_social_event = shadow
    state_module.rows_valid = rows_shadow
