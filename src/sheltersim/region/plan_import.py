"""Import operator-provided shelter records and explicitly traced floor plans."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import (
    FacilityGeometry,
    FacilityServicePoint,
    PhysicalRoute,
    PhysicalZone,
    Point2D,
    ServiceAccessConditions,
)
from sheltersim.experiments.scenario import FacilitySpec, ZoneEdgeSpec, ZoneSpec
from sheltersim.region.facility import (
    EditableFacilityGeometry,
    FacilityEquipmentFootprint,
    FacilityObstacle,
    FacilityOpening,
    FacilityWall,
    RouteOpeningUse,
    validate_editable_routes,
    validate_facility_physical_layout,
    with_legacy_service_points,
)
from sheltersim.region.models import (
    ACCESSIBLE_ROUTE_MIN_WIDTH_M,
    DataSource,
    MunicipalPlanObservation,
    RegionModel,
    RegionResearchResult,
    ShelterCandidate,
)

MAX_PLAN_BYTES = 15 * 1024 * 1024
MAX_CATALOG_BYTES = 8 * 1024 * 1024


class PlanAsset(RegionModel):
    source_id: str
    file_name: str = Field(min_length=1, max_length=240)
    media_type: Literal[
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/geo+json",
        "application/json",
    ]
    byte_count: int = Field(gt=0, le=MAX_PLAN_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_at: datetime
    source_url: str | None = None
    status: Literal["metadata_only", "manual_trace_required", "vector_data"]
    notice: str
    retention_state: Literal["transient_request_only", "external_asset_store_required"] = (
        "external_asset_store_required"
    )
    rights_status: Literal[
        "unknown", "public_license_review_required", "operator_permission_asserted"
    ] = "unknown"
    use_restriction_note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_upload_metadata(self) -> PlanAsset:
        if "/" in self.file_name or "\\" in self.file_name or "\x00" in self.file_name:
            raise ValueError("plan file name must not contain path separators or NUL")
        if self.source_url is not None and not self.source_url.startswith("https://"):
            raise ValueError("plan source_url must use HTTPS")
        if self.imported_at.tzinfo is None:
            raise ValueError("plan import timestamp must include a timezone")
        return self


class ShelterCSVMapping(RegionModel):
    name_column: str
    address_column: str | None = None
    latitude_column: str | None = None
    longitude_column: str | None = None
    capacity_column: str | None = None
    area_column: str | None = None
    hazards_column: str | None = None
    encoding: Literal["utf-8", "utf-8-sig", "cp932"] = "utf-8-sig"
    delimiter: Literal[",", "\t"] = ","


class MunicipalPlanCSVMapping(RegionModel):
    """Explicit columns for numeric plan facts; mapping does not verify applicability."""

    metric_column: str = Field(min_length=1, max_length=120)
    value_column: str = Field(min_length=1, max_length=120)
    unit_column: str = Field(min_length=1, max_length=120)
    geographic_scope_column: str = Field(min_length=1, max_length=120)
    period_column: str = Field(min_length=1, max_length=120)
    applicability_column: str = Field(min_length=1, max_length=120)
    municipality_code_column: str | None = Field(default=None, max_length=120)
    encoding: Literal["utf-8", "utf-8-sig", "cp932"] = "utf-8-sig"
    delimiter: Literal[",", "\t"] = ","


class ManualPlanZone(RegionModel):
    zone_id: str
    name: str
    pixel_polygon: tuple[tuple[float, float], ...] = Field(min_length=3, max_length=128)
    usable_fraction: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)


class ManualPlanFacility(RegionModel):
    id: str
    kind: str
    zone_id: str
    capacity: int = Field(ge=0)
    working_units: int | None = Field(default=None, ge=0)
    pixel_x: float = Field(ge=0, allow_inf_nan=False)
    pixel_y: float = Field(ge=0, allow_inf_nan=False)


class ManualPlanRoute(RegionModel):
    edge_id: str
    from_zone_id: str
    to_zone_id: str
    points_px: tuple[tuple[float, float], ...] = Field(min_length=2, max_length=128)
    width_m: float = Field(gt=0, le=20, allow_inf_nan=False)
    accessible: bool = True


class ManualPlanWall(RegionModel):
    wall_id: str
    start_px: tuple[float, float]
    end_px: tuple[float, float]


class ManualPlanOpening(RegionModel):
    opening_id: str
    wall_id: str
    start_px: tuple[float, float]
    end_px: tuple[float, float]
    width_m: float = Field(gt=0, le=20, allow_inf_nan=False)
    opening_kind: str = "doorway"


class ManualPlanObstacle(RegionModel):
    obstacle_id: str
    pixel_polygon: tuple[tuple[float, float], ...] = Field(min_length=3, max_length=128)


class ManualPlanEquipmentFootprint(RegionModel):
    facility_id: str
    pixel_polygon: tuple[tuple[float, float], ...] = Field(min_length=4, max_length=4)


class ManualRouteOpening(RegionModel):
    route_id: str
    opening_id: str


class ManualPlanTrace(RegionModel):
    raster_pixel_width: int = Field(gt=0, le=30_000)
    raster_pixel_height: int = Field(gt=0, le=30_000)
    source_page_index: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    plan_pixel_left: int = Field(default=0, ge=0, le=30_000)
    plan_pixel_top: int = Field(default=0, ge=0, le=30_000)
    plan_pixel_width: int | None = Field(default=None, gt=0, le=30_000)
    plan_pixel_height: int | None = Field(default=None, gt=0, le=30_000)
    building_width_m: float = Field(gt=0, le=2_000, allow_inf_nan=False)
    building_height_m: float = Field(gt=0, le=2_000, allow_inf_nan=False)
    zones: tuple[ManualPlanZone, ...] = Field(min_length=1, max_length=100)
    facilities: tuple[ManualPlanFacility, ...] = Field(max_length=500)
    routes: tuple[ManualPlanRoute, ...] = Field(max_length=500)
    walls: tuple[ManualPlanWall, ...] = Field(default=(), max_length=1_000)
    openings: tuple[ManualPlanOpening, ...] = Field(default=(), max_length=500)
    obstacles: tuple[ManualPlanObstacle, ...] = Field(default=(), max_length=500)
    equipment_footprints: tuple[ManualPlanEquipmentFootprint, ...] = Field(
        default=(), max_length=500
    )
    route_openings: tuple[ManualRouteOpening, ...] = Field(default=(), max_length=500)
    service_points: tuple[FacilityServicePoint, ...] = Field(
        default=(),
        max_length=10_000,
        description=(
            "Authored service points use FacilityServicePoint.point_m in calibrated building "
            "metres."
        ),
    )
    extraction_proposal_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_feature_ids: tuple[str, ...] = ()
    operator_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def unique_ids(self) -> ManualPlanTrace:
        for field_name, values in (
            ("zone", [item.zone_id for item in self.zones]),
            ("facility", [item.id for item in self.facilities]),
            ("route", [item.edge_id for item in self.routes]),
            ("wall", [item.wall_id for item in self.walls]),
            ("opening", [item.opening_id for item in self.openings]),
            ("obstacle", [item.obstacle_id for item in self.obstacles]),
            ("equipment footprint", [item.facility_id for item in self.equipment_footprints]),
            ("service point", [item.id for item in self.service_points]),
            ("service queue", [item.queue_id for item in self.service_points]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {field_name} ID in manual plan trace")
        if (
            self.plan_pixel_left + (self.plan_pixel_width or self.raster_pixel_width)
            > self.raster_pixel_width
        ):
            raise ValueError("calibrated plan crop exceeds raster width")
        if (
            self.plan_pixel_top + (self.plan_pixel_height or self.raster_pixel_height)
            > self.raster_pixel_height
        ):
            raise ValueError("calibrated plan crop exceeds raster height")
        wall_ids = {item.wall_id for item in self.walls}
        if any(item.wall_id not in wall_ids for item in self.openings):
            raise ValueError("every traced opening must reference a traced wall")
        if any(
            item.opening_id not in {opening.opening_id for opening in self.openings}
            for item in self.route_openings
        ):
            raise ValueError("route openings must reference a traced opening")
        if any(
            item.route_id not in {route.edge_id for route in self.routes}
            for item in self.route_openings
        ):
            raise ValueError("route openings must reference a traced route")
        if any(
            item.facility_id not in {facility.id for facility in self.facilities}
            for item in self.equipment_footprints
        ):
            raise ValueError("equipment footprints must reference a traced facility")
        if any(
            item.facility_id not in {facility.id for facility in self.facilities}
            for item in self.service_points
        ):
            raise ValueError("service points must reference a traced facility")
        return self


class PlanFeatureProposal(RegionModel):
    """Untrusted PDF/image extraction output; it cannot enter a ScenarioSpec directly."""

    feature_id: str
    asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_index: int = Field(ge=0, le=10_000)
    feature_kind: Literal[
        "zone", "facility", "route", "wall", "opening", "obstacle", "service_point"
    ]
    geometry_kind: Literal["point", "line", "polygon"]
    coordinates_px: tuple[tuple[float, float], ...] = Field(min_length=1, max_length=128)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    extraction_method: Literal[
        "vision_model", "pdf_vector", "ocr", "operator_trace", "raster_heuristic"
    ]
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    extraction_source_id: str = Field(min_length=1, max_length=64)
    review_status: Literal["pending", "accepted", "rejected"] = "pending"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    corrected_coordinates_px: tuple[tuple[float, float], ...] | None = None
    corrected_attributes: dict[str, str | int | float | bool | None] | None = None

    @model_validator(mode="after")
    def shape_matches_feature(self) -> PlanFeatureProposal:
        expected = {
            "zone": "polygon",
            "facility": "point",
            "route": "line",
            "wall": "line",
            "opening": "line",
            "obstacle": "polygon",
            "service_point": "point",
        }[self.feature_kind]
        if self.geometry_kind != expected:
            raise ValueError("plan extraction feature kind and geometry shape do not match")
        if self.geometry_kind == "point" and len(self.coordinates_px) != 1:
            raise ValueError("point extraction features require exactly one coordinate")
        if self.geometry_kind == "line" and len(self.coordinates_px) < 2:
            raise ValueError("line extraction features require at least two coordinates")
        if self.geometry_kind == "polygon" and len(self.coordinates_px) < 3:
            raise ValueError("polygon extraction features require at least three coordinates")
        for point in self.coordinates_px:
            if len(point) != 2 or any(not math.isfinite(value) or value < 0 for value in point):
                raise ValueError(
                    "extracted pixel coordinates must be finite non-negative x/y pairs"
                )
        if len(self.attributes) > 40 or any(
            not key
            or len(key) > 80
            or (isinstance(value, float) and not math.isfinite(value))
            or (isinstance(value, str) and len(value) > 240)
            for key, value in self.attributes.items()
        ):
            raise ValueError("extraction feature attributes must be bounded finite JSON scalars")
        if self.corrected_coordinates_px is not None and any(
            len(point) != 2 or any(not math.isfinite(value) or value < 0 for value in point)
            for point in self.corrected_coordinates_px
        ):
            raise ValueError(
                "human corrected pixel coordinates must be finite non-negative x/y pairs"
            )
        if self.corrected_attributes is not None and (
            len(self.corrected_attributes) > 40
            or any(
                not key
                or len(key) > 80
                or (isinstance(value, float) and not math.isfinite(value))
                or (isinstance(value, str) and len(value) > 240)
                for key, value in self.corrected_attributes.items()
            )
        ):
            raise ValueError(
                "human-corrected extraction attributes must be bounded finite JSON scalars"
            )
        if self.review_status == "pending" and (self.reviewed_by or self.reviewed_at):
            raise ValueError("pending extraction features cannot have a human reviewer")
        if self.review_status != "pending" and (not self.reviewed_by or self.reviewed_at is None):
            raise ValueError("human-reviewed features require reviewer identity and time")
        if self.reviewed_at is not None and self.reviewed_at.tzinfo is None:
            raise ValueError("plan extraction review timestamp must include a timezone")
        return self


class PlanExtractionProposal(RegionModel):
    schema_version: Literal[1] = 1
    asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_method: Literal[
        "vision_model", "pdf_vector", "ocr", "operator_trace", "raster_heuristic"
    ]
    features: tuple[PlanFeatureProposal, ...] = Field(max_length=2_000)
    extraction_source_id: str = Field(min_length=1, max_length=64)
    created_at: datetime
    raster_size_px: tuple[int, int] | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def features_reference_one_asset(self) -> PlanExtractionProposal:
        if any(item.asset_sha256 != self.asset_sha256 for item in self.features):
            raise ValueError("plan extraction features must reference the same uploaded asset hash")
        if any(item.extraction_method != self.extraction_method for item in self.features):
            raise ValueError("plan extraction feature method must match the proposal method")
        if any(item.extraction_source_id != self.extraction_source_id for item in self.features):
            raise ValueError(
                "plan extraction features must reference the proposal's extraction source"
            )
        if len({item.feature_id for item in self.features}) != len(self.features):
            raise ValueError("plan extraction feature IDs must be unique")
        if self.raster_size_px is not None:
            width, height = self.raster_size_px
            if width <= 0 or height <= 0:
                raise ValueError("raster page dimensions must be positive")
            for feature in self.features:
                points = feature.coordinates_px + (feature.corrected_coordinates_px or ())
                if any(x > width or y > height for x, y in points):
                    raise ValueError(
                        "extracted or corrected coordinates lie outside the declared page"
                    )
        if self.created_at.tzinfo is None:
            raise ValueError("plan extraction proposal timestamp must include a timezone")
        return self


class PlanFeatureReview(RegionModel):
    feature_id: str
    decision: Literal["accepted", "rejected"]
    reviewer_id: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=500)
    corrected_coordinates_px: tuple[tuple[float, float], ...] | None = Field(
        default=None, max_length=128
    )
    corrected_attributes: dict[str, str | int | float | bool | None] | None = None
    reviewed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> PlanFeatureReview:
        if self.reviewed_at.tzinfo is None:
            raise ValueError("human review timestamp must include a timezone")
        if self.corrected_attributes is not None and (
            len(self.corrected_attributes) > 40
            or any(
                not key
                or len(key) > 80
                or (isinstance(value, float) and not math.isfinite(value))
                or (isinstance(value, str) and len(value) > 240)
                for key, value in self.corrected_attributes.items()
            )
        ):
            raise ValueError("human review attributes must be bounded finite JSON scalars")
        return self


class PlanSourceProvenance(RegionModel):
    """Versioned source/calibration retained with an approved plan geometry.

    The six coefficients map source pixels to local metres as
    ``x_m=a*x_px+c*y_px+e`` and ``y_m=b*x_px+d*y_px+f``. On a later edited
    revision the source calibration remains provenance, but is explicitly scoped
    to the initial trace rather than being claimed as a transform for changed shapes.
    """

    schema_version: Literal[1] = 1
    source_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_page_index: int | None = Field(default=None, strict=True, ge=0, le=10_000)
    raster_asset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raster_size_px: tuple[int, int] | None = None
    pixel_to_local_m_transform: tuple[float, float, float, float, float, float] | None = None
    transform_scope: Literal["current_geometry", "source_trace_only"] = "current_geometry"

    @model_validator(mode="before")
    @classmethod
    def reject_coerced_source_dimensions(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        dimensions = value.get("raster_size_px")
        if dimensions is not None and (
            not isinstance(dimensions, (tuple, list))
            or any(type(item) is not int for item in dimensions)
        ):
            raise ValueError("source raster dimensions must be integer pixel counts")
        transform = value.get("pixel_to_local_m_transform")
        if transform is not None and (
            not isinstance(transform, (tuple, list))
            or any(type(item) not in (int, float) for item in transform)
        ):
            raise ValueError("pixel-to-metre coefficients must be numeric values")
        return value

    @model_validator(mode="after")
    def calibration_is_consistent(self) -> PlanSourceProvenance:
        if (self.raster_size_px is None) != (self.pixel_to_local_m_transform is None):
            raise ValueError(
                "raster dimensions and pixel-to-metre transform must be supplied together"
            )
        if self.raster_size_px is not None and any(
            type(value) is not int or not 1 <= value <= 30_000 for value in self.raster_size_px
        ):
            raise ValueError("source raster dimensions must be positive bounded pixel counts")
        if self.pixel_to_local_m_transform is not None:
            a, b, c, d, e, f = self.pixel_to_local_m_transform
            if (
                any(not math.isfinite(value) for value in (a, b, c, d, e, f))
                or a <= 0
                or d <= 0
                or b != 0
                or c != 0
            ):
                raise ValueError(
                    "pixel-to-metre transform must be finite, axis-aligned, and positively scaled"
                )
        return self


class ImportedPlanGeometry(RegionModel):
    asset: PlanAsset
    zones: tuple[ZoneSpec, ...]
    zone_edges: tuple[ZoneEdgeSpec, ...]
    facilities: tuple[FacilitySpec, ...]
    geometry: FacilityGeometry
    editable_geometry: EditableFacilityGeometry | None = None
    extraction_proposal: PlanExtractionProposal | None = None
    source_provenance: PlanSourceProvenance | None = None
    notes: tuple[str, ...]

    @model_validator(mode="after")
    def provenance_matches_source_asset(self) -> ImportedPlanGeometry:
        provenance = self.source_provenance
        if provenance is not None:
            if provenance.source_asset_sha256 != self.asset.sha256:
                raise ValueError(
                    "geometry coordinate provenance must reference its exact source Asset"
                )
            if (
                provenance.source_page_index is not None
                and self.asset.media_type != "application/pdf"
            ):
                if provenance.source_page_index != 0:
                    raise ValueError("raster image geometry uses source page index zero")
            if provenance.raster_asset_sha256 is not None and provenance.raster_size_px is None:
                raise ValueError("a derived raster hash requires its declared pixel dimensions")
            if self.extraction_proposal is not None:
                pages = {
                    item.page_index for item in accepted_plan_features(self.extraction_proposal)
                }
                if pages and provenance.source_page_index not in pages:
                    raise ValueError(
                        "geometry page provenance differs from its reviewed extraction page"
                    )
        return self


def create_plan_extraction_proposal(
    asset: PlanAsset,
    *,
    extraction_method: Literal[
        "vision_model", "pdf_vector", "ocr", "operator_trace", "raster_heuristic"
    ],
    extraction_source_id: str,
    features: tuple[PlanFeatureProposal, ...],
    raster_size_px: tuple[int, int] | None = None,
    notes: tuple[str, ...] = (),
) -> PlanExtractionProposal:
    """Bind externally extracted raster/vector suggestions to the exact uploaded bytes."""
    if asset.status != "manual_trace_required" and asset.media_type not in {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
    }:
        raise ValueError("reviewable extraction proposals require a PDF or image plan asset")
    if any(item.asset_sha256 != asset.sha256 for item in features):
        raise ValueError("every extraction suggestion must match the uploaded asset hash")
    if any(item.review_status != "pending" for item in features):
        raise ValueError("new extraction suggestions must remain pending human review")
    return PlanExtractionProposal(
        asset_sha256=asset.sha256,
        extraction_method=extraction_method,
        features=features,
        extraction_source_id=extraction_source_id,
        created_at=datetime.now(UTC),
        raster_size_px=raster_size_px,
        notes=notes,
    )


def review_plan_extraction(
    proposal: PlanExtractionProposal,
    reviews: tuple[PlanFeatureReview, ...],
) -> PlanExtractionProposal:
    """Record human decisions and pixel corrections separately from extractor confidence."""
    if len({item.feature_id for item in reviews}) != len(reviews):
        raise ValueError("each feature can receive at most one review in a review update")
    review_by_id = {item.feature_id: item for item in reviews}
    feature_ids = {item.feature_id for item in proposal.features}
    if set(review_by_id) - feature_ids:
        raise ValueError("plan review references an unknown extracted feature")
    updated = []
    for feature in proposal.features:
        review = review_by_id.get(feature.feature_id)
        if review is None:
            updated.append(feature)
            continue
        if feature.review_status != "pending":
            raise ValueError(
                "a reviewed plan feature cannot be reviewed again without creating a new "
                "extraction proposal"
            )
        corrected = review.corrected_coordinates_px
        if corrected is not None and proposal.raster_size_px is not None:
            width, height = proposal.raster_size_px
            if any(x > width or y > height for x, y in corrected):
                raise ValueError("human correction coordinates lie outside the declared page")
        if corrected is not None:
            expected = {"point": 1}.get(feature.geometry_kind)
            if expected is not None and len(corrected) != expected:
                raise ValueError("human correction coordinates do not match the feature geometry")
            if feature.geometry_kind == "line" and len(corrected) < 2:
                raise ValueError("human corrected line requires at least two coordinates")
            if feature.geometry_kind == "polygon" and len(corrected) < 3:
                raise ValueError("human corrected polygon requires at least three coordinates")
        updated.append(
            PlanFeatureProposal.model_validate(
                feature.model_dump(mode="python")
                | {
                    "review_status": review.decision,
                    "reviewed_by": review.reviewer_id,
                    "reviewed_at": review.reviewed_at,
                    "review_note": review.note,
                    "corrected_coordinates_px": corrected,
                    "corrected_attributes": review.corrected_attributes,
                }
            )
        )
    return PlanExtractionProposal.model_validate(
        proposal.model_dump(mode="python") | {"features": tuple(updated)}
    )


def accepted_plan_features(proposal: PlanExtractionProposal) -> tuple[PlanFeatureProposal, ...]:
    """Return only human-accepted suggestions for downstream explicit scenario mapping."""
    if any(feature.review_status == "pending" for feature in proposal.features):
        raise ValueError("every extracted plan feature must be accepted or rejected before mapping")
    return tuple(item for item in proposal.features if item.review_status == "accepted")


def manual_trace_from_reviewed_features(
    proposal: PlanExtractionProposal,
    *,
    building_width_m: float,
    building_height_m: float,
    operator_id: str,
    plan_pixel_left: int = 0,
    plan_pixel_top: int = 0,
    plan_pixel_width: int | None = None,
    plan_pixel_height: int | None = None,
) -> ManualPlanTrace:
    """Create a calibrated trace only from reviewed features and explicit feature mappings."""
    accepted = accepted_plan_features(proposal)
    if proposal.raster_size_px is None:
        raise ValueError("extraction-assisted tracing requires declared raster page dimensions")
    page_ids = {item.page_index for item in accepted}
    if len(page_ids) > 1:
        raise ValueError("one trace can map features from only one reviewed page")
    if not accepted:
        raise ValueError("extraction proposal has no human-accepted plan features")
    if not operator_id.strip():
        raise ValueError("a human operator identity is required to map reviewed features")

    def coords(feature: PlanFeatureProposal) -> tuple[tuple[float, float], ...]:
        return feature.corrected_coordinates_px or feature.coordinates_px

    def attributes(feature: PlanFeatureProposal) -> dict[str, Any]:
        return feature.attributes | (feature.corrected_attributes or {})

    def required(
        feature: PlanFeatureProposal, name: str, value_type: type | tuple[type, ...]
    ) -> Any:
        value = attributes(feature).get(name)
        if value_type is int:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"reviewed {feature.feature_kind} feature {feature.feature_id!r} "
                    f"requires integer {name!r}"
                )
        elif value_type in {float, (int, float)}:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise ValueError(
                    f"reviewed {feature.feature_kind} feature {feature.feature_id!r} "
                    f"requires numeric {name!r}"
                )
        elif not isinstance(value, value_type):
            raise ValueError(
                f"reviewed {feature.feature_kind} feature {feature.feature_id!r} requires {name!r}"
            )
        return value

    zones: list[ManualPlanZone] = []
    facilities: list[ManualPlanFacility] = []
    routes: list[ManualPlanRoute] = []
    walls: list[ManualPlanWall] = []
    openings: list[ManualPlanOpening] = []
    obstacles: list[ManualPlanObstacle] = []
    service_points: list[FacilityServicePoint] = []
    route_openings: list[ManualRouteOpening] = []
    for feature in accepted:
        points = coords(feature)
        if feature.feature_kind == "zone":
            zone_attributes = attributes(feature)
            zones.append(
                ManualPlanZone(
                    zone_id=required(feature, "zone_id", str),
                    name=required(feature, "name", str),
                    pixel_polygon=points,
                    usable_fraction=(
                        float(required(feature, "usable_fraction", float))
                        if "usable_fraction" in zone_attributes
                        else 0.9
                    ),
                )
            )
        elif feature.feature_kind == "facility":
            facilities.append(
                ManualPlanFacility(
                    id=required(feature, "id", str),
                    kind=required(feature, "kind", str),
                    zone_id=required(feature, "zone_id", str),
                    capacity=required(feature, "capacity", int),
                    working_units=(
                        required(feature, "working_units", int)
                        if attributes(feature).get("working_units") is not None
                        else None
                    ),
                    pixel_x=points[0][0],
                    pixel_y=points[0][1],
                )
            )
        elif feature.feature_kind == "route":
            route_id = required(feature, "edge_id", str)
            routes.append(
                ManualPlanRoute(
                    edge_id=route_id,
                    from_zone_id=required(feature, "from_zone_id", str),
                    to_zone_id=required(feature, "to_zone_id", str),
                    points_px=points,
                    width_m=float(required(feature, "width_m", (int, float))),
                    accessible=required(feature, "accessible", bool),
                )
            )
            opening_id = attributes(feature).get("opening_id")
            if opening_id is not None:
                route_openings.append(
                    ManualRouteOpening(
                        route_id=route_id,
                        opening_id=required(feature, "opening_id", str),
                    )
                )
        elif feature.feature_kind == "wall":
            if len(points) != 2:
                raise ValueError("reviewed wall features must have exactly two endpoints")
            walls.append(
                ManualPlanWall(
                    wall_id=required(feature, "wall_id", str),
                    start_px=points[0],
                    end_px=points[1],
                )
            )
        elif feature.feature_kind == "opening":
            if len(points) != 2:
                raise ValueError("reviewed opening features must have exactly two endpoints")
            openings.append(
                ManualPlanOpening(
                    opening_id=required(feature, "opening_id", str),
                    wall_id=required(feature, "wall_id", str),
                    start_px=points[0],
                    end_px=points[1],
                    width_m=float(required(feature, "width_m", (int, float))),
                    opening_kind=(
                        required(feature, "opening_kind", str)
                        if attributes(feature).get("opening_kind") is not None
                        else "doorway"
                    ),
                )
            )
        elif feature.feature_kind == "obstacle":
            obstacles.append(
                ManualPlanObstacle(
                    obstacle_id=required(feature, "obstacle_id", str),
                    pixel_polygon=points,
                )
            )
        elif feature.feature_kind == "service_point":
            access = ServiceAccessConditions.model_validate(
                {
                    "accessibility": attributes(feature).get("accessibility", "unknown"),
                    "required_access_tags": attributes(feature).get("required_access_tags", ()),
                    "required_staff_skills": attributes(feature).get("required_staff_skills", ()),
                }
            )
            service_points.append(
                FacilityServicePoint(
                    id=required(feature, "id", str),
                    facility_id=required(feature, "facility_id", str),
                    point_m=Point2D(x=points[0][0], y=points[0][1]),
                    access=access,
                    service_capacity=required(feature, "service_capacity", int),
                    queue_id=required(feature, "queue_id", str),
                )
            )
    raster_width, raster_height = proposal.raster_size_px
    crop_width = plan_pixel_width or raster_width
    crop_height = plan_pixel_height or raster_height

    def calibrated_point(pixel_point: Point2D) -> Point2D:
        if (
            pixel_point.x < plan_pixel_left
            or pixel_point.y < plan_pixel_top
            or pixel_point.x > plan_pixel_left + crop_width
            or pixel_point.y > plan_pixel_top + crop_height
        ):
            raise ValueError("reviewed service point lies outside the calibrated plan crop")
        return Point2D(
            x=(pixel_point.x - plan_pixel_left) * building_width_m / crop_width,
            y=(pixel_point.y - plan_pixel_top) * building_height_m / crop_height,
        )

    calibrated_service_points = tuple(
        item.model_copy(update={"point_m": calibrated_point(item.point_m)})
        for item in service_points
    )
    return ManualPlanTrace(
        raster_pixel_width=raster_width,
        raster_pixel_height=raster_height,
        plan_pixel_left=plan_pixel_left,
        plan_pixel_top=plan_pixel_top,
        plan_pixel_width=plan_pixel_width,
        plan_pixel_height=plan_pixel_height,
        building_width_m=building_width_m,
        building_height_m=building_height_m,
        zones=tuple(zones),
        facilities=tuple(facilities),
        routes=tuple(routes),
        walls=tuple(walls),
        openings=tuple(openings),
        obstacles=tuple(obstacles),
        route_openings=tuple(route_openings),
        service_points=calibrated_service_points,
        extraction_proposal_sha256=proposal.asset_sha256,
        reviewed_feature_ids=tuple(item.feature_id for item in accepted),
        source_page_index=next(iter(page_ids)),
        operator_id=operator_id,
    )


def inspect_plan_upload(
    *,
    file_name: str,
    media_type: str,
    content: bytes,
    source_url: str | None = None,
    rights_status: Literal[
        "unknown", "public_license_review_required", "operator_permission_asserted"
    ] = "unknown",
    use_restriction_note: str | None = None,
) -> PlanAsset:
    """Record evidence for a user-selected plan; raster bytes remain in the Asset store."""
    normalized_type = media_type.split(";", 1)[0].strip().lower()
    if normalized_type not in {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/geo+json",
        "application/json",
    }:
        raise ValueError("plan file must be PDF, PNG, JPEG, WebP, GeoJSON, or JSON")
    if not content or len(content) > MAX_PLAN_BYTES:
        raise ValueError("plan file must be non-empty and no larger than 15 MiB")
    if source_url is not None and not source_url.startswith("https://"):
        raise ValueError("plan source_url must use HTTPS")
    if "/" in file_name or "\\" in file_name or "\x00" in file_name:
        raise ValueError("plan file name must not contain path separators or NUL")
    digest = hashlib.sha256(content).hexdigest()
    raster = normalized_type in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    return PlanAsset(
        source_id=f"SRC-PLAN-{digest[:24]}",
        file_name=file_name,
        media_type=normalized_type,
        byte_count=len(content),
        sha256=digest,
        imported_at=datetime.now(UTC),
        source_url=source_url,
        status="manual_trace_required" if raster else "vector_data",
        notice=(
            (
                "The original PDF/image is retained in the external Asset store. Candidate "
                "extraction is heuristic and untrusted; an operator must review, calibrate, and "
                "trace zones, fixtures, and routes."
            )
            if raster
            else (
                "Vector data must be explicitly mapped to the canonical scenario zones, "
                "facilities, and routes."
            )
        ),
        retention_state="external_asset_store_required",
        rights_status=rights_status,
        use_restriction_note=use_restriction_note,
    )


def import_municipal_shelter_csv(
    content: bytes,
    *,
    title: str,
    publisher: str,
    source_url: str,
    mapping: ShelterCSVMapping,
    published_on: date | None = None,
) -> tuple[DataSource, tuple[ShelterCandidate, ...]]:
    """Parse only user-provided source files with an explicit column mapping."""
    if not content or len(content) > MAX_CATALOG_BYTES:
        raise ValueError("shelter catalog must be non-empty and no larger than 8 MiB")
    if not source_url.startswith("https://"):
        raise ValueError("municipal catalog source_url must use HTTPS")
    text = content.decode(mapping.encoding)
    reader = csv.DictReader(io.StringIO(text), delimiter=mapping.delimiter)
    if not reader.fieldnames or mapping.name_column not in reader.fieldnames:
        raise ValueError("mapped shelter name column is not present in the uploaded file")
    optional = (
        mapping.address_column,
        mapping.latitude_column,
        mapping.longitude_column,
        mapping.capacity_column,
        mapping.area_column,
        mapping.hazards_column,
    )
    missing = [column for column in optional if column and column not in reader.fieldnames]
    if missing:
        raise ValueError(f"mapped CSV columns are missing: {', '.join(missing)}")
    digest = hashlib.sha256(content).hexdigest()
    source_id = f"SRC-MUNICIPAL-SHELTERS-{digest[:16]}"
    source = DataSource(
        id=source_id,
        title=title,
        publisher=publisher,
        url=source_url,
        source_kind="municipal",
        accessed_at=datetime.now(UTC),
        published_on=published_on,
        geographic_scope="operator-supplied dataset; geographic scope not inferred",
        freshness="unknown",
        license_note=(
            "Confirm publication date, terms, coverage, and latest revisions with the publisher."
        ),
    )
    records: list[ShelterCandidate] = []
    for index, row in enumerate(reader, start=1):
        name = (row.get(mapping.name_column) or "").strip()
        if not name:
            continue
        records.append(
            ShelterCandidate(
                id=f"SHELTER-{digest[:8]}-{index:05d}",
                name=name,
                address=_cell(row, mapping.address_column),
                latitude=_float_cell(row, mapping.latitude_column),
                longitude=_float_cell(row, mapping.longitude_column),
                capacity=_int_cell(row, mapping.capacity_column),
                area_m2=_float_cell(row, mapping.area_column),
                supported_hazards=tuple(
                    part.strip()
                    for part in (_cell(row, mapping.hazards_column) or "")
                    .replace("、", ",")
                    .split(",")
                    if part.strip()
                ),
                source_id=source_id,
                source_kind="municipal",
                freshness="unknown",
                verified_by_user=False,
                notes=(
                    "Imported from the operator-supplied file; confirm current status with "
                    "the municipality.",
                ),
            )
        )
    return source, tuple(records)


def attach_shelter_catalog(
    research: RegionResearchResult,
    source: DataSource,
    shelters: tuple[ShelterCandidate, ...],
) -> RegionResearchResult:
    """Attach an imported official file to research so the operator can select a record."""
    if source.source_kind not in {"municipal", "official"}:
        raise ValueError("shelter catalogs must come from an official or municipal publisher")
    if any(item.source_id != source.id for item in shelters):
        raise ValueError("every imported shelter candidate must point to the supplied file source")
    existing_ids = {item.id for item in research.shelters}
    if existing_ids & {item.id for item in shelters}:
        raise ValueError("imported shelter IDs must not collide with existing research candidates")
    sources = {item.id: item for item in research.sources}
    sources[source.id] = source
    return RegionResearchResult.model_validate(
        research.model_dump(mode="python")
        | {
            "sources": tuple(sources[key] for key in sorted(sources)),
            "shelters": (*research.shelters, *shelters),
        }
    )


def import_municipal_plan_csv(
    content: bytes,
    *,
    title: str,
    publisher: str,
    source_url: str,
    mapping: MunicipalPlanCSVMapping,
    published_on: date | None = None,
) -> tuple[DataSource, tuple[MunicipalPlanObservation, ...]]:
    """Map operator-supplied plan facts without claiming official verification."""
    if not content or len(content) > MAX_CATALOG_BYTES:
        raise ValueError("municipal plan table must be non-empty and no larger than 8 MiB")
    if not source_url.startswith("https://"):
        raise ValueError("municipal plan citation must use HTTPS")
    reader = csv.DictReader(
        io.StringIO(content.decode(mapping.encoding)), delimiter=mapping.delimiter
    )
    required_columns = (
        mapping.metric_column,
        mapping.value_column,
        mapping.unit_column,
        mapping.geographic_scope_column,
        mapping.period_column,
        mapping.applicability_column,
    )
    columns = (
        *required_columns,
        *((mapping.municipality_code_column,) if mapping.municipality_code_column else ()),
    )
    if not reader.fieldnames or any(column not in reader.fieldnames for column in columns):
        raise ValueError("every mapped municipal plan column must exist in the uploaded table")
    if len(columns) != len(set(columns)):
        raise ValueError("municipal plan table fields must map to distinct source columns")
    digest = hashlib.sha256(content).hexdigest()
    source_id = f"SRC-MUNICIPAL-PLAN-{digest[:16]}"
    source = DataSource(
        id=source_id,
        title=title,
        publisher=publisher,
        url=source_url,
        source_kind="operator",
        accessed_at=datetime.now(UTC),
        published_on=published_on,
        geographic_scope="operator supplied; each row preserves its mapped scope",
        freshness="unknown",
        content_sha256=digest,
        license_note=(
            "Operator-mapped values are not independently verified; confirm the cited plan, "
            "terms, period, and geographic applicability before use."
        ),
    )
    observations: list[MunicipalPlanObservation] = []
    for row_number, row in enumerate(reader, start=1):
        if row_number > 50_000:
            raise ValueError("municipal plan table exceeds the 50,000-row review limit")
        metric = _cell(row, mapping.metric_column)
        if not metric:
            continue
        raw_value = _cell(row, mapping.value_column)
        try:
            value = float(raw_value.replace(",", "")) if raw_value else math.nan
        except ValueError as exc:
            raise ValueError(f"municipal plan row {row_number} has a non-numeric value") from exc
        if not math.isfinite(value):
            raise ValueError(f"municipal plan row {row_number} has a missing or non-finite value")
        unit = _cell(row, mapping.unit_column)
        geographic_scope = _cell(row, mapping.geographic_scope_column)
        period = _cell(row, mapping.period_column)
        applicability = _cell(row, mapping.applicability_column)
        if not all((unit, geographic_scope, period, applicability)):
            raise ValueError(
                f"municipal plan row {row_number} must include unit, scope, period, and "
                "applicability"
            )
        observations.append(
            MunicipalPlanObservation(
                row_number=row_number,
                metric=metric,
                value=value,
                unit=unit,
                geographic_scope=geographic_scope,
                period=period,
                applicability=applicability,
                municipality_code=_cell(row, mapping.municipality_code_column),
                content_sha256=digest,
                source_id=source_id,
            )
        )
    return source, tuple(observations)


def attach_municipal_plan_data(
    research: RegionResearchResult,
    source: DataSource,
    observations: tuple[MunicipalPlanObservation, ...],
) -> RegionResearchResult:
    """Attach mapped plan facts while keeping their pending-review status visible."""
    if source.source_kind != "operator":
        raise ValueError("operator supplied municipal plan imports must retain operator provenance")
    if any(
        item.source_id != source.id or item.content_sha256 != source.content_sha256
        for item in observations
    ):
        raise ValueError(
            "municipal plan observations must reference the supplied source hash and ID"
        )
    selected_code = research.request.municipality_code
    if selected_code is not None and any(
        item.municipality_code is not None and item.municipality_code != selected_code
        for item in observations
    ):
        raise ValueError(
            "municipal plan rows include a municipality code different from the selected "
            "research request"
        )
    if any(item.source_id == source.id for item in research.municipal_plan_observations):
        raise ValueError("the same municipal plan source cannot be attached more than once")
    sources = {item.id: item for item in research.sources}
    sources[source.id] = source
    return RegionResearchResult.model_validate(
        research.model_dump(mode="python")
        | {
            "sources": tuple(sources[key] for key in sorted(sources)),
            "municipal_plan_observations": (*research.municipal_plan_observations, *observations),
            "warnings": (
                *research.warnings,
                "取り込んだ自治体計画値は入力者による列対応結果です。出典・単位・期間・地理的適用範囲を確認するまで実測・公式値として扱っていません。",
            ),
        }
    )


def trace_uploaded_plan(
    asset: PlanAsset,
    trace: ManualPlanTrace,
    *,
    reviewed_proposal: PlanExtractionProposal | None = None,
) -> ImportedPlanGeometry:
    """Convert an operator trace to meters using declared raster dimensions."""
    if asset.status != "manual_trace_required":
        raise ValueError("manual tracing is limited to uploaded PDF/image assets")
    if asset.media_type == "application/pdf" and trace.source_page_index is None:
        raise ValueError("PDF plan trace requires its zero-based source page index")
    if asset.media_type != "application/pdf" and trace.source_page_index not in (None, 0):
        raise ValueError("image plan trace must use source page index zero")
    if reviewed_proposal is not None:
        if reviewed_proposal.asset_sha256 != asset.sha256:
            raise ValueError(
                "human-reviewed extraction proposal does not match the uploaded plan hash"
            )
        accepted_ids = {item.feature_id for item in accepted_plan_features(reviewed_proposal)}
        if trace.extraction_proposal_sha256 != reviewed_proposal.asset_sha256:
            raise ValueError("manual trace is not bound to its reviewed extraction proposal")
        if set(trace.reviewed_feature_ids) != accepted_ids:
            raise ValueError(
                "manual trace must map every human-accepted extraction feature exactly once"
            )
        reviewed_pages = {item.page_index for item in accepted_plan_features(reviewed_proposal)}
        if reviewed_pages and trace.source_page_index not in reviewed_pages:
            raise ValueError("manual trace page must match its human-reviewed extraction page")
    elif trace.extraction_proposal_sha256 is not None:
        raise ValueError("manual trace references a missing extraction proposal")
    crop_width = trace.plan_pixel_width or trace.raster_pixel_width
    crop_height = trace.plan_pixel_height or trace.raster_pixel_height
    sx = trace.building_width_m / crop_width
    sy = trace.building_height_m / crop_height
    source_provenance = PlanSourceProvenance(
        source_asset_sha256=asset.sha256,
        source_page_index=trace.source_page_index,
        raster_size_px=(trace.raster_pixel_width, trace.raster_pixel_height),
        pixel_to_local_m_transform=(
            sx,
            0.0,
            0.0,
            sy,
            -trace.plan_pixel_left * sx,
            -trace.plan_pixel_top * sy,
        ),
    )

    def point(px: float, py: float) -> Point2D:
        right = trace.plan_pixel_left + crop_width
        bottom = trace.plan_pixel_top + crop_height
        if px < trace.plan_pixel_left or py < trace.plan_pixel_top or px > right or py > bottom:
            raise ValueError("trace point lies outside the calibrated plan crop")
        return Point2D(x=(px - trace.plan_pixel_left) * sx, y=(py - trace.plan_pixel_top) * sy)

    physical_zones: list[PhysicalZone] = []
    zones: list[ZoneSpec] = []
    for item in trace.zones:
        polygon = tuple(point(x, y) for x, y in item.pixel_polygon)
        area = _polygon_area(polygon)
        if area <= 0:
            raise ValueError(f"zone {item.zone_id!r} trace must enclose a positive area")
        physical_zones.append(
            PhysicalZone(
                zone_id=item.zone_id,
                polygon=polygon,
                usable_area_m2=area * item.usable_fraction,
                source_ids=(asset.source_id,),
            )
        )
        zones.append(ZoneSpec(id=item.zone_id, name=item.name, capacity=None))
    _validate_zone_topology(tuple(item.polygon for item in physical_zones))
    facility_specs = tuple(
        FacilitySpec(
            id=item.id,
            kind=item.kind,
            zone_id=item.zone_id,
            capacity=item.capacity,
            working_units=item.working_units,
        )
        for item in trace.facilities
    )
    facility_points: dict[str, Point2D] = {}
    for item in trace.facilities:
        facility_points[item.id] = point(item.pixel_x, item.pixel_y)
    routes = tuple(
        PhysicalRoute(
            edge_id=item.edge_id,
            points=tuple(point(x, y) for x, y in item.points_px),
            width_m=item.width_m,
            accessible=item.accessible,
        )
        for item in trace.routes
    )
    zone_ids = {zone.id for zone in zones}
    zone_edges = []
    for item in trace.routes:
        if item.from_zone_id not in zone_ids or item.to_zone_id not in zone_ids:
            raise ValueError(f"route {item.edge_id!r} references an unknown traced zone")
        if item.from_zone_id == item.to_zone_id:
            raise ValueError(f"route {item.edge_id!r} must connect different zones")
        route_points = tuple(point(x, y) for x, y in item.points_px)
        distance_m = sum(
            math.hypot(right.x - left.x, right.y - left.y)
            for left, right in zip(route_points, route_points[1:])
        )
        zone_edges.append(
            ZoneEdgeSpec(
                id=item.edge_id,
                from_zone_id=item.from_zone_id,
                to_zone_id=item.to_zone_id,
                travel_minutes=max(1, math.ceil(distance_m / 60.0)),
            )
        )
    geometry = FacilityGeometry(
        width_m=trace.building_width_m,
        height_m=trace.building_height_m,
        source_kind="inferred",
        source_ids=(asset.source_id,),
        zones=tuple(physical_zones),
        facility_points=facility_points,
        service_points=trace.service_points,
        routes=routes,
    )
    geometry = with_legacy_service_points(geometry, facility_specs)
    editable_geometry: EditableFacilityGeometry | None = None
    equipment_footprint_values = tuple(
        FacilityEquipmentFootprint(
            facility_id=item.facility_id,
            polygon=tuple(point(x, y) for x, y in item.pixel_polygon),
            area_m2=_polygon_area(tuple(point(x, y) for x, y in item.pixel_polygon)),
            source_ids=(asset.source_id,),
        )
        for item in trace.equipment_footprints
    )
    facility_spec_by_id = {item.id: item for item in facility_specs}
    for footprint in equipment_footprint_values:
        facility = facility_spec_by_id[footprint.facility_id]
        zone_polygon = next(
            (item.polygon for item in physical_zones if item.zone_id == facility.zone_id), None
        )
        if zone_polygon is None:
            raise ValueError(f"facility {facility.id!r} references an unknown traced zone")
        if not all(_point_in_or_on_polygon(vertex, zone_polygon) for vertex in footprint.polygon):
            raise ValueError(
                f"equipment footprint {footprint.facility_id!r} lies outside its assigned zone"
            )
        if not _point_in_or_on_polygon(facility_points[footprint.facility_id], footprint.polygon):
            raise ValueError(
                f"equipment footprint {footprint.facility_id!r} does not contain its facility point"
            )
    if (
        trace.walls
        or trace.openings
        or trace.obstacles
        or trace.route_openings
        or equipment_footprint_values
    ):
        wall_values = tuple(
            FacilityWall(
                wall_id=item.wall_id,
                start=point(*item.start_px),
                end=point(*item.end_px),
                source_ids=(asset.source_id,),
            )
            for item in trace.walls
        )
        opening_values = tuple(
            FacilityOpening(
                opening_id=item.opening_id,
                wall_id=item.wall_id,
                start=point(*item.start_px),
                end=point(*item.end_px),
                width_m=item.width_m,
                opening_kind=item.opening_kind,
                source_ids=(asset.source_id,),
            )
            for item in trace.openings
        )
        obstacle_values = tuple(
            FacilityObstacle(
                obstacle_id=item.obstacle_id,
                polygon=tuple(point(x, y) for x, y in item.pixel_polygon),
                source_ids=(asset.source_id,),
            )
            for item in trace.obstacles
        )
        route_opening_values = tuple(
            RouteOpeningUse(route_id=item.route_id, opening_id=item.opening_id)
            for item in trace.route_openings
        )
        editable_geometry = EditableFacilityGeometry(
            width_m=trace.building_width_m,
            height_m=trace.building_height_m,
            walls=wall_values,
            openings=opening_values,
            obstacles=obstacle_values,
            route_openings=route_opening_values,
            equipment_footprints=equipment_footprint_values,
        )
        opening_by_id = {item.opening_id: item for item in opening_values}
        route_by_id = {item.edge_id: item for item in routes}
        for use in route_opening_values:
            if route_by_id[use.route_id].width_m > opening_by_id[use.opening_id].width_m:
                raise ValueError(f"route {use.route_id!r} is wider than its mapped opening")
    validate_editable_routes(
        routes,
        editable_geometry
        or EditableFacilityGeometry(
            width_m=trace.building_width_m, height_m=trace.building_height_m
        ),
        physical_zones=tuple(physical_zones),
    )
    if {item.zone_id for item in physical_zones} != {item.id for item in zones}:
        raise ValueError("trace must define physical geometry for every scenario zone")
    if set(facility_points) != {item.id for item in facility_specs}:
        raise ValueError("trace must position every scenario facility")
    if {item.edge_id for item in routes} != {item.id for item in zone_edges}:
        raise ValueError("trace must define physical geometry for every scenario route")
    zone_polygon_by_id = {item.zone_id: item.polygon for item in physical_zones}
    for facility in facility_specs:
        if facility.zone_id not in zone_polygon_by_id or not _point_in_or_on_polygon(
            facility_points[facility.id], zone_polygon_by_id[facility.zone_id]
        ):
            raise ValueError(
                f"facility {facility.id!r} point lies outside its assigned traced zone"
            )
    route_by_id = {item.edge_id: item for item in routes}
    for edge in zone_edges:
        route = route_by_id[edge.id]
        if not _point_in_or_on_polygon(route.points[0], zone_polygon_by_id[edge.from_zone_id]):
            raise ValueError(f"route {edge.id!r} does not start inside its from-zone")
        if not _point_in_or_on_polygon(route.points[-1], zone_polygon_by_id[edge.to_zone_id]):
            raise ValueError(f"route {edge.id!r} does not end inside its to-zone")
    _validate_imported_geometry(
        tuple(zones),
        tuple(zone_edges),
        facility_specs,
        geometry,
        editable_geometry=editable_geometry,
    )
    return ImportedPlanGeometry(
        asset=asset,
        zones=tuple(zones),
        zone_edges=tuple(zone_edges),
        facilities=facility_specs,
        geometry=geometry,
        editable_geometry=editable_geometry,
        extraction_proposal=reviewed_proposal,
        source_provenance=source_provenance,
        notes=(
            "Geometry is an operator trace calibrated to stated raster extents; check source "
            "plan, scale, closures, and accessible paths.",
            f"Original upload SHA-256: {asset.sha256}",
            f"Trace operator: {trace.operator_id or 'unspecified'}",
            *(
                (
                    "Extraction confidence is proposal evidence only; accepted/rejected "
                    "status and pixel corrections were supplied by human review.",
                )
                if reviewed_proposal is not None
                else ()
            ),
        ),
    )


def import_metric_geojson(asset: PlanAsset, content: bytes) -> ImportedPlanGeometry:
    """Load the canonical local-meter JSON envelope from a user-supplied vector drawing."""
    if asset.status != "vector_data" or asset.media_type not in {
        "application/geo+json",
        "application/json",
    }:
        raise ValueError("metric GeoJSON import requires a JSON or GeoJSON plan asset")
    if hashlib.sha256(content).hexdigest() != asset.sha256:
        raise ValueError("vector plan bytes do not match the inspected upload SHA-256")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("vector plan must contain valid UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or document.get("units") != "m"
        or document.get("origin") != "top_left"
    ):
        raise ValueError("vector plan must declare units='m' and origin='top_left'")
    if document.get("type") == "FeatureCollection":
        return _import_metric_feature_collection(asset, document)
    zone_values = tuple(
        ZoneSpec.model_validate(value) for value in _json_objects(document, "zones")
    )
    edge_values = tuple(
        ZoneEdgeSpec.model_validate(value) for value in _json_objects(document, "zone_edges")
    )
    facility_values = tuple(
        FacilitySpec.model_validate(value) for value in _json_objects(document, "facilities")
    )
    geometry_values = document.get("geometry")
    if not isinstance(geometry_values, dict):
        raise ValueError("vector plan geometry object is required")
    geometry_data = dict(geometry_values)
    # The uploaded file is the evidence available at this boundary. Do not let
    # embedded claims promote unverified geometry to measured or bind arbitrary
    # source IDs that are absent from the research result.
    geometry_data["source_kind"] = "operator"
    geometry_data["source_ids"] = [asset.source_id]
    geometry_data["zones"] = [
        dict(zone, source_ids=[asset.source_id]) for zone in _json_objects(geometry_data, "zones")
    ]
    geometry = FacilityGeometry.model_validate(geometry_data)
    editable_geometry = None
    if document.get("editable_geometry") is not None:
        editable_data = document["editable_geometry"]
        if not isinstance(editable_data, dict):
            raise ValueError("editable_geometry must be an object when supplied")
        editable_data = dict(editable_data)
        editable_data["walls"] = [
            dict(item, source_ids=[asset.source_id])
            for item in _json_objects(editable_data, "walls")
        ]
        editable_data["openings"] = [
            dict(item, source_ids=[asset.source_id])
            for item in _json_objects(editable_data, "openings")
        ]
        editable_data["obstacles"] = [
            dict(item, source_ids=[asset.source_id])
            for item in _json_objects(editable_data, "obstacles")
        ]
        editable_data["equipment_footprints"] = [
            dict(item, source_ids=[asset.source_id])
            for item in _json_objects(editable_data, "equipment_footprints")
        ]
        editable_geometry = EditableFacilityGeometry.model_validate(editable_data)
    geometry = with_legacy_service_points(geometry, facility_values)
    validate_editable_routes(
        geometry.routes,
        editable_geometry
        or EditableFacilityGeometry(width_m=geometry.width_m, height_m=geometry.height_m),
        physical_zones=geometry.zones,
    )
    if {item.zone_id for item in geometry.zones} != {item.id for item in zone_values}:
        raise ValueError("vector plan geometry must position each scenario zone exactly once")
    if set(geometry.facility_points) != {item.id for item in facility_values}:
        raise ValueError("vector plan must position each scenario facility exactly once")
    if {item.edge_id for item in geometry.routes} != {item.id for item in edge_values}:
        raise ValueError("vector plan must define each scenario edge route exactly once")
    _validate_zone_topology(tuple(item.polygon for item in geometry.zones))
    _validate_imported_geometry(
        zone_values,
        edge_values,
        facility_values,
        geometry,
        editable_geometry=editable_geometry,
    )
    return ImportedPlanGeometry(
        asset=asset,
        zones=zone_values,
        zone_edges=edge_values,
        facilities=facility_values,
        geometry=geometry,
        editable_geometry=editable_geometry,
        notes=(
            "Metric local-coordinate vector upload imported with explicit scenario mapping.",
            f"Original upload SHA-256: {asset.sha256}",
        ),
    )


def _validate_imported_geometry(
    zones: tuple[ZoneSpec, ...],
    edges: tuple[ZoneEdgeSpec, ...],
    facilities: tuple[FacilitySpec, ...],
    geometry: FacilityGeometry,
    *,
    editable_geometry: EditableFacilityGeometry | None = None,
) -> None:
    zone_ids = {item.id for item in zones}
    facility_ids = {item.id for item in facilities}
    edge_ids = {item.id for item in edges}
    if (
        len(zone_ids) != len(zones)
        or len(facility_ids) != len(facilities)
        or len(edge_ids) != len(edges)
    ):
        raise ValueError("vector plan scenario IDs must be unique")
    if {item.zone_id for item in geometry.zones} != zone_ids:
        raise ValueError("vector plan geometry must position every scenario zone exactly once")
    if set(geometry.facility_points) != facility_ids:
        raise ValueError("vector plan must position every scenario facility exactly once")
    if {item.edge_id for item in geometry.routes} != edge_ids:
        raise ValueError("vector plan must define every scenario edge route exactly once")
    zone_by_id = {item.zone_id: item.polygon for item in geometry.zones}
    route_by_id = {item.edge_id: item for item in geometry.routes}
    if any(item.zone_id not in zone_ids for item in facilities):
        raise ValueError("vector plan facility references an unknown zone")
    for facility in facilities:
        if not _point_in_or_on_polygon(
            geometry.facility_points[facility.id], zone_by_id[facility.zone_id]
        ):
            raise ValueError(f"facility {facility.id!r} point lies outside its assigned zone")
    for edge in edges:
        if edge.from_zone_id not in zone_ids or edge.to_zone_id not in zone_ids:
            raise ValueError(f"vector plan route {edge.id!r} references an unknown zone")
        if edge.from_zone_id == edge.to_zone_id:
            raise ValueError(f"vector plan route {edge.id!r} must connect different zones")
        route = route_by_id[edge.id]
        if route.accessible and route.width_m < ACCESSIBLE_ROUTE_MIN_WIDTH_M:
            raise ValueError(
                f"route {edge.id!r} is marked accessible but is narrower than "
                f"{ACCESSIBLE_ROUTE_MIN_WIDTH_M:g} m"
            )
        if not _point_in_or_on_polygon(route.points[0], zone_by_id[edge.from_zone_id]):
            raise ValueError(f"route {edge.id!r} does not start inside its from-zone")
        if not _point_in_or_on_polygon(route.points[-1], zone_by_id[edge.to_zone_id]):
            raise ValueError(f"route {edge.id!r} does not end inside its to-zone")
    validate_facility_physical_layout(
        geometry,
        facilities,
        edges,
        editable_geometry
        or EditableFacilityGeometry(width_m=geometry.width_m, height_m=geometry.height_m),
    )
    reachable = {"sleeping"}
    while True:
        newly_reachable = {
            edge.to_zone_id for edge in edges if not edge.closed and edge.from_zone_id in reachable
        }
        updated = reachable | newly_reachable
        if updated == reachable:
            break
        reachable = updated
    returnable = {"sleeping"}
    while True:
        newly_returnable = {
            edge.from_zone_id for edge in edges if not edge.closed and edge.to_zone_id in returnable
        }
        updated = returnable | newly_returnable
        if updated == returnable:
            break
        returnable = updated
    service_zones = {facility.zone_id for facility in facilities}
    unreachable_services = sorted((service_zones - reachable) | (service_zones - returnable))
    if unreachable_services:
        raise ValueError(
            "vector plan has service facilities without an open round trip from the sleeping zone: "
            + ", ".join(unreachable_services)
        )


def revise_imported_plan_geometry(
    imported: ImportedPlanGeometry | dict[str, object],
    *,
    geometry: FacilityGeometry | dict[str, object],
    editable_geometry: EditableFacilityGeometry | dict[str, object] | None = None,
    edit_source_id: str = "SRC-OPERATOR-PLAN-REVISION",
) -> ImportedPlanGeometry:
    """Create a newly validated revision of an approved imported plan geometry.

    This function returns a new value and never mutates the source geometry asset.
    The caller can preview and approve the returned revision under a new geometry ID.
    """
    imported = (
        imported
        if isinstance(imported, ImportedPlanGeometry)
        else ImportedPlanGeometry.model_validate(imported)
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", edit_source_id):
        raise ValueError("plan correction source ID must be a stable identifier")
    candidate = (
        geometry
        if isinstance(geometry, FacilityGeometry)
        else FacilityGeometry.model_validate(geometry)
    )
    if editable_geometry is None:
        editable = imported.editable_geometry or EditableFacilityGeometry(
            width_m=candidate.width_m, height_m=candidate.height_m
        )
    elif isinstance(editable_geometry, EditableFacilityGeometry):
        editable = editable_geometry
    else:
        editable = EditableFacilityGeometry.model_validate(editable_geometry)
    candidate = with_legacy_service_points(candidate, imported.facilities)
    changed = candidate != imported.geometry or editable != imported.editable_geometry
    if changed:
        candidate = candidate.model_copy(
            update={
                "source_kind": "operator",
                "source_ids": tuple(sorted(set(candidate.source_ids) | {edit_source_id})),
                "zones": tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in candidate.zones
                ),
            }
        )
        editable = editable.model_copy(
            update={
                field_name: tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in getattr(editable, field_name)
                )
                for field_name in ("walls", "openings", "obstacles", "equipment_footprints")
            }
        )
    if not math.isclose(
        candidate.width_m, editable.width_m, rel_tol=0, abs_tol=1e-9
    ) or not math.isclose(candidate.height_m, editable.height_m, rel_tol=0, abs_tol=1e-9):
        raise ValueError("walkable and editable plan geometry must use the same metric dimensions")
    validate_editable_routes(candidate.routes, editable, physical_zones=candidate.zones)
    _validate_imported_geometry(
        imported.zones,
        imported.zone_edges,
        imported.facilities,
        candidate,
        editable_geometry=editable,
    )
    notes = imported.notes
    provenance = imported.source_provenance
    if changed:
        notes = (
            *notes,
            f"Operator geometry revision source: {edit_source_id}; original imported "
            "geometry remains unchanged.",
        )
        if provenance is not None and provenance.pixel_to_local_m_transform is not None:
            provenance = provenance.model_copy(update={"transform_scope": "source_trace_only"})
    return imported.model_copy(
        update={
            "geometry": candidate,
            "editable_geometry": editable,
            "source_provenance": provenance,
            "notes": notes,
        }
    )


def _cell(row: dict[str, str], column: str | None) -> str | None:
    if column is None:
        return None
    value = (row.get(column) or "").strip()
    return value or None


def _float_cell(row: dict[str, str], column: str | None) -> float | None:
    value = _cell(row, column)
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"column {column!r} contains a non-finite number")
    return result


def _int_cell(row: dict[str, str], column: str | None) -> int | None:
    value = _cell(row, column)
    if value is None:
        return None
    return int(value)


def _polygon_area(polygon: tuple[Point2D, ...]) -> float:
    points = [(item.x, item.y) for item in polygon]
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1]))) / 2


def _import_metric_feature_collection(
    asset: PlanAsset, document: dict[str, Any]
) -> ImportedPlanGeometry:
    width_m = _geojson_number(document.get("width_m", 0), "width_m")
    height_m = _geojson_number(document.get("height_m", 0), "height_m")
    if width_m <= 0 or height_m <= 0:
        raise ValueError("metric GeoJSON FeatureCollection requires positive width_m and height_m")
    zones: list[ZoneSpec] = []
    physical_zones: list[PhysicalZone] = []
    edges: list[ZoneEdgeSpec] = []
    physical_routes: list[PhysicalRoute] = []
    facilities: list[FacilitySpec] = []
    facility_points: dict[str, Point2D] = {}
    service_points: list[FacilityServicePoint] = []
    walls: list[FacilityWall] = []
    openings: list[FacilityOpening] = []
    obstacles: list[FacilityObstacle] = []
    equipment_footprints: list[FacilityEquipmentFootprint] = []
    route_openings: list[RouteOpeningUse] = []
    features = document.get("features")
    if not isinstance(features, list):
        raise ValueError("GeoJSON FeatureCollection features must be an array")
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError("each GeoJSON feature needs a properties object")
        properties = feature["properties"]
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("each GeoJSON feature needs a geometry object")
        feature_type = properties.get("sheltersim_type")
        coordinates = geometry.get("coordinates")
        if feature_type == "zone" and geometry.get("type") == "Polygon":
            if not isinstance(coordinates, list) or not coordinates:
                raise ValueError("zone Polygon requires an exterior ring")
            if len(coordinates) != 1:
                raise ValueError(
                    "zone Polygon holes are unsupported; import an exterior-only outline"
                )
            ring = coordinates[0]
            if not isinstance(ring, list):
                raise ValueError("zone Polygon exterior ring must be an array")
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring = ring[:-1]
            polygon = tuple(_geojson_point(item) for item in ring)
            zone_id = str(properties.get("zone_id") or "")
            area = _geojson_number(properties.get("usable_area_m2", 0), "usable_area_m2")
            zones.append(
                ZoneSpec(
                    id=zone_id,
                    name=str(properties.get("name") or zone_id),
                    capacity=properties.get("capacity"),
                    display_x=properties.get("display_x"),
                    display_y=properties.get("display_y"),
                )
            )
            physical_zones.append(
                PhysicalZone(
                    zone_id=zone_id,
                    polygon=polygon,
                    usable_area_m2=area,
                    source_ids=(asset.source_id,),
                )
            )
        elif feature_type == "facility" and geometry.get("type") == "Point":
            facility_id = str(properties.get("id") or "")
            point = _geojson_point(coordinates)
            facilities.append(
                FacilitySpec(
                    id=facility_id,
                    kind=str(properties.get("kind") or ""),
                    zone_id=str(properties.get("zone_id") or ""),
                    capacity=properties.get("capacity", 0),
                    working_units=properties.get("working_units"),
                )
            )
            facility_points[facility_id] = point
        elif feature_type == "service_point" and geometry.get("type") == "Point":
            service_points.append(
                FacilityServicePoint(
                    id=str(properties.get("id") or ""),
                    facility_id=str(properties.get("facility_id") or ""),
                    point_m=_geojson_point(coordinates),
                    access=ServiceAccessConditions.model_validate(
                        {
                            "accessibility": properties.get("accessibility", "unknown"),
                            "required_access_tags": properties.get("required_access_tags", ()),
                            "required_staff_skills": properties.get("required_staff_skills", ()),
                        }
                    ),
                    service_capacity=properties.get("service_capacity"),
                    queue_id=str(properties.get("queue_id") or ""),
                )
            )
        elif feature_type == "route" and geometry.get("type") == "LineString":
            route_points = _geojson_line(coordinates, feature_type="route")
            edge_id = str(properties.get("edge_id") or "")
            edges.append(
                ZoneEdgeSpec(
                    id=edge_id,
                    from_zone_id=str(properties.get("from_zone_id") or ""),
                    to_zone_id=str(properties.get("to_zone_id") or ""),
                    travel_minutes=properties.get("travel_minutes", 1),
                    closed=properties.get("closed", False),
                )
            )
            physical_routes.append(
                PhysicalRoute(
                    edge_id=edge_id,
                    points=route_points,
                    width_m=properties.get("width_m", 1.8),
                    accessible=properties.get("accessible", False),
                )
            )
            opening_id = properties.get("opening_id")
            if opening_id is not None:
                route_openings.append(RouteOpeningUse(route_id=edge_id, opening_id=str(opening_id)))
        elif feature_type == "wall" and geometry.get("type") == "LineString":
            points = _geojson_line(coordinates, feature_type="wall")
            if len(points) != 2:
                raise ValueError("GeoJSON wall features must contain exactly two endpoints")
            walls.append(
                FacilityWall(
                    wall_id=str(properties.get("wall_id") or ""),
                    start=points[0],
                    end=points[1],
                    source_ids=(asset.source_id,),
                )
            )
        elif feature_type == "opening" and geometry.get("type") == "LineString":
            points = _geojson_line(coordinates, feature_type="opening")
            if len(points) != 2:
                raise ValueError("GeoJSON opening features must contain exactly two endpoints")
            openings.append(
                FacilityOpening(
                    opening_id=str(properties.get("opening_id") or ""),
                    wall_id=str(properties.get("wall_id") or ""),
                    start=points[0],
                    end=points[1],
                    width_m=properties.get("width_m", 0),
                    opening_kind=str(properties.get("opening_kind") or "doorway"),
                    source_ids=(asset.source_id,),
                )
            )
        elif feature_type == "obstacle" and geometry.get("type") == "Polygon":
            coordinates = _geojson_polygon_ring(coordinates, feature_type="obstacle")
            obstacles.append(
                FacilityObstacle(
                    obstacle_id=str(properties.get("obstacle_id") or ""),
                    polygon=tuple(_geojson_point(item) for item in coordinates),
                    source_ids=(asset.source_id,),
                )
            )
        elif feature_type == "equipment_footprint" and geometry.get("type") == "Polygon":
            coordinates = _geojson_polygon_ring(coordinates, feature_type="equipment_footprint")
            equipment_footprints.append(
                FacilityEquipmentFootprint(
                    facility_id=str(properties.get("facility_id") or ""),
                    polygon=tuple(_geojson_point(item) for item in coordinates),
                    area_m2=properties.get("area_m2", 0),
                    source_ids=(asset.source_id,),
                )
            )
        else:
            raise ValueError(
                "GeoJSON features must map to zone, facility, service_point, route, wall, "
                "opening, obstacle, or equipment_footprint geometry"
            )
    _validate_zone_topology(tuple(item.polygon for item in physical_zones))
    facility_geometry = FacilityGeometry(
        width_m=width_m,
        height_m=height_m,
        source_kind="operator",
        source_ids=(asset.source_id,),
        zones=tuple(physical_zones),
        facility_points=facility_points,
        service_points=tuple(service_points),
        routes=tuple(physical_routes),
    )
    facility_geometry = with_legacy_service_points(facility_geometry, tuple(facilities))
    editable_geometry = None
    if walls or openings or obstacles or equipment_footprints or route_openings:
        editable_geometry = EditableFacilityGeometry(
            width_m=width_m,
            height_m=height_m,
            walls=tuple(walls),
            openings=tuple(openings),
            obstacles=tuple(obstacles),
            route_openings=tuple(route_openings),
            equipment_footprints=tuple(equipment_footprints),
        )
        validate_editable_routes(
            tuple(physical_routes), editable_geometry, physical_zones=facility_geometry.zones
        )
    _validate_imported_geometry(
        tuple(zones),
        tuple(edges),
        tuple(facilities),
        facility_geometry,
        editable_geometry=editable_geometry,
    )
    if {item.zone_id for item in physical_zones} != {item.id for item in zones}:
        raise ValueError("GeoJSON must define physical geometry for every zone")
    if {item.edge_id for item in physical_routes} != {item.id for item in edges}:
        raise ValueError("GeoJSON must define physical geometry for every route")
    if set(facility_points) != {item.id for item in facilities}:
        raise ValueError("GeoJSON must define a point for every facility")
    zone_ids = {item.id for item in zones}
    if any(item.from_zone_id not in zone_ids or item.to_zone_id not in zone_ids for item in edges):
        raise ValueError("GeoJSON route references an unknown zone")
    if any(item.zone_id not in zone_ids for item in facilities):
        raise ValueError("GeoJSON facility references an unknown zone")
    validate_editable_routes(
        tuple(physical_routes),
        editable_geometry or EditableFacilityGeometry(width_m=width_m, height_m=height_m),
        physical_zones=tuple(physical_zones),
    )
    zone_polygon_by_id = {item.zone_id: item.polygon for item in physical_zones}
    for facility in facilities:
        if not _point_in_or_on_polygon(
            facility_points[facility.id], zone_polygon_by_id[facility.zone_id]
        ):
            raise ValueError(f"facility {facility.id!r} point lies outside its assigned zone")
    route_by_id = {item.edge_id: item for item in physical_routes}
    for edge in edges:
        route = route_by_id[edge.id]
        if not _point_in_or_on_polygon(route.points[0], zone_polygon_by_id[edge.from_zone_id]):
            raise ValueError(f"route {edge.id!r} does not start inside its from-zone")
        if not _point_in_or_on_polygon(route.points[-1], zone_polygon_by_id[edge.to_zone_id]):
            raise ValueError(f"route {edge.id!r} does not end inside its to-zone")
    return ImportedPlanGeometry(
        asset=asset,
        zones=tuple(zones),
        zone_edges=tuple(edges),
        facilities=tuple(facilities),
        geometry=facility_geometry,
        editable_geometry=editable_geometry,
        notes=(
            "Metric local-coordinate GeoJSON features imported with explicit ShelterSim "
            "feature types, including optional editable walls, openings, obstacles, and "
            "equipment footprints.",
            f"Original upload SHA-256: {asset.sha256}",
        ),
    )


def _geojson_point(value: Any) -> Point2D:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("GeoJSON coordinates must contain x and y")
    return Point2D(x=_geojson_number(value[0], "x"), y=_geojson_number(value[1], "y"))


def _geojson_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"GeoJSON {name} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"GeoJSON {name} must be finite")
    return number


def _geojson_line(coordinates: Any, *, feature_type: str) -> tuple[Point2D, ...]:
    if not isinstance(coordinates, list):
        raise ValueError(f"GeoJSON {feature_type} LineString coordinates must be an array")
    return tuple(_geojson_point(item) for item in coordinates)


def _json_objects(document: dict[str, Any], name: str) -> list[dict[str, Any]]:
    values = document.get(name, [])
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise ValueError(f"vector plan {name} must be an array of objects")
    return values


def _geojson_polygon_ring(coordinates: Any, *, feature_type: str) -> list[Any]:
    if not isinstance(coordinates, list) or len(coordinates) != 1:
        raise ValueError(f"GeoJSON {feature_type} Polygon requires one exterior ring without holes")
    ring = coordinates[0]
    if not isinstance(ring, list):
        raise ValueError(f"GeoJSON {feature_type} Polygon exterior ring must be an array")
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise ValueError(f"GeoJSON {feature_type} Polygon requires at least three vertices")
    return ring


def _validate_zone_topology(polygons: tuple[tuple[Point2D, ...], ...]) -> None:
    """Reject self-crossing zone outlines and intersecting zone interiors."""
    for polygon in polygons:
        coordinates = tuple((point.x, point.y) for point in polygon)
        segments = tuple(zip(coordinates, coordinates[1:] + coordinates[:1]))
        for left_index, first in enumerate(segments):
            for right_index, second in enumerate(segments):
                if right_index <= left_index + 1 or (
                    left_index == 0 and right_index == len(segments) - 1
                ):
                    continue
                if _segments_cross(first[0], first[1], second[0], second[1]):
                    raise ValueError("traced zone polygon self-intersects")
    for index, first in enumerate(polygons):
        for second in polygons[index + 1 :]:
            if any(_point_strictly_inside(point, second) for point in first) or any(
                _point_strictly_inside(point, first) for point in second
            ):
                raise ValueError("traced physical zone interiors overlap")
            first_segments = tuple(zip(first, first[1:] + first[:1]))
            second_segments = tuple(zip(second, second[1:] + second[:1]))
            if any(
                _segments_cross((a.x, a.y), (b.x, b.y), (c.x, c.y), (d.x, d.y))
                for (a, b) in first_segments
                for (c, d) in second_segments
            ):
                raise ValueError("traced physical zone boundaries cross")
            if any(
                _point_strictly_inside(sample, second)
                for sample in _polygon_interior_samples(first)
            ) or any(
                _point_strictly_inside(sample, first)
                for sample in _polygon_interior_samples(second)
            ):
                raise ValueError("traced physical zone interiors overlap")
            first_area = _polygon_area(first)
            second_area = _polygon_area(second)
            if (
                abs(first_area - second_area) <= 1e-9
                and all(_point_on_polygon(point, second) for point in first)
                and all(_point_on_polygon(point, first) for point in second)
            ):
                raise ValueError("traced physical zone interiors overlap")


def _polygon_interior_samples(polygon: tuple[Point2D, ...]) -> tuple[Point2D, ...]:
    """Sample just inside each edge to catch overlapping collinear boundaries."""
    coordinates = tuple((point.x, point.y) for point in polygon)
    signed_area_twice = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(coordinates, coordinates[1:] + coordinates[:1])
    )
    if abs(signed_area_twice) <= 1e-12:
        return ()
    span = max(
        max(point.x for point in polygon) - min(point.x for point in polygon),
        max(point.y for point in polygon) - min(point.y for point in polygon),
        1.0,
    )
    offset = max(1e-8, span * 1e-7)
    orientation = 1.0 if signed_area_twice > 0 else -1.0
    samples: list[Point2D] = []
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        dx = second.x - first.x
        dy = second.y - first.y
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        inward_x = orientation * -dy / length
        inward_y = orientation * dx / length
        samples.append(
            Point2D(
                x=(first.x + second.x) / 2 + inward_x * offset,
                y=(first.y + second.y) / 2 + inward_y * offset,
            )
        )
    return tuple(sample for sample in samples if _point_strictly_inside(sample, polygon))


def _point_inside(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current.y > point.y) != (previous.y > point.y):
            crossing_x = (previous.x - current.x) * (point.y - current.y) / (
                previous.y - current.y
            ) + current.x
            if point.x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _point_strictly_inside(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    return not _point_on_polygon(point, polygon) and _point_inside(point, polygon)


def _point_on_polygon(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        cross = (point.x - first.x) * (second.y - first.y) - (point.y - first.y) * (
            second.x - first.x
        )
        within = (
            min(first.x, second.x) - 1e-9 <= point.x <= max(first.x, second.x) + 1e-9
            and min(first.y, second.y) - 1e-9 <= point.y <= max(first.y, second.y) + 1e-9
        )
        if abs(cross) <= 1e-9 and within:
            return True
    return False


def _point_in_or_on_polygon(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    return _point_on_polygon(point, polygon) or _point_inside(point, polygon)


def _segments_cross(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    def orientation(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    o1 = orientation(first_start, first_end, second_start)
    o2 = orientation(first_start, first_end, second_end)
    o3 = orientation(second_start, second_end, first_start)
    o4 = orientation(second_start, second_end, first_end)
    crosses_first = o1 > 1e-9 and o2 < -1e-9 or o1 < -1e-9 and o2 > 1e-9
    crosses_second = o3 > 1e-9 and o4 < -1e-9 or o3 < -1e-9 and o4 > 1e-9
    return crosses_first and crosses_second
