"""The plan preview routes of the Project screen accept a reviewed plan and refuse mistakes.

A plan is input the operator draws or uploads, so every mistake must be refused with
a reason: a StorageError (HTTP 409) or a ValueError, including a contract
ValidationError (HTTP 400). Any other exception is an HTTP 500. The routes are called
on the ProjectAPI with the synthetic shelter of tests/support/plans.py, uploaded as a
drawing or as GeoJSON.
"""

from __future__ import annotations

import copy
import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from support.plans import SHELTER_TRACE, rooms_plan_png, shelter_plan_geojson, shelter_plan_png

from sheltersim.storage.projects import StorageError

REFUSED = (StorageError, ValueError)
_FAILS_WITHOUT_REASON = pytest.mark.xfail(
    raises=(StopIteration, TypeError), strict=True, reason="#202: fails without a reason"
)


@pytest.fixture
def drawn_plan(ui_project_api, upload_plan) -> tuple[Any, str, dict[str, Any]]:
    """The ProjectAPI, the uploaded shelter drawing, and its extraction preview."""
    asset = upload_plan(ui_project_api, shelter_plan_png())
    extracted = ui_project_api.post(
        ["project", "assets", asset, "plan-preview"], {"operation": "extract"}
    )
    return ui_project_api, asset, extracted


def _plan_preview(api: Any, asset: str, body: dict[str, Any]) -> dict[str, Any]:
    return api.post(["project", "assets", asset, "plan-preview"], body)


def _feature(document: dict, kind: str) -> dict:
    return next(
        item for item in document["features"] if item["properties"]["sheltersim_type"] == kind
    )


def _footprint(facility_id: str, pixels: list[list[int]]) -> dict:
    return {"facility_id": facility_id, "pixel_polygon": pixels}


TRACE_MISTAKES: dict[str, Callable[[dict], object]] = {
    "facility in an unknown zone": lambda t: t["facilities"][0].update(zone_id="nowhere"),
    "facility outside its zone": lambda t: t["facilities"][0].update(pixel_x=100),
    "point outside the page": lambda t: t["facilities"][0].update(pixel_x=500),
    "infinite coordinate": lambda t: t["facilities"][0].update(pixel_x=float("inf")),
    "text capacity": lambda t: t["facilities"][0].update(capacity="many"),
    "route to an unknown zone": lambda t: t["routes"][0].update(to_zone_id="nowhere"),
    "route within one zone": lambda t: t["routes"][0].update(to_zone_id="sleeping"),
    "route through a closed wall": lambda t: t.update(route_openings=t["route_openings"][1:]),
    "route opening of an unknown route": lambda t: t["route_openings"].append(
        {"route_id": "nowhere", "opening_id": "door_food"}
    ),
    "opening in an unknown wall": lambda t: t["openings"][0].update(wall_id="nowhere"),
    "zone without area": lambda t: t["zones"][2].update(
        pixel_polygon=[[200, 150], [300, 150], [380, 150]]
    ),
    "self-crossing zone": lambda t: t["zones"][2].update(
        pixel_polygon=[[200, 150], [380, 280], [380, 150], [200, 280]]
    ),
    "overlapping zones": lambda t: t["zones"][1].update(
        pixel_polygon=[[150, 20], [380, 20], [380, 150], [150, 150]]
    ),
    "duplicate zone": lambda t: t["zones"].append(copy.deepcopy(t["zones"][0])),
    "NaN zone corner": lambda t: t["zones"][0]["pixel_polygon"].__setitem__(0, [float("nan"), 20]),
    "NaN wall end": lambda t: t["walls"][0].update(start_px=[float("nan"), 20]),
    "no sleeping zone": lambda t: [
        item.update({key: "hall"})
        for item in (*t["zones"], *t["routes"])
        for key in ("zone_id", "from_zone_id", "to_zone_id")
        if item.get(key) == "sleeping"
    ],
    "footprint outside its zone": lambda t: t.update(
        equipment_footprints=[
            _footprint("water_station_main", [[150, 40], [270, 40], [270, 60], [150, 60]])
        ]
    ),
    "service point of an unknown facility": lambda t: t.update(
        service_points=[
            {
                "id": "point",
                "facility_id": "nowhere",
                "point_m": {"x": 26, "y": 5},
                "service_capacity": 1,
                "queue_id": "queue",
            }
        ]
    ),
    "footprint of a facility in an unknown zone": pytest.param(
        lambda t: (
            t["facilities"][0].update(zone_id="nowhere"),
            t.update(
                equipment_footprints=[
                    _footprint("water_station_main", [[250, 40], [270, 40], [270, 60], [250, 60]])
                ]
            ),
        ),
        marks=_FAILS_WITHOUT_REASON,
    ),
}


