"""Relationship keys are checked as before when the check of each key is remembered (#228)."""

from __future__ import annotations

import json

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.social_network import (
    SOCIAL_NETWORK_STATE_KEY,
    create_social_network_state,
    validate_social_network_state,
)

RECORD = {
    "contact_count": 1,
    "conversation_count": 0,
    "cooperation_count": 0,
    "trust": 0.5,
    "shared_message_ids": [],
    "last_contact_minute": 3,
    "last_interaction": None,
}


def _outcome_before(pair_key: object) -> str:
    """The key check before #228, which parsed and wrote back every key."""
    try:
        pair = json.loads(pair_key)  # type: ignore[arg-type]
    except (TypeError, json.JSONDecodeError):
        return "social_network_v1 relationship key is invalid"
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or any(not isinstance(item, str) for item in pair)
        or pair[0] == pair[1]
        or json.dumps(pair, separators=(",", ":")) != pair_key
    ):
        return "social_network_v1 relationship record is invalid"
    return "valid"


def _outcome(world: WorldState, pair_key: object) -> str:
    world.model_state[SOCIAL_NETWORK_STATE_KEY]["relationships"] = {pair_key: dict(RECORD)}
    try:
        validate_social_network_state(world)
    except ValueError as error:
        return str(error)
    return "valid"


@pytest.mark.parametrize(
    "pair_key",
    [
        '["A001","A002"]',
        '["A002","A001"]',
        '["A001","A001"]',
        '["A001"]',
        '["A001","A002","A003"]',
        '[ "A001","A002"]',
        '["A001",2]',
        '{"A001":"A002"}',
        '["A001","\\u00e9"]',
        '["A001","é"]',
        "[",
        "",
        "A001",
        7,
        None,
    ],
)
def test_each_relationship_key_is_judged_as_before_every_time(generated_scenario, pair_key) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[SOCIAL_NETWORK_STATE_KEY] = create_social_network_state()
    expected = _outcome_before(pair_key)
    # The second check of the same key uses the remembered result.
    assert [_outcome(world, pair_key) for _ in range(2)] == [expected, expected]


def _record_accepted_before(record: object) -> bool:
    """The record check before it was written inline."""
    return (
        isinstance(record, dict)
        and set(record) == set(RECORD)
        and not any(
            not isinstance(record[key], int) or isinstance(record[key], bool) or record[key] < 0
            for key in ("contact_count", "conversation_count", "cooperation_count")
        )
        and isinstance(record["shared_message_ids"], list)
    )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"contact_count": -1},
        {"conversation_count": True},
        {"cooperation_count": 1.0},
        {"contact_count": 10**20},
        {"shared_message_ids": ()},
        {"extra": 1},
        {"contact_count": None},
    ],
)
def test_each_relationship_record_is_judged_as_before(generated_scenario, change) -> None:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[SOCIAL_NETWORK_STATE_KEY] = create_social_network_state()
    record = {**RECORD, **change}
    missing = dict(RECORD)
    del missing["trust"]
    for candidate in (record, missing, ["not", "a", "dict"]):
        world.model_state[SOCIAL_NETWORK_STATE_KEY]["relationships"] = {
            '["A001","A002"]': candidate
        }
        try:
            validate_social_network_state(world)
            accepted = True
        except ValueError as error:
            accepted = "relationship record is invalid" not in str(error)
        assert accepted is _record_accepted_before(candidate), candidate
