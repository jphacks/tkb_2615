from __future__ import annotations

import base64
import hashlib

import pytest

from sheltersim.contracts.workspace import AssetBegin, AssetChunk
from sheltersim.region.plan_import import ManualPlanTrace
from sheltersim.storage.artifacts import ArtifactStore
from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.web.plan_services import PlanServices


def _plan_service(tmp_path):
    store = ProjectStore(tmp_path)
    store.create_project("project")
    artifacts = ArtifactStore(store)
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7kQAAAAASUVORK5CYII="
    )
    asset_id = hashlib.sha256(content).hexdigest()
    artifacts.begin(
        "project",
        "upload",
        AssetBegin(
            media_type="image/png",
            byte_count=len(content),
            sha256=asset_id,
            permission_confirmed=True,
            sensitive_building=False,
            file_name="synthetic-plan.png",
            coordinate_system="raster_pixel",
        ),
    )
    artifacts.chunk(
        "project",
        "upload",
        AssetChunk(
            index=0,
            data_base64=base64.b64encode(content).decode("ascii"),
        ),
    )
    artifacts.finish("project", "upload")
    return store, PlanServices(store), asset_id


def test_imported_plan_arrival_route_is_a_new_validated_geometry_revision(tmp_path) -> None:
    store, plans, asset_id = _plan_service(tmp_path)
    trace = ManualPlanTrace.model_validate(
        {
            "raster_pixel_width": 100,
            "raster_pixel_height": 100,
            "source_page_index": 0,
            "building_width_m": 10,
            "building_height_m": 10,
            "zones": [
                {
                    "zone_id": "sleeping",
                    "name": "Sleeping",
                    "pixel_polygon": [[10, 10], [90, 10], [90, 90], [10, 90]],
                }
            ],
            "facilities": [],
            "routes": [],
        }
    )
    initial = plans.preview(
        "project", asset_id, {"operation": "trace", "trace": trace.model_dump(mode="json")}
    )
    base_id = "geometry-base"
    base = plans.approve(
        "project",
        base_id,
        {
            "preview_id": initial["preview_id"],
            "asset_id": asset_id,
            "approved_hash": initial["value_hash"],
            "approved": True,
        },
    )
    base_snapshot = plans.geometry("project", base_id)
    geometry = dict(base["geometry"]["geometry"])
    geometry["arrival_approach_routes"] = [
        {
            "route_id": "gate-to-sleeping",
            "destination_zone_id": "sleeping",
            "points_m": [{"x": 0.5, "y": 5.0}, {"x": 2.0, "y": 5.0}],
            "walking_speed_m_per_min": 1.2,
            "source_kind": "operator",
            "source_ids": ["SRC-SITE-ROUTE-SURVEY"],
        }
    ]

    revision = plans.preview(
        "project",
        asset_id,
        {
            "operation": "geometry_revision",
            "base_geometry_id": base_id,
            "base_geometry_hash": base["geometry_hash"],
            "geometry": geometry,
            "editable_geometry": base["geometry"]["editable_geometry"],
        },
    )

    assert revision["kind"] == "geometry"
    assert revision["value"]["geometry"]["arrival_approach_routes"][0]["route_id"] == (
        "gate-to-sleeping"
    )
    assert plans.geometry("project", base_id) == base_snapshot
    approved_revision = plans.approve(
        "project",
        "geometry-with-route",
        {
            "preview_id": revision["preview_id"],
            "asset_id": asset_id,
            "approved_hash": revision["value_hash"],
            "approved": True,
        },
    )
    assert approved_revision["geometry"]["geometry"]["arrival_approach_routes"][0][
        "source_ids"
    ] == ["SRC-SITE-ROUTE-SURVEY"]
    assert plans.geometry("project", base_id) == base_snapshot

    outside = dict(geometry)
    outside["arrival_approach_routes"] = [
        {
            **geometry["arrival_approach_routes"][0],
            "points_m": [{"x": -0.1, "y": 5.0}, {"x": 2.0, "y": 5.0}],
        }
    ]
    with pytest.raises(StorageError, match="plan_geometry_revision_invalid"):
        plans.preview(
            "project",
            asset_id,
            {
                "operation": "geometry_revision",
                "base_geometry_id": base_id,
                "base_geometry_hash": base["geometry_hash"],
                "geometry": outside,
                "editable_geometry": base["geometry"]["editable_geometry"],
            },
        )
    assert plans.geometry("project", base_id) == base_snapshot