def test_a_traced_drawing_is_approved_and_read_back_bound_to_its_raster(drawn_plan):
    api, asset, extracted = drawn_plan
    preview = _plan_preview(
        api,
        asset,
        {
            "operation": "trace",
            "trace": SHELTER_TRACE,
            "raster_preview_id": extracted["preview_id"],
        },
    )
    approval = {
        "preview_id": preview["preview_id"],
        "asset_id": asset,
        "approved_hash": preview["value_hash"],
        "approved": True,
    }

    with pytest.raises(StorageError, match="geometry_approval_hash_mismatch"):
        api.post(["project", "geometries", "plan"], {**approval, "approved_hash": "0" * 64})
    with pytest.raises(StorageError, match="geometry_requires_explicit_approval"):
        api.post(["project", "geometries", "plan"], {**approval, "approved": False})
    approved = api.post(["project", "geometries", "plan"], approval)

    assert api.get(["project", "geometries", "plan"], {}) == approved
    provenance = approved["geometry"]["source_provenance"]
    assert provenance["raster_asset_sha256"] == extracted["raster_preview"]["asset_id"]
    assert provenance["source_page_index"] == 0
    assert approved["geometry"]["geometry"]["facility_points"]["water_station_main"] == {
        "x": 26.0,
        "y": 5.0,
    }


def test_reviewed_candidates_become_a_geometry_of_the_accepted_features_only(
    ui_project_api, upload_plan
):
    # Three rooms side by side; only the middle one is a room candidate (#200).
    api, asset = ui_project_api, upload_plan(ui_project_api, rooms_plan_png(2))
    extracted = _plan_preview(api, asset, {"operation": "extract"})
    features = extracted["value"]["features"]
    (room,) = [item for item in features if item["feature_kind"] == "zone"]
    reviews = [
        {"feature_id": item["feature_id"], "reviewer_id": "operator", "decision": "rejected"}
        for item in features
        if item["feature_kind"] != "zone"
    ]
    reviews.append(
        {
            "feature_id": room["feature_id"],
            "reviewer_id": "operator",
            "decision": "accepted",
            "corrected_attributes": {"zone_id": "sleeping", "name": "Hall"},
        }
    )
    reviewed = _plan_preview(
        api,
        asset,
        {"operation": "review", "proposal_preview_id": extracted["preview_id"], "reviews": reviews},
    )

    geometry = _plan_preview(
        api,
        asset,
        {
            "operation": "reviewed_trace",
            "reviewed_preview_id": reviewed["preview_id"],
            "building_width_m": 40,
            "building_height_m": 30,
            "operator_id": "operator",
        },
    )

    assert geometry["kind"] == "geometry"
    (zone,) = geometry["value"]["geometry"]["zones"]
    # The candidate's pixel corners at 10 px per metre.
    corners = {(round(point["x"], 1), round(point["y"], 1)) for point in zone["polygon"]}
    pixels = {(round(x / 10, 1), round(y / 10, 1)) for x, y in room["coordinates_px"]}
    assert (zone["zone_id"], corners) == ("sleeping", pixels)
    assert geometry["value"]["editable_geometry"] is None, "rejected walls are left out"
    with pytest.raises(ValueError, match="cannot be reviewed again"):
        _plan_preview(
            api,
            asset,
            {
                "operation": "review",
                "proposal_preview_id": reviewed["preview_id"],
                "reviews": reviews[:1],
            },
        )


@pytest.mark.parametrize("change", TRACE_MISTAKES.values(), ids=TRACE_MISTAKES.keys())
def test_a_mistaken_trace_is_refused_with_a_reason(drawn_plan, change):
    api, asset, extracted = drawn_plan
    trace = copy.deepcopy(SHELTER_TRACE)
    change(trace)

    with pytest.raises(REFUSED):
        _plan_preview(
            api,
            asset,
            {"operation": "trace", "trace": trace, "raster_preview_id": extracted["preview_id"]},
        )


