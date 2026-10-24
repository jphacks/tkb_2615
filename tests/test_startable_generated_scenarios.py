"""Default generated drafts can start without hand-entered households or geometry."""

from __future__ import annotations

import pytest

from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.region.facility import FacilityDesignRequest, StandardShelterGenerator
from sheltersim.region.models import ACCESSIBLE_ROUTE_MIN_WIDTH_M
from sheltersim.region.population import (
    OPERATOR_POPULATION_SOURCE_ID,
    SYNTHETIC_HOUSEHOLD_TEMPLATE_ID,
)


def _draft(evacuees: int, seed: int, **extra):
    return InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality", disaster="earthquake",
        evacuees=evacuees, duration_days=1, seed=seed, **extra,
    ))


@pytest.mark.parametrize("evacuees", [10, 30, 100])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_default_drafts_have_no_approval_blockers(evacuees: int, seed: int) -> None:
    draft = _draft(evacuees, seed)
    assert draft.approval_blockers == ()
    assert draft.can_start


def test_default_templates_link_every_minor_and_are_recorded_as_assumptions() -> None:
    scenario = _draft(100, 7).scenario
    people = scenario.model_configs["population"]["people"]
    context = scenario.model_configs["population_context"]["people"]
    minors = [agent_id for agent_id, person in people.items() if person["age_years"] < 18]
    assert minors
    assert all(context[agent_id]["guardian_status"] == "accompanied" for agent_id in minors)
    assert all(context[agent_id]["parent_ids"] for agent_id in minors)
    assert SYNTHETIC_HOUSEHOLD_TEMPLATE_ID in scenario.model_configs["population"]["source_ids"]
    # Assumed templates are not operator input.
    assert OPERATOR_POPULATION_SOURCE_ID not in scenario.model_configs["population"]["source_ids"]
    source = next(row for row in scenario.provenance["sources"]
                  if row["id"] == SYNTHETIC_HOUSEHOLD_TEMPLATE_ID)
    assert source["source_kind"] == "assumption"


def test_entered_household_shapes_replace_the_default_templates() -> None:
    unlinked = {
        "household_composition": {
            "templates": [
                {"template_id": f"unlinked-{size}", "members": [{"role": "unspecified"}] * size}
                for size in range(1, 6)
            ],
            "source_ids": ["SRC-TEST-UNLINKED-HOUSEHOLDS"],
        },
        "source_ids": ["SRC-TEST-UNLINKED-HOUSEHOLDS"],
    }
    draft = _draft(20, 42, population_conditions=unlinked)
    population_sources = draft.scenario.model_configs["population"]["source_ids"]
    assert SYNTHETIC_HOUSEHOLD_TEMPLATE_ID not in population_sources
    assert any("未成年者が" in row for row in draft.approval_blockers)


def test_adult_only_selection_drops_child_templates_instead_of_failing() -> None:
    draft = _draft(30, 1, evacuation_selection={
        "age_band_multipliers": {"child": 0.0, "adult": 1.0, "older_adult": 1.0},
        "source_ids": ["SRC-TEST-ADULT-SELECTION"],
    })
    people = draft.scenario.model_configs["population"]["people"]
    assert all(person["age_years"] >= 18 for person in people.values())
    assert draft.can_start


@pytest.mark.parametrize("population", [10, 30, 100])
def test_small_standard_plans_give_every_room_an_accessible_doorway(population: int) -> None:
    design = StandardShelterGenerator().generate(FacilityDesignRequest(population=population))
    assert all(route.accessible and route.width_m >= ACCESSIBLE_ROUTE_MIN_WIDTH_M
               for route in design.geometry.routes)
    assert design.capacity_supported >= population
    layout = next(row for row in design.assumptions if row["subject"] == "standard_shelter_layout")
    assert layout["value"]["frontage_widened_zones"]
    assert any("出入口幅" in warning for warning in design.warnings)


def test_large_standard_plans_keep_their_requested_shares() -> None:
    design = StandardShelterGenerator().generate(FacilityDesignRequest(population=300))
    layout = next(row for row in design.assumptions if row["subject"] == "standard_shelter_layout")
    assert layout["value"]["frontage_widened_zones"] == []
    assert layout["value"]["zone_shares"]["intake"] == pytest.approx(0.05)
