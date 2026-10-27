"""A synthetic visual layout maps one to one onto the Scenario's zones, edges and
facilities, and is not part of the Scenario hash."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sheltersim.domain.layouts import LayoutSpec

LAYOUT_PATH = Path(__file__).parents[3] / "fixtures" / "layouts" / "gym_synthetic.json"


def test_synthetic_layout_maps_scenario_zones_edges_and_facilities_one_to_one(
    small_replay_scenario,
) -> None:
    layout = LayoutSpec.from_mapping(json.loads(LAYOUT_PATH.read_text(encoding="utf-8")))
    scenario = small_replay_scenario()

    layout.validate_scenario(scenario)

    assert {zone.zone_id for zone in layout.zones if zone.modelled} == {
        zone.id for zone in scenario.zones
    }
    assert {edge.edge_id for edge in layout.route_edges} == {
        edge.id for edge in scenario.zone_edges
    }
    assert all(
        facility.display_zone_id in {zone.zone_id for zone in layout.zones}
        for facility in layout.facilities
    )
    assert "合成" in layout.note


def test_layout_rejects_missing_graph_edges_and_out_of_canvas_polygons(
    small_replay_scenario,
) -> None:
    document = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    document["route_edges"].pop()
    layout = LayoutSpec.from_mapping(document)
    with pytest.raises(ValueError, match="route edges"):
        layout.validate_scenario(small_replay_scenario())

    document = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    document["zones"][0]["polygon"][0] = [-1, 62]
    with pytest.raises(ValueError, match="outside the canvas"):
        LayoutSpec.from_mapping(document)


def test_visual_layout_edit_is_not_part_of_scenario_hash(small_replay_scenario) -> None:
    scenario = small_replay_scenario()
    before = hashlib.sha256(scenario.canonical_json().encode()).hexdigest()
    layout = LayoutSpec.from_mapping(json.loads(LAYOUT_PATH.read_text(encoding="utf-8")))
    moved_zone = layout.zones[0].model_copy(
        update={"polygon": tuple((x + 10, y + 10) for x, y in layout.zones[0].polygon)}
    )
    moved_layout = layout.model_copy(update={"zones": (moved_zone, *layout.zones[1:])})
    after = hashlib.sha256(scenario.canonical_json().encode()).hexdigest()

    assert moved_layout.zones[0].polygon != layout.zones[0].polygon
    assert before == after
