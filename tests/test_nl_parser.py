"""Offline typed DraftPlan parsing; never calls a provider or mutates simulation state."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.natural_language import parse_natural_language
from sheltersim.llm.schemas import (
    DraftPlan,
    EffectProgram,
    FacilityTarget,
    ParseRequest,
    ScenarioSummary,
    ZoneTarget,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"


def summary(*, toilets: int = 1, default_zone_id: str | None = "main") -> ScenarioSummary:
    facilities = [
        FacilityTarget(
            id=f"toilet-{index}",
            name=f"Restroom {index}",
            kind="toilet",
            zone_id="main",
            capacity=4,
            working_units=4,
        )
        for index in range(1, toilets + 1)
    ]
    facilities.append(
        FacilityTarget(
            id="water-station",
            name="Water station",
            kind="water_station",
            zone_id="main",
            capacity=1,
            working_units=1,
        )
    )
    return ScenarioSummary(
        duration_minutes=10080,
        start_date=date(2026, 10, 12),
        default_zone_id=default_zone_id,
        facilities=facilities,
        zones=[
            ZoneTarget(id="main", name="Main hall", has_water_station=True),
            ZoneTarget(id="sleeping", name="Sleeping area"),
        ],
    )


def parse(text: str, *, context: ScenarioSummary | None = None):
    return parse_natural_language(
        ParseRequest(
            text=text,
            branch_id="baseline",
            selected_minute=0,
            scenario_summary=context or summary(),
        )
    )


@pytest.mark.parametrize(
    ("text", "op", "minute", "count"),
    [
        ("Day4 14:00にトイレ2基故障", "facility_outage", 5160, 2),
        ("4日目14時にトイレ二基が故障", "facility_outage", 5160, 2),
        ("4日目午後2時に便器1台が壊れた", "facility_outage", 5160, 1),
        ("Day 4 at 2pm toilet 2 fail", "facility_outage", 5160, 2),
        ("Day 4 staff 2 add", "add_staff", 4320, 2),
        ("4日目にスタッフを2人追加", "add_staff", 4320, 2),
        ("4日目に職員二名増員", "add_staff", 4320, 2),
        ("Day4 add 2 toilets", "add_toilet", 4320, 2),
        ("4日目に仮設トイレを2基追加", "add_toilet", 4320, 2),
        ("4日目にトイレが使えなくなる", "facility_outage", 4320, 1),
        ("4日目にトイレ二基の利用を停止", "facility_outage", 4320, 2),
        ("4日目スタッフ二人追加", "add_staff", 4320, 2),
        ("4日目にスタッフを二人増やす", "add_staff", 4320, 2),
        ("Day4 water outage", "water_outage", 4320, None),
        ("4日目に断水", "water_outage", 4320, None),
        ("4日目の給水が停止", "water_outage", 4320, None),
    ],
)
def test_offline_parser_handles_japanese_and_english_examples(
    text: str, op: str, minute: int, count: int | None
) -> None:
    result = parse(text)

    assert result.status == "parsed"
    assert result.issues == []
    assert result.draft_plan is not None
    action = result.draft_plan.effects[0]
    assert action.op == op
    assert action.start_minute == minute
    assert action.source_id == "offline-parser-h06"
    assert action.target_selector.target_id is not None
    if count is not None:
        assert action.parameters["count" if op != "facility_outage" else "units"] == count


def test_day_four_1400_converts_to_5160_and_keeps_existing_target_id() -> None:
    result = parse("Day4 14:00にトイレ2基故障")
    assert result.draft_plan is not None
    assert result.draft_plan.effects[0].start_minute == 5160
    assert result.draft_plan.effects[0].target_selector.target_id == "toilet-1"
    assert result.draft_plan.effects[0].target_selector.target_id != "A001"


def test_composed_plan_with_two_effect_types_passes_shared_schema() -> None:
    result = parse("Day4 toilet 2 failure and staff 2 added")

    assert result.status == "parsed"
    assert result.draft_plan is not None
    assert result.draft_plan.support_mode == "composed"
    assert {item.op for item in result.draft_plan.effects} == {"facility_outage", "add_staff"}
    decoded = DraftPlan.model_validate_json(result.draft_plan.model_dump_json())
    assert len(decoded.effects) == 2
    assert all(item.units and item.start_minute == 4320 for item in decoded.effects)


def test_schema_accepts_a_new_typed_operator_without_allowlisting_its_name() -> None:
    plan = parse("Day4 toilet 1 failure").draft_plan
    assert plan is not None
    document = plan.model_dump(
        mode="json",
        exclude={"branch_id", "fork_at_minute", "ambiguities", "requires_confirmation"},
    )
    document["effects"][0]["op"] = "future_supported_primitive"

    program = EffectProgram.model_validate(document)

    assert program.effects[0].op == "future_supported_primitive"


def test_guard_is_a_bounded_typed_ast_with_an_allowlisted_field_set() -> None:
    plan = parse("Day4 toilet 1 failure").draft_plan
    assert plan is not None
    document = plan.model_dump(mode="json")
    leaf = {"operator": "gte", "field": "target.working_units", "value": 2}
    document["effects"][0]["guard"] = {
        "operator": "and",
        "operands": [
            leaf,
            {"operator": "lt", "field": "simulation.minute", "value": 9000},
        ],
    }

    guarded = DraftPlan.model_validate(document)
    assert guarded.effects[0].guard is not None

    document["effects"][0]["guard"] = {
        "operator": "eq",
        "field": "world.stress",
        "value": 1,
    }
    with pytest.raises(ValidationError, match="world.stress"):
        DraftPlan.model_validate(document)


def test_guard_rejects_excessive_nesting() -> None:
    plan = parse("Day4 toilet 1 failure").draft_plan
    assert plan is not None
    document = plan.model_dump(mode="json")
    leaf = {"operator": "eq", "field": "simulation.minute", "value": 1}
    guard = leaf
    for _ in range(4):
        guard = {"operator": "and", "operands": [guard, leaf]}
    document["effects"][0]["guard"] = guard

    with pytest.raises(ValidationError, match="depth cannot exceed four"):
        DraftPlan.model_validate(document)


def test_explicit_date_and_time_range_are_converted_with_scenario_anchor() -> None:
    result = parse("2026-10-14 14:00から16:00まで toilet outage")

    assert result.draft_plan is not None
    effect = result.draft_plan.effects[0]
    assert (effect.start_minute, effect.end_minute) == (3720, 3840)


def test_ambiguous_target_returns_candidates_for_confirmation() -> None:
    result = parse("Day4 toilet 2 failure", context=summary(toilets=2))

    assert result.status == "clarification_required"
    assert result.draft_plan is not None
    assert result.draft_plan.support_mode == "clarification_required"
    assert result.draft_plan.requires_confirmation is True
    assert result.draft_plan.effects[0].target_selector.target_id is None
    assert result.draft_plan.effects[0].target_selector.candidate_ids == ["toilet-1", "toilet-2"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("午後にトイレが故障", "ambiguous_time"),
        ("Day4 staff -2 add", "invalid_count"),
        ("Day4 staff 1000 add", "invalid_count"),
        ("Day4 toilet 5 fail", "invalid_count"),
        ("感染者を1000人増やして", "unsupported_event"),
    ],
)
def test_unsafe_or_unsupported_input_is_rejected(text: str, code: str) -> None:
    result = parse(text)

    assert result.status == "rejected"
    assert result.draft_plan is None
    assert code in {item.code for item in result.issues}


def test_unknown_target_is_rejected_instead_of_inventing_a_facility() -> None:
    context = ScenarioSummary(
        duration_minutes=10080,
        zones=[ZoneTarget(id="main", name="Main hall")],
    )
    result = parse("Day4 toilet 1 failure", context=context)

    assert result.status == "rejected"
    assert result.draft_plan is None
    assert result.issues[0].code == "unknown_target"


def test_explicit_unknown_target_id_is_not_replaced_by_the_only_matching_kind() -> None:
    result = parse("Day4 toilet missing-restroom failure")

    assert result.status == "rejected"
    assert result.draft_plan is None
    assert result.issues[0].code == "unknown_target"
    assert result.issues[0].choices == ["toilet-1"]


def test_parser_does_not_mutate_world_rng_event_queue_or_branch_runtime() -> None:
    scenario = ScenarioSpec.load(FIXTURE)
    world = WorldState.from_scenario(scenario)
    runtime = BranchRuntime.from_world(world)
    original_world = world.canonical_json()
    runtime_world = runtime.world
    original_runtime_world = runtime.world.canonical_json()
    original_branch_id = runtime.branch_id
    original_parent_id = runtime.parent_branch_id
    original_command_log = list(runtime.command_log)

    scenario_summary = ScenarioSummary(
        duration_minutes=scenario.duration_minutes,
        facilities=[
            FacilityTarget(
                id=item.id,
                name=item.id,
                kind=item.kind,
                zone_id=item.zone_id,
                capacity=max(1, item.capacity),
                working_units=min(
                    item.capacity if item.working_units is None else item.working_units,
                    max(1, item.capacity),
                ),
            )
            for item in scenario.facilities
            if item.kind == "toilet"
        ],
        zones=[ZoneTarget(id=item.id, name=item.name) for item in scenario.zones],
        default_zone_id=scenario.zones[0].id,
    )
    result = parse_natural_language(
        ParseRequest(
            text="Day4 14:00 toilet_1 failure",
            branch_id=runtime.branch_id,
            selected_minute=world.minute_index,
            scenario_summary=scenario_summary,
        )
    )

    assert result.status == "parsed"
    assert world.canonical_json() == original_world
    assert runtime.world is runtime_world
    assert runtime.world.canonical_json() == original_runtime_world
    assert runtime.branch_id == original_branch_id
    assert runtime.parent_branch_id == original_parent_id
    assert runtime.command_log == original_command_log


def test_shared_contract_rejects_unknown_fields_and_invalid_time_windows() -> None:
    result = parse("Day4 toilet 1 failure")
    assert result.draft_plan is not None
    document = result.draft_plan.model_dump(mode="json")
    document["effects"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        DraftPlan.model_validate(document)

    document = result.draft_plan.model_dump(mode="json")
    document["effects"][0]["end_minute"] = document["effects"][0]["start_minute"]
    with pytest.raises(ValidationError, match="later than"):
        DraftPlan.model_validate(document)


def test_hyphenated_facility_id_is_a_target_not_a_negative_count() -> None:
    result = parse("Day4 toilet-2を故障させる", context=summary(toilets=2))

    assert result.status == "parsed", result.issues
    assert result.draft_plan is not None
    effect = result.draft_plan.effects[0]
    assert effect.op == "facility_outage"
    assert effect.target_selector.target_id == "toilet-2"
    assert effect.parameters == {"units": 1}


def test_out_of_range_outage_count_names_units_not_evacuees() -> None:
    result = parse("Day4 トイレが2000基故障")

    assert result.status == "rejected"
    message = next(item.message for item in result.issues if item.code == "invalid_count")
    assert "停止するユニット" in message
    assert "避難者" not in message
