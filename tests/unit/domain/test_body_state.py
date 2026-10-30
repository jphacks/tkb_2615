"""The saved body-state config is accepted exactly when it is the canonical config
without some of the optional fields older versions did not write (#228).
"""

from __future__ import annotations

import random

import pytest

from sheltersim.core.world import WorldState
from sheltersim.domain.body_state import (
    BODY_STATE_KEY,
    _registry_affect_model,
    canonical_body_config,
    create_body_state,
    validate_body_state,
)

OPTIONAL_FIELDS = {
    "social_interaction_minutes",
    "relationship_distance_scale_m",
    "relationship_repeat_contact_bonus",
    "hygiene_support_relief",
    "caregiver_assistance_stress_relief",
    "caregiver_assistance_reassurance_gain",
    "privacy_accommodation_discontent_relief",
    "mobility_assistance_stress_relief",
    "language_access_uncertainty_relief",
    "medical_assessment_uncertainty_relief",
    "medication_support_uncertainty_relief",
    "dependent_assistance_stress_relief",
    "dependent_assistance_reassurance_gain",
}


def _accepted_before(world: WorldState, saved: dict) -> bool:
    """The check before #228: saved is one of every reduced canonical config."""
    config = canonical_body_config(saved)
    optional = set(OPTIONAL_FIELDS)
    if _registry_affect_model(world) is None:
        optional.add("affect_policy")
    reduced = [config]
    for field in sorted(optional):
        reduced.extend(
            {key: value for key, value in candidate.items() if key != field}
            for candidate in tuple(reduced)
            if field in candidate
        )
    return saved in reduced


def _accepted(world: WorldState, saved: dict) -> bool:
    world.model_state[BODY_STATE_KEY]["config"] = saved
    try:
        validate_body_state(world)
    except ValueError as error:
        if str(error) != "body_state_v1 config is not canonical":
            raise
        return False
    return True


@pytest.fixture
def body_world(generated_scenario) -> WorldState:
    world = WorldState.from_scenario(generated_scenario)
    world.model_state[BODY_STATE_KEY] = create_body_state(world)
    return world


def test_a_saved_config_is_accepted_as_the_reduced_canonical_configs_were(body_world) -> None:
    world = body_world
    canonical = dict(world.model_state[BODY_STATE_KEY]["config"])
    optional = sorted(OPTIONAL_FIELDS | {"affect_policy"} & set(canonical))
    required = sorted(set(canonical) - set(optional))
    generator = random.Random(228)
    candidates = [canonical]
    for _ in range(300):
        saved = {
            key: value
            for key, value in canonical.items()
            if key not in optional or generator.random() < 0.5
        }
        kind = generator.randrange(5)
        if kind == 1 and required:
            # A required field is missing.
            del saved[generator.choice(required)]
        elif kind == 2:
            saved["unknown_field"] = 1
        elif kind == 3:
            # A kept optional field differs from the canonical value.
            kept = [key for key in optional if key in saved and isinstance(saved[key], float)]
            if kept:
                saved[generator.choice(kept)] += 0.125
        candidates.append(saved)
    accepted = 0
    for saved in candidates:
        try:
            expected = _accepted_before(world, saved)
        except (ValueError, TypeError, KeyError):
            # canonical_body_config rejects it before the comparison in both versions.
            with pytest.raises((ValueError, TypeError, KeyError)):
                _accepted(world, saved)
            continue
        assert _accepted(world, saved) is expected
        accepted += expected
    # Both outcomes occur.
    assert 0 < accepted < len(candidates)


def _relationship_outcome_before(world: WorldState, key: object) -> str:
    """The key check before #228, which parsed and wrote back every key."""
    import json

    try:
        pair = json.loads(key)  # type: ignore[arg-type]
    except (TypeError, json.JSONDecodeError):
        return "body_state_v1 relationship key is invalid"
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or any(item not in world.agents for item in pair)
        or pair[0] == pair[1]
        or json.dumps(pair, separators=(",", ":")) != key
    ):
        return "body_state_v1 relationship record is invalid"
    return "valid"


def test_each_relationship_key_is_judged_as_before_every_time(body_world) -> None:
    """A key's parse is remembered; whether its people exist is checked every time."""
    world = body_world
    first, second = sorted(world.agents)[:2]
    record = {
        "contact_count": 1,
        "trust": 0.5,
        "contact_minutes": 2,
        "last_contact_minute": 0,
        "last_interaction": None,
        "topic_ids": [],
    }
    keys = [
        f'["{first}","{second}"]',
        f'["{second}","{first}"]',
        f'["{first}","{first}"]',
        f'["{first}","nobody"]',
        f'[ "{first}","{second}"]',
        f'["{first}"]',
        f'["{first}","{second}","{first}"]',
        f'{{"{first}":"{second}"}}',
        "[",
        "",
        7,
    ]
    state = world.model_state[BODY_STATE_KEY]
    for key in keys:
        expected = _relationship_outcome_before(world, key)
        for _ in range(2):
            state["relationships"] = {key: dict(record)}
            try:
                validate_body_state(world)
                outcome = "valid"
            except ValueError as error:
                outcome = str(error)
            assert outcome == expected, key


def test_each_relationship_record_is_judged_as_before(body_world) -> None:
    """The record check, written inline, accepts what the field-by-field rules did."""
    world = body_world
    first, second = sorted(world.agents)[:2]
    record = {
        "contact_count": 1,
        "trust": 0.5,
        "contact_minutes": 2,
        "last_contact_minute": 0,
        "last_interaction": None,
        "topic_ids": [],
    }

    def accepted_before(candidate: object) -> bool:
        return (
            isinstance(candidate, dict)
            and set(candidate) == set(record)
            and not any(
                type(candidate[name]) is not int or candidate[name] < 0
                for name in ("contact_count", "contact_minutes")
            )
            and isinstance(candidate["topic_ids"], list)
            and len(candidate["topic_ids"]) <= 32
            and not any(
                not isinstance(item, str) or len(item) > 64 for item in candidate["topic_ids"]
            )
        )

    missing = dict(record)
    del missing["trust"]
    candidates = [
        record,
        missing,
        {**record, "extra": 1},
        {**record, "contact_count": -1},
        {**record, "contact_count": True},
        {**record, "contact_minutes": 2.0},
        {**record, "topic_ids": ("a",)},
        {**record, "topic_ids": ["a"] * 33},
        {**record, "topic_ids": ["a" * 65]},
        {**record, "topic_ids": [1]},
        {**record, "topic_ids": ["a", "b"]},
        ["not", "a", "dict"],
    ]
    state = world.model_state[BODY_STATE_KEY]
    for candidate in candidates:
        state["relationships"] = {f'["{first}","{second}"]': candidate}
        try:
            validate_body_state(world)
            accepted = True
        except ValueError as error:
            accepted = "relationship record is invalid" not in str(error)
        assert accepted is accepted_before(candidate), candidate
