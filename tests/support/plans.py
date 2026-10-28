"""A synthetic shelter floor plan, drawn as an image and written as GeoJSON, and its upload.

The plan is 40 × 30 m, drawn at 10 px per metre on a 400 × 300 px page. The sleeping
hall fills the west half; the east half is split into the water and meal area (food)
and the toilets. Each east room opens to the hall through a 2 m door in the
partition, and every route is the accessible minimum of 1.8 m. The facilities are the
ones the Scenario generator needs to start a Scenario: water, meals, a toilet and an
accessible toilet.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
from collections.abc import Callable
from typing import Any

import pytest

PLAN_WIDTH_PX, PLAN_HEIGHT_PX = 400, 300
PLAN_WIDTH_M, PLAN_HEIGHT_M = 40, 30

# The operator's trace of the drawing in page pixels, as the Project screen's plan
# editor posts it (operation "trace").
SHELTER_TRACE: dict[str, Any] = {
    "raster_pixel_width": PLAN_WIDTH_PX,
    "raster_pixel_height": PLAN_HEIGHT_PX,
    "building_width_m": PLAN_WIDTH_M,
    "building_height_m": PLAN_HEIGHT_M,
    "operator_id": "operator",
    "zones": [
        {
            "zone_id": "sleeping",
            "name": "Hall",
            "pixel_polygon": [[20, 20], [200, 20], [200, 280], [20, 280]],
        },
        {
            "zone_id": "food",
            "name": "Water and meals",
            "pixel_polygon": [[200, 20], [380, 20], [380, 150], [200, 150]],
        },
        {
            "zone_id": "toilet",
            "name": "Toilets",
            "pixel_polygon": [[200, 150], [380, 150], [380, 280], [200, 280]],
        },
    ],
    "facilities": [
        {
            "id": "water_station_main",
            "kind": "water_station",
            "zone_id": "food",
            "capacity": 1,
            "pixel_x": 260,
            "pixel_y": 50,
        },
        {
            "id": "meal_distribution_main",
            "kind": "meal_distribution",
            "zone_id": "food",
            "capacity": 1,
            "pixel_x": 330,
            "pixel_y": 110,
        },
        {
            "id": "toilet_01",
            "kind": "toilet",
            "zone_id": "toilet",
            "capacity": 1,
            "working_units": 1,
            "pixel_x": 260,
            "pixel_y": 250,
        },
        {
            "id": "accessible_toilet_01",
            "kind": "toilet",
            "zone_id": "toilet",
            "capacity": 1,
            "working_units": 1,
            "pixel_x": 330,
            "pixel_y": 250,
        },
    ],
    "routes": [
        {
            "edge_id": edge_id,
            "from_zone_id": start,
            "to_zone_id": end,
            "points_px": points,
            "width_m": 1.8,
        }
        for edge_id, start, end, points in (
            ("sleeping_food", "sleeping", "food", [[100, 80], [300, 80]]),
            ("food_sleeping", "food", "sleeping", [[300, 80], [100, 80]]),
            ("sleeping_toilet", "sleeping", "toilet", [[100, 210], [300, 210]]),
            ("toilet_sleeping", "toilet", "sleeping", [[300, 210], [100, 210]]),
        )
    ],
    "walls": [
        {"wall_id": "north", "start_px": [20, 20], "end_px": [380, 20]},
        {"wall_id": "south", "start_px": [20, 280], "end_px": [380, 280]},
        {"wall_id": "west", "start_px": [20, 20], "end_px": [20, 280]},
        {"wall_id": "east", "start_px": [380, 20], "end_px": [380, 280]},
        {"wall_id": "partition", "start_px": [200, 20], "end_px": [200, 280]},
        {"wall_id": "food_toilet", "start_px": [200, 150], "end_px": [380, 150]},
    ],
    "openings": [
        {
            "opening_id": "door_food",
            "wall_id": "partition",
            "start_px": [200, 70],
            "end_px": [200, 90],
            "width_m": 2.0,
        },
        {
            "opening_id": "door_toilet",
            "wall_id": "partition",
            "start_px": [200, 200],
            "end_px": [200, 220],
            "width_m": 2.0,
        },
    ],
    "route_openings": [
        {"route_id": "sleeping_food", "opening_id": "door_food"},
        {"route_id": "food_sleeping", "opening_id": "door_food"},
        {"route_id": "sleeping_toilet", "opening_id": "door_toilet"},
        {"route_id": "toilet_sleeping", "opening_id": "door_toilet"},
    ],
}


def shelter_plan_png() -> bytes:
    """The drawing SHELTER_TRACE traces: walls 4 px thick, with gaps for the two doors."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (PLAN_WIDTH_PX, PLAN_HEIGHT_PX), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 380, 280), outline=0, width=4)
    for top, bottom in ((20, 70), (90, 200), (220, 280)):
        draw.line((200, top, 200, bottom), fill=0, width=4)
    draw.line((200, 150, 380, 150), fill=0, width=4)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def rooms_plan_png(partitions: int) -> bytes:
    """A 400 × 300 px rectangle split into partitions + 1 rooms by full-height walls."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (PLAN_WIDTH_PX, PLAN_HEIGHT_PX), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 380, 280), outline=0, width=4)
    for index in range(1, partitions + 1):
        x = 20 + 360 * index // (partitions + 1)
        draw.line((x, 20, x, 280), fill=0, width=4)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def shelter_plan_geojson() -> dict[str, Any]:
    """SHELTER_TRACE as a metric GeoJSON FeatureCollection (operation "geojson").

    GeoJSON states each zone's usable area instead of deriving it from the polygon.
    """

    def metres(point: list[float]) -> list[float]:
        return [point[0] * PLAN_WIDTH_M / PLAN_WIDTH_PX, point[1] * PLAN_HEIGHT_M / PLAN_HEIGHT_PX]

    def feature(kind: str, properties: dict[str, Any], shape: str, coordinates: Any) -> dict:
        return {
            "type": "Feature",
            "properties": {"sheltersim_type": kind, **properties},
            "geometry": {"type": shape, "coordinates": coordinates},
        }

    trace = SHELTER_TRACE
    doors = {item["route_id"]: item["opening_id"] for item in trace["route_openings"]}
    features = []
    for zone in trace["zones"]:
        ring = [metres(point) for point in zone["pixel_polygon"]]
        properties = {"zone_id": zone["zone_id"], "name": zone["name"], "usable_area_m2": 100}
        features.append(feature("zone", properties, "Polygon", [[*ring, ring[0]]]))
    for facility in trace["facilities"]:
        properties = {key: value for key, value in facility.items() if not key.startswith("pixel")}
        point = metres([facility["pixel_x"], facility["pixel_y"]])
        features.append(feature("facility", properties, "Point", point))
    for route in trace["routes"]:
        properties = {
            key: route[key] for key in ("edge_id", "from_zone_id", "to_zone_id", "width_m")
        }
        properties |= {"accessible": True, "opening_id": doors[route["edge_id"]]}
        line = [metres(point) for point in route["points_px"]]
        features.append(feature("route", properties, "LineString", line))
    for wall in trace["walls"]:
        line = [metres(wall["start_px"]), metres(wall["end_px"])]
        features.append(feature("wall", {"wall_id": wall["wall_id"]}, "LineString", line))
    for opening in trace["openings"]:
        properties = {key: opening[key] for key in ("opening_id", "wall_id", "width_m")}
        line = [metres(opening["start_px"]), metres(opening["end_px"])]
        features.append(feature("opening", properties, "LineString", line))
    return {
        "type": "FeatureCollection",
        "units": "m",
        "origin": "top_left",
        "width_m": PLAN_WIDTH_M,
        "height_m": PLAN_HEIGHT_M,
        "features": features,
    }


@pytest.fixture
def shelter_trace() -> dict[str, Any]:
    """A fresh copy of SHELTER_TRACE, safe to change in one test."""
    return copy.deepcopy(SHELTER_TRACE)


@pytest.fixture
def upload_plan() -> Callable[..., str]:
    """Upload a plan file to a Project as the Project screen does, and return its asset ID.

    upload_plan(api, content, media_type) posts the upload, its one chunk and its
    finish to the ProjectAPI of project "project", with the operator's permission.
    """
    counter = iter(range(1_000_000))

    def upload(api: Any, content: bytes, media_type: str = "image/png") -> str:
        upload_id = f"upload-{next(counter)}"
        vector = media_type in {"application/json", "application/geo+json"}
        sha256 = hashlib.sha256(content).hexdigest()
        root = ["project", "uploads", upload_id]
        api.post(
            root,
            {
                "media_type": media_type,
                "byte_count": len(content),
                "sha256": sha256,
                "permission_confirmed": True,
                "sensitive_building": False,
                "file_name": "plan." + media_type.rsplit("/", 1)[-1].removeprefix("geo+"),
                "coordinate_system": "local_metre" if vector else "raster_pixel",
            },
        )
        api.post(
            [*root, "chunks"],
            {"index": 0, "data_base64": base64.b64encode(content).decode("ascii")},
        )
        api.post([*root, "finish"], {})
        return sha256

    return upload