REQUEST_MISTAKES: dict[str, Callable[[str], dict[str, Any]]] = {
    "unknown operation": lambda _: {"operation": "magic"},
    "unknown field": lambda _: {"operation": "trace", "trace": SHELTER_TRACE, "extra": 1},
    "trace that is not an object": lambda _: {"operation": "trace", "trace": "hall"},
    "second page of an image": lambda _: {
        "operation": "trace",
        "trace": SHELTER_TRACE,
        "page_index": 1,
    },
    "trace of another raster size": lambda extraction: {
        "operation": "trace",
        "trace": {**SHELTER_TRACE, "raster_pixel_width": 800, "raster_pixel_height": 600},
        "raster_preview_id": extraction,
    },
    "unknown raster preview": lambda _: {
        "operation": "trace",
        "trace": SHELTER_TRACE,
        "raster_preview_id": "plan-unknown",
    },
    "extraction of page 1 of an image": lambda _: {"operation": "extract", "page_index": 1},
    "extraction smaller than 64 px": lambda _: {"operation": "extract", "max_dimension_px": 10},
    "extraction page true": lambda _: {"operation": "extract", "page_index": True},
    "review of an unknown preview": lambda _: {
        "operation": "review",
        "proposal_preview_id": "plan-unknown",
        "reviews": [],
    },
    "review of an unknown candidate": lambda extraction: {
        "operation": "review",
        "proposal_preview_id": extraction,
        "reviews": [{"feature_id": "unknown", "decision": "accepted", "reviewer_id": "operator"}],
    },
    "correction outside the page": lambda extraction: {
        "operation": "review",
        "proposal_preview_id": extraction,
        "reviews": [
            {
                "feature_id": "wall-0000",
                "decision": "accepted",
                "reviewer_id": "operator",
                "corrected_coordinates_px": [[0, 0], [900, 0]],
            }
        ],
    },
    "candidate reviewed twice in one update": lambda extraction: {
        "operation": "review",
        "proposal_preview_id": extraction,
        "reviews": [{"feature_id": "wall-0000", "decision": "accepted", "reviewer_id": "a"}] * 2,
    },
    "reviews that are text": lambda extraction: {
        "operation": "review",
        "proposal_preview_id": extraction,
        "reviews": "all",
    },
    "trace of candidates still pending": lambda extraction: {
        "operation": "reviewed_trace",
        "reviewed_preview_id": extraction,
        "building_width_m": 40,
        "building_height_m": 30,
    },
    "revision of an unknown geometry": lambda _: {
        "operation": "geometry_revision",
        "base_geometry_id": "unknown",
        "base_geometry_hash": "0" * 64,
        "geometry": {},
    },
}


@pytest.mark.parametrize("body", REQUEST_MISTAKES.values(), ids=REQUEST_MISTAKES.keys())
def test_a_mistaken_plan_request_is_refused_with_a_reason(drawn_plan, body):
    api, asset, extracted = drawn_plan

    with pytest.raises(REFUSED):
        _plan_preview(api, asset, body(extracted["preview_id"]))


def test_a_plan_page_that_does_not_exist_is_refused(ui_project_api, upload_plan):
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(stream, format="PDF")
    asset = upload_plan(ui_project_api, stream.getvalue(), "application/pdf")

    with pytest.raises(ValueError, match="selected PDF page does not exist"):
        _plan_preview(ui_project_api, asset, {"operation": "extract", "page_index": 1})
    with pytest.raises(ValueError, match="PDF plan trace requires its zero-based source page"):
        _plan_preview(ui_project_api, asset, {"operation": "trace", "trace": SHELTER_TRACE})


GEOJSON_MISTAKES: dict[str, Callable[[dict], object]] = {
    "text width": lambda d: d.update(width_m="wide"),
    "no units": lambda d: d.pop("units"),
    "features that are an object": lambda d: d.update(features={}),
    "feature that is text": lambda d: d["features"].append("hall"),
    "feature without geometry": lambda d: _feature(d, "zone").pop("geometry"),
    "zone without a ring": lambda d: _feature(d, "zone")["geometry"].update(coordinates=5),
    "zone ring of numbers": lambda d: _feature(d, "zone")["geometry"].update(
        coordinates=[[1, 2, 3]]
    ),
    "zone with a hole": lambda d: _feature(d, "zone")["geometry"]["coordinates"].append(
        [[1, 1], [2, 1], [2, 2]]
    ),
    "point with one coordinate": lambda d: _feature(d, "facility")["geometry"].update(
        coordinates=[1]
    ),
    "point of text": lambda d: _feature(d, "facility")["geometry"].update(coordinates=["a", "b"]),
    "duplicate facility": lambda d: d["features"].append(copy.deepcopy(_feature(d, "facility"))),
    "duplicate zone": lambda d: d["features"].append(copy.deepcopy(_feature(d, "zone"))),
    "duplicate route": lambda d: d["features"].append(copy.deepcopy(_feature(d, "route"))),
    "facility in an unknown zone": lambda d: _feature(d, "facility")["properties"].update(
        zone_id="nowhere"
    ),
    "route to an unknown zone": lambda d: _feature(d, "route")["properties"].update(
        to_zone_id="nowhere"
    ),
    "route within one zone": lambda d: _feature(d, "route")["properties"].update(
        to_zone_id="sleeping"
    ),
    "route through an unknown opening": lambda d: _feature(d, "route")["properties"].update(
        opening_id="nowhere"
    ),
    "opening in an unknown wall": lambda d: _feature(d, "opening")["properties"].update(
        wall_id="nowhere"
    ),
    "negative travel minutes": lambda d: _feature(d, "route")["properties"].update(
        travel_minutes=-5
    ),
    "text capacity": lambda d: _feature(d, "facility")["properties"].update(capacity="many"),
    "facility without an ID": lambda d: _feature(d, "facility")["properties"].pop("id"),
    "unknown feature type": lambda d: d["features"].append(
        {
            "type": "Feature",
            "properties": {"sheltersim_type": "stairs"},
            "geometry": {"type": "Point", "coordinates": [1, 1]},
        }
    ),
    "no sleeping zone": lambda d: [
        item["properties"].update({key: "hall"})
        for item in d["features"]
        for key in ("zone_id", "from_zone_id", "to_zone_id")
        if item["properties"].get(key) == "sleeping"
    ],
    "envelope geometry that is a list": lambda d: (
        d.clear(),
        d.update(units="m", origin="top_left", geometry=[]),
    ),
    "width that is an object": pytest.param(
        lambda d: d.update(width_m={"metres": 40}), marks=_FAILS_WITHOUT_REASON
    ),
    "usable area null": pytest.param(
        lambda d: _feature(d, "zone")["properties"].update(usable_area_m2=None),
        marks=_FAILS_WITHOUT_REASON,
    ),
    "usable area list": pytest.param(
        lambda d: _feature(d, "zone")["properties"].update(usable_area_m2=[100]),
        marks=_FAILS_WITHOUT_REASON,
    ),
    "route coordinates number": pytest.param(
        lambda d: _feature(d, "route")["geometry"].update(coordinates=5),
        marks=_FAILS_WITHOUT_REASON,
    ),
    "route coordinates null": pytest.param(
        lambda d: _feature(d, "route")["geometry"].update(coordinates=None),
        marks=_FAILS_WITHOUT_REASON,
    ),
    "envelope zones that are a number": pytest.param(
        lambda d: (d.clear(), d.update(units="m", origin="top_left", zones=5, geometry={})),
        marks=_FAILS_WITHOUT_REASON,
    ),
}


def test_a_geojson_plan_is_imported_as_its_features(ui_project_api, upload_plan):
    content = json.dumps(shelter_plan_geojson()).encode()
    asset = upload_plan(ui_project_api, content, "application/geo+json")

    preview = _plan_preview(ui_project_api, asset, {"operation": "geojson"})

    value = preview["value"]
    assert {zone["zone_id"]: zone["usable_area_m2"] for zone in value["geometry"]["zones"]} == {
        "sleeping": 100,
        "food": 100,
        "toilet": 100,
    }
    assert [edge["id"] for edge in value["zone_edges"]] == [
        "sleeping_food",
        "food_sleeping",
        "sleeping_toilet",
        "toilet_sleeping",
    ]
    assert len(value["editable_geometry"]["walls"]) == 6
    assert value["geometry"]["source_kind"] == "operator"


@pytest.mark.parametrize("change", GEOJSON_MISTAKES.values(), ids=GEOJSON_MISTAKES.keys())
def test_a_mistaken_geojson_plan_is_refused_with_a_reason(ui_project_api, upload_plan, change):
    document = shelter_plan_geojson()
    change(document)
    asset = upload_plan(ui_project_api, json.dumps(document).encode(), "application/geo+json")

    with pytest.raises(REFUSED):
        _plan_preview(ui_project_api, asset, {"operation": "geojson"})


@pytest.mark.parametrize("content", [b"[1, 2]", b"{not json"], ids=["list", "not JSON"])
def test_a_json_upload_that_is_not_an_object_is_refused_when_it_finishes(
    ui_project_api, upload_plan, content
):
    with pytest.raises(REFUSED):
        upload_plan(ui_project_api, content, "application/geo+json")
