"""Build editable standard shelter zones and physical geometry from explicit inputs."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Literal

from pydantic import Field, model_validator

from sheltersim.contracts.extensions import (
    FacilityGeometry,
    PhysicalRoute,
    PhysicalZone,
    Point2D,
    point_in_polygon,
)
from sheltersim.contracts.spatial import legacy_service_points
from sheltersim.experiments.scenario import FacilitySpec, StaffSpec, ZoneEdgeSpec, ZoneSpec
from sheltersim.region.conditions import FacilityEquipmentCondition
from sheltersim.region.models import ACCESSIBLE_ROUTE_MIN_WIDTH_M, RegionModel

SYNTHETIC_FACILITY_SOURCE_ID = "ASM-STANDARD-SHELTER-GEOMETRY-V1"
SYNTHETIC_CAPACITY_SOURCE_ID = "ASM-SHELTER-AREA-HEURISTIC-V1"
SYNTHETIC_STAFFING_SOURCE_ID = "ASM-SHELTER-STAFFING-HEURISTIC-V2"
SYNTHETIC_TOILET_SOURCE_ID = "ASM-SHELTER-TOILET-HEURISTIC-V2"
SERVICE_STATION_SOURCE_ID = "ASM-SHELTER-SERVICE-STATIONS-V1"


class FacilityZoneCondition(RegionModel):
    """Typed add/remove instruction for optional and custom standard-facility zones."""

    action: Literal["include", "exclude"]
    zone_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    area_share: float | None = Field(default=None, gt=0, le=0.8, allow_inf_nan=False)
    facility_id: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$")
    facility_kind: str | None = Field(default=None, min_length=1, max_length=80)
    capacity: int = Field(default=1, ge=0)
    source_id: str = Field(
        default="SRC-OPERATOR-FACILITY-ZONE", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
    )
    source_kind: Literal["operator", "assumption"] = "operator"

    @model_validator(mode="after")
    def validate_instruction(self) -> FacilityZoneCondition:
        if self.action == "include" and self.area_share is None:
            raise ValueError("an included facility zone requires an area_share")
        if self.action == "exclude" and any(
            value is not None
            for value in (self.name, self.area_share, self.facility_id, self.facility_kind)
        ):
            raise ValueError("an excluded facility zone cannot define replacement fields")
        if (self.facility_id is None) != (self.facility_kind is None):
            raise ValueError("custom zone facilities require both facility_id and facility_kind")
        return self


class FacilityDesignRequest(RegionModel):
    population: int = Field(ge=1, le=10_000)
    gross_area_m2: float | None = Field(default=None, gt=0, le=100_000, allow_inf_nan=False)
    aspect_ratio: float = Field(default=1.5, gt=0.3, le=5.0, allow_inf_nan=False)
    accessible_toilet_count: int = Field(default=1, ge=0, le=20)
    staff_count: int | None = Field(default=None, ge=0, le=100)
    facility_conditions: tuple[str, ...] = ()
    zone_area_shares: dict[str, float] = Field(default_factory=dict)
    zone_conditions: tuple[FacilityZoneCondition, ...] = ()
    equipment: tuple[FacilityEquipmentCondition, ...] = ()
    area_source_ids: tuple[str, ...] = ()
    stated_capacity: int | None = Field(default=None, ge=0)
    capacity_source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def area_shares_are_bounded(self) -> FacilityDesignRequest:
        if any(
            not math.isfinite(value) or value < 0 or value > 1
            for value in self.zone_area_shares.values()
        ):
            raise ValueError("zone area shares must be finite fractions from 0 to 1")
        if sum(self.zone_area_shares.values()) > 1 + 1e-9:
            raise ValueError("zone area shares cannot exceed the full building area")
        if len({item.zone_id for item in self.zone_conditions}) != len(self.zone_conditions):
            raise ValueError("facility zone conditions must use unique zone IDs")
        return self


class FacilityWall(RegionModel):
    wall_id: str
    start: Point2D
    end: Point2D
    source_ids: tuple[str, ...] = ()


class FacilityOpening(RegionModel):
    opening_id: str
    wall_id: str
    start: Point2D
    end: Point2D
    width_m: float = Field(gt=0, le=20, allow_inf_nan=False)
    opening_kind: str = "doorway"
    source_ids: tuple[str, ...] = ()


class FacilityObstacle(RegionModel):
    obstacle_id: str
    polygon: tuple[Point2D, ...] = Field(min_length=3, max_length=128)
    source_ids: tuple[str, ...] = ()


class FacilityEquipmentFootprint(RegionModel):
    facility_id: str
    polygon: tuple[Point2D, ...] = Field(min_length=4, max_length=4)
    area_m2: float = Field(gt=0, allow_inf_nan=False)
    source_ids: tuple[str, ...] = ()


class RouteOpeningUse(RegionModel):
    route_id: str
    opening_id: str


class EditableFacilityGeometry(RegionModel):
    """Region preview geometry; shared FacilityGeometry v1 remains the simulation contract."""

    schema_version: int = 1
    width_m: float = Field(gt=0, allow_inf_nan=False)
    height_m: float = Field(gt=0, allow_inf_nan=False)
    walls: tuple[FacilityWall, ...] = ()
    openings: tuple[FacilityOpening, ...] = ()
    obstacles: tuple[FacilityObstacle, ...] = ()
    route_openings: tuple[RouteOpeningUse, ...] = ()
    equipment_footprints: tuple[FacilityEquipmentFootprint, ...] = ()

    @model_validator(mode="after")
    def validate_boundaries(self) -> EditableFacilityGeometry:
        wall_by_id = {item.wall_id: item for item in self.walls}
        opening_by_id = {item.opening_id: item for item in self.openings}
        if len(wall_by_id) != len(self.walls) or len(opening_by_id) != len(self.openings):
            raise ValueError("facility wall and opening IDs must be unique")

        def bounded(point: Point2D) -> bool:
            return 0 <= point.x <= self.width_m and 0 <= point.y <= self.height_m

        if any(not bounded(point) for wall in self.walls for point in (wall.start, wall.end)):
            raise ValueError("facility wall segment is outside the building bounds")
        if any(wall.start == wall.end for wall in self.walls):
            raise ValueError("facility walls must have positive length")
        for opening in self.openings:
            wall = wall_by_id.get(opening.wall_id)
            if wall is None:
                raise ValueError(f"opening {opening.opening_id!r} references an unknown wall")
            if not bounded(opening.start) or not bounded(opening.end):
                raise ValueError("facility opening is outside the building bounds")
            if not _point_on_segment(opening.start, wall.start, wall.end) or not _point_on_segment(
                opening.end, wall.start, wall.end
            ):
                raise ValueError("facility opening endpoints must lie on their host wall")
            segment_length = math.hypot(
                opening.end.x - opening.start.x, opening.end.y - opening.start.y
            )
            if abs(segment_length - opening.width_m) > max(1e-6, opening.width_m * 1e-6):
                raise ValueError("facility opening width must match its drawn opening segment")
        for index, first in enumerate(self.openings):
            wall = wall_by_id[first.wall_id]
            axis_length = math.hypot(wall.end.x - wall.start.x, wall.end.y - wall.start.y)
            axis_x = (wall.end.x - wall.start.x) / axis_length
            axis_y = (wall.end.y - wall.start.y) / axis_length
            first_interval = sorted(
                (
                    (first.start.x - wall.start.x) * axis_x
                    + (first.start.y - wall.start.y) * axis_y,
                    (first.end.x - wall.start.x) * axis_x + (first.end.y - wall.start.y) * axis_y,
                )
            )
            for second in self.openings[index + 1 :]:
                if second.wall_id != first.wall_id:
                    continue
                second_interval = sorted(
                    (
                        (second.start.x - wall.start.x) * axis_x
                        + (second.start.y - wall.start.y) * axis_y,
                        (second.end.x - wall.start.x) * axis_x
                        + (second.end.y - wall.start.y) * axis_y,
                    )
                )
                if (
                    min(first_interval[1], second_interval[1])
                    > max(first_interval[0], second_interval[0]) + 1e-8
                ):
                    raise ValueError("facility openings on the same wall cannot overlap")
        uses = {item.route_id: item.opening_id for item in self.route_openings}
        if len(uses) != len(self.route_openings):
            raise ValueError("a physical route can reference only one opening in this draft")
        for item in self.route_openings:
            if item.opening_id not in opening_by_id:
                raise ValueError("route opening reference points to an unknown opening")
        obstacle_ids = [item.obstacle_id for item in self.obstacles]
        if len(obstacle_ids) != len(set(obstacle_ids)):
            raise ValueError("facility obstacle IDs must be unique")
        if any(not bounded(point) for obstacle in self.obstacles for point in obstacle.polygon):
            raise ValueError("facility obstacle is outside the building bounds")
        if any(not _valid_simple_polygon(obstacle.polygon) for obstacle in self.obstacles):
            raise ValueError("facility obstacle polygons must be simple and have positive area")
        footprint_ids = [item.facility_id for item in self.equipment_footprints]
        if len(footprint_ids) != len(set(footprint_ids)):
            raise ValueError("facility equipment footprints must have unique facility IDs")
        if any(not bounded(point) for item in self.equipment_footprints for point in item.polygon):
            raise ValueError("equipment footprint is outside the building bounds")
        for item in self.equipment_footprints:
            if not _valid_simple_polygon(item.polygon):
                raise ValueError(
                    "equipment footprint polygons must be simple and have positive area"
                )
            points = item.polygon
            polygon_area = (
                abs(sum(a.x * b.y - b.x * a.y for a, b in zip(points, points[1:] + points[:1]))) / 2
            )
            if abs(polygon_area - item.area_m2) > max(1e-6, item.area_m2 * 1e-6):
                raise ValueError("equipment footprint area does not match its polygon")
        return self


class FacilityDesign(RegionModel):
    zones: tuple[ZoneSpec, ...]
    zone_edges: tuple[ZoneEdgeSpec, ...]
    facilities: tuple[FacilitySpec, ...]
    staff: tuple[StaffSpec, ...]
    geometry: FacilityGeometry
    editable_geometry: EditableFacilityGeometry
    capacity_supported: int
    requested_population: int
    observations: tuple[dict[str, object], ...]
    assumptions: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]


class FacilityGeometryIssue(RegionModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=1_000)
    severity: Literal["error", "warning"] = "error"
    target_id: str | None = Field(default=None, max_length=120)


class StandardFacilityRevalidation(RegionModel):
    """Editable standard-plan revision plus explicit geometry and capacity findings."""

    schema_version: Literal[1] = 1
    status: Literal["valid", "invalid"]
    design: FacilityDesign
    issues: tuple[FacilityGeometryIssue, ...] = ()
    edit_source_id: str | None = None
    effective_sleeping_area_m2: float = Field(default=0, ge=0, allow_inf_nan=False)


_ZONE_DESIGN: tuple[tuple[str, str, float], ...] = (
    ("sleeping", "避難スペース", 0.62),
    ("quiet", "静養スペース", 0.08),
    ("medical", "救護スペース", 0.06),
    ("food", "食事・給水スペース", 0.08),
    ("toilet", "トイレ", 0.06),
    ("intake", "受付", 0.05),
    ("staff", "運営本部", 0.05),
)


class StandardShelterGenerator:
    """Create a reproducible editable floor-plan draft, clearly marked as synthetic."""

    def generate(self, request: FacilityDesignRequest) -> FacilityDesign:
        if not isinstance(request, FacilityDesignRequest):
            request = FacilityDesignRequest.model_validate(request)
        area_source_ids = request.area_source_ids or (
            ("SRC-OPERATOR-AREA",) if request.gross_area_m2 is not None else ()
        )
        zone_design = list(_ZONE_DESIGN)
        condition_text = " ".join(request.facility_conditions).casefold()
        recognized: set[str] = set()
        for terms, optional_zone in (
            (("ペット", "pet"), ("pet", "ペット同行スペース", 0.08)),
            (("授乳", "乳幼児", "family", "infant"), ("family", "乳幼児・授乳スペース", 0.06)),
            (("個室", "プライバシー", "privacy"), ("private", "個別・プライバシースペース", 0.08)),
        ):
            if any(term in condition_text for term in terms):
                zone_design.append(optional_zone)
                recognized.update(terms)
                sleeping_index = next(
                    index for index, item in enumerate(zone_design) if item[0] == "sleeping"
                )
                key, label, share = zone_design[sleeping_index]
                zone_design[sleeping_index] = (key, label, share - optional_zone[2])
        zone_design = _apply_zone_conditions(zone_design, request.zone_conditions)
        accessible_terms = ("車いす", "車椅子", "wheelchair", "バリアフリー")
        if any(term in condition_text for term in accessible_terms):
            recognized.update(accessible_terms)
        if request.zone_area_shares:
            known_zone_ids = {zone_id for zone_id, _, _ in zone_design}
            unknown_zone_ids = set(request.zone_area_shares) - known_zone_ids
            if unknown_zone_ids:
                raise ValueError(
                    "zone area shares reference unknown standard zones: "
                    + ", ".join(sorted(unknown_zone_ids))
                )
            zone_design = _apply_zone_area_shares(zone_design, request.zone_area_shares)
        gross_area = request.gross_area_m2 or _default_gross_area(
            request.population,
            aspect_ratio=request.aspect_ratio,
            zone_design=zone_design,
        )
        width_m = math.sqrt(gross_area * request.aspect_ratio)
        requested_shares = {zone_id: share for zone_id, _, share in zone_design}
        zone_design = _with_accessible_frontage(zone_design, width_m)
        widened_zones = [
            zone_id
            for zone_id, _, share in zone_design
            if zone_id != "sleeping" and share > requested_shares[zone_id] + 1e-12
        ]
        sleeping_share = next(share for zone_id, _, share in zone_design if zone_id == "sleeping")
        height_m = gross_area / width_m
        corridor_depth_m = min(1.8, height_m * 0.25)
        room_height_m = height_m - corridor_depth_m
        room_fraction = room_height_m / height_m
        sleeping_usable_area_m2 = gross_area * sleeping_share * room_fraction * 0.9
        area_capacity = math.floor(sleeping_usable_area_m2 / 3.5)
        # This planning heuristic is intentionally visible and is not a legal standard.
        capacity_supported = (
            min(request.stated_capacity, area_capacity)
            if request.stated_capacity is not None
            else area_capacity
        )
        warnings: list[str] = []
        if widened_zones:
            warnings.append(
                f"出入口幅{ACCESSIBLE_ROUTE_MIN_WIDTH_M} mを確保するため、次のゾーンの面積配分を広げ、その分を避難スペースから差し引きました: "
                + "、".join(widened_zones)
            )
        unrecognized = [
            condition
            for condition in request.facility_conditions
            if not any(term in condition.casefold() for term in recognized)
        ]
        if unrecognized:
            warnings.append(
                "次の施設条件は標準モデルへ自動反映できず、編集時に確認が必要です: "
                + "、".join(unrecognized)
            )
        if request.stated_capacity is not None and request.stated_capacity > area_capacity:
            warnings.append(
                f"公開収容数{request.stated_capacity}人に対し、避難スペース面積からの参考上限は{area_capacity}人です。より小さい値をシナリオに使っています。"
            )
        if capacity_supported < request.population and request.stated_capacity is not None:
            warnings.append(
                f"公開収容数と避難スペース面積から採用した参考上限{capacity_supported}人が入力人数{request.population}人を下回っています。出典の現行性と実際の利用可能面積を確認してください。"
            )
        elif capacity_supported < request.population:
            warnings.append(
                f"入力人数{request.population}人に対し、避難スペースの参考収容数は{capacity_supported}人です。実際の利用可能面積を確認してください。"
            )
        minimum_route_width = min(
            corridor_depth_m,
            *(width_m * share for _, _, share in zone_design),
        )
        if minimum_route_width < ACCESSIBLE_ROUTE_MIN_WIDTH_M:
            warnings.append(
                "標準モデルの共用通路または居室出入口は1.8 m未満です。一部の生成経路はアクセシブル経路として扱っていません。"
            )
        warnings.extend(
            (
                "平面形状、1.8 m通路、面積配分、設備数は標準モデルの仮定です。実施設の図面・法令適合・避難安全性を示しません。",
                "食料・飲料水の在庫量は施設数から推定していません。在庫を入力しない場合の既定量はScenarioの仮定として記録します。",
            )
        )
        sources = tuple(
            sorted(
                {
                    SYNTHETIC_FACILITY_SOURCE_ID,
                    SYNTHETIC_CAPACITY_SOURCE_ID,
                    SYNTHETIC_STAFFING_SOURCE_ID,
                    SYNTHETIC_TOILET_SOURCE_ID,
                    SERVICE_STATION_SOURCE_ID,
                    *area_source_ids,
                    *request.capacity_source_ids,
                    *(item.source_id for item in request.zone_conditions),
                }
            )
        )
        geometry_source_ids = tuple(
            sorted(
                {
                    SYNTHETIC_FACILITY_SOURCE_ID,
                    *area_source_ids,
                    *request.capacity_source_ids,
                    *(item.source_id for item in request.equipment),
                    *(item.source_id for item in request.zone_conditions),
                }
            )
        )
        source_kind = "inferred" if area_source_ids or request.capacity_source_ids else "synthetic"
        if (request.stated_capacity is None and request.capacity_source_ids) or (
            request.stated_capacity is not None and not request.capacity_source_ids
        ):
            raise ValueError("a stated facility capacity requires a source ID")
        if source_kind == "inferred":
            warnings.append(
                "選択施設の出典値を使っていますが、長さ・ゾーン・設備配置は実図面ではなく編集可能な概略推定です。"
            )

        zones: list[ZoneSpec] = []
        physical_zones: list[PhysicalZone] = []
        zone_centers: dict[str, Point2D] = {}
        corridor_points = (
            Point2D(x=0.0, y=0.0),
            Point2D(x=width_m, y=0.0),
            Point2D(x=width_m, y=corridor_depth_m),
            Point2D(x=0.0, y=corridor_depth_m),
        )
        zones.append(
            ZoneSpec(
                id="corridor", name="共用通路", capacity=None, display_x=100.0, display_y=100.0
            )
        )
        physical_zones.append(
            PhysicalZone(
                zone_id="corridor",
                polygon=corridor_points,
                usable_area_m2=width_m * corridor_depth_m * 0.9,
                source_ids=geometry_source_ids,
            )
        )
        zone_centers["corridor"] = Point2D(x=width_m / 2, y=corridor_depth_m / 2)
        x_cursor = 0.0
        for index, (zone_id, name, share) in enumerate(zone_design):
            zone_width = width_m * share if index < len(zone_design) - 1 else width_m - x_cursor
            left = x_cursor
            right = width_m if index == len(zone_design) - 1 else x_cursor + zone_width
            top = corridor_depth_m
            bottom = height_m
            points = (
                Point2D(x=left, y=top),
                Point2D(x=right, y=top),
                Point2D(x=right, y=bottom),
                Point2D(x=left, y=bottom),
            )
            area = (right - left) * (bottom - top)
            zones.append(
                ZoneSpec(
                    id=zone_id,
                    name=name,
                    capacity=capacity_supported if zone_id == "sleeping" else request.population,
                    display_x=100.0,
                    display_y=260.0 + index * 120.0,
                )
            )
            physical_zones.append(
                PhysicalZone(
                    zone_id=zone_id,
                    polygon=points,
                    usable_area_m2=area * 0.9,
                    source_ids=geometry_source_ids,
                )
            )
            zone_centers[zone_id] = Point2D(x=(left + right) / 2, y=(top + bottom) / 2)
            x_cursor = right

        counts = standard_service_counts(request.population)
        toilets = max(
            request.accessible_toilet_count + counts["toilet_standard"], counts["toilet_demand"]
        )
        if toilets > 20:
            warnings.append(
                "標準モデルのトイレ設備は20基で上限化されています。追加設備を手動指定してください。"
            )
            toilets = 20
        facilities: list[FacilitySpec] = [
            FacilitySpec(
                id=f"accessible_toilet_{index:02d}",
                kind="toilet",
                zone_id="toilet",
                capacity=1,
                working_units=1,
            )
            for index in range(1, min(request.accessible_toilet_count, toilets) + 1)
        ]
        facilities.extend(
            FacilitySpec(
                id=f"toilet_{index:02d}",
                kind="toilet",
                zone_id="toilet",
                capacity=1,
                working_units=1,
            )
            for index in range(1, toilets - min(request.accessible_toilet_count, toilets) + 1)
        )
        facilities.extend(
            (
                *_service_stations("water_station", counts["water_station"]),
                *_service_stations("meal_distribution", counts["meal_distribution"]),
                FacilitySpec(
                    id="medical_desk", kind="medical_support", zone_id="medical", capacity=1
                ),
                FacilitySpec(id="intake_desk", kind="intake", zone_id="intake", capacity=1),
                FacilitySpec(id="quiet_support", kind="quiet_space", zone_id="quiet", capacity=1),
            )
        )
        optional_facilities = {
            "pet": FacilitySpec(id="pet_area", kind="pet_support", zone_id="pet", capacity=1),
            "family": FacilitySpec(
                id="family_space", kind="family_support", zone_id="family", capacity=1
            ),
            "private": FacilitySpec(
                id="privacy_space", kind="privacy_support", zone_id="private", capacity=1
            ),
        }
        facilities.extend(
            item
            for zone_id, _, _ in zone_design
            if (item := optional_facilities.get(zone_id)) is not None
        )
        facilities.extend(
            FacilitySpec(
                id=condition.facility_id,
                kind=condition.facility_kind,
                zone_id=condition.zone_id,
                capacity=condition.capacity,
            )
            for condition in request.zone_conditions
            if condition.action == "include" and condition.facility_id is not None
        )
        if len({item.id for item in facilities}) != len(facilities):
            raise ValueError(
                "custom facility IDs must not collide with generated standard facilities"
            )
        equipment_overrides = {item.facility_id: item for item in request.equipment}
        facility_ids = {item.id for item in facilities}
        for condition in request.equipment:
            if condition.facility_id not in facility_ids and condition.add:
                if condition.zone_id not in {item[0] for item in zone_design}:
                    raise ValueError(
                        f"new equipment {condition.facility_id!r} references an unknown standard zone"
                    )
                facilities.append(
                    FacilitySpec(
                        id=condition.facility_id,
                        kind=condition.facility_kind or "equipment",
                        zone_id=condition.zone_id or "staff",
                        capacity=condition.capacity,
                        working_units=condition.working_units,
                    )
                )
                facility_ids.add(condition.facility_id)
            elif condition.facility_id not in facility_ids:
                warnings.append(
                    f"設備条件 {condition.facility_id!r} は標準生成設備にないため適用できません。"
                )
        facilities = [
            FacilitySpec(
                id=item.id,
                kind=item.kind,
                zone_id=item.zone_id,
                capacity=equipment_overrides[item.id].capacity,
                working_units=equipment_overrides[item.id].working_units,
            )
            if item.id in equipment_overrides
            else item
            for item in facilities
        ]
        facilities.sort(key=lambda item: item.id)
        facility_points = _place_facilities(facilities, physical_zones)
        physical_zone_by_id = {item.zone_id: item for item in physical_zones}
        for item in request.equipment:
            if item.position_m is None or item.facility_id not in facility_points:
                continue
            zone = physical_zone_by_id[
                next(facility.zone_id for facility in facilities if facility.id == item.facility_id)
            ]
            if not point_in_polygon(item.position_m, zone.polygon):
                raise ValueError(
                    f"equipment {item.facility_id!r} position lies outside its assigned zone"
                )
            facility_points[item.facility_id] = item.position_m

        # Water and meal service start only with skilled staff in the food zone, so each
        # station gets its own station-based staff member next to the general staff.
        station_staff_needed = counts["water_station"] + counts["meal_distribution"]
        if request.staff_count is None:
            general_staff_count = max(1, math.ceil(request.population / 100))
            station_staff_count = station_staff_needed
        else:
            station_staff_count = min(station_staff_needed, max(0, request.staff_count - 1))
            general_staff_count = request.staff_count - station_staff_count
        staff_count = general_staff_count + station_staff_count
        staff_skills = (
            "support",
            "intake",
            "information",
            "water_service",
            "meal_service",
            "medical_support",
        )
        staff = tuple(
            StaffSpec(id=f"staff_{index:03d}", zone_id="staff", skills=staff_skills)
            for index in range(1, general_staff_count + 1)
        ) + tuple(
            StaffSpec(
                id=f"staff_{index:03d}", zone_id="food", skills=("water_service", "meal_service")
            )
            for index in range(general_staff_count + 1, staff_count + 1)
        )

        edges: list[ZoneEdgeSpec] = []
        routes: list[PhysicalRoute] = []
        for zone_id, _, share in zone_design:
            zone_center = zone_centers[zone_id]
            corridor_point = zone_centers["corridor"]
            doorway_point = Point2D(x=zone_center.x, y=corridor_depth_m)
            route_width = min(1.8, corridor_depth_m, width_m * share)
            # Cross the partition perpendicularly before turning along the corridor.
            # A direct diagonal from the doorway to the corridor centre can require
            # more opening width than this route declares, especially in small plans.
            corridor_approach = Point2D(x=zone_center.x, y=corridor_point.y)
            candidates = (zone_center, doorway_point, corridor_approach, corridor_point)
            route_points = (
                candidates[0],
                *(
                    point
                    for previous, point in zip(candidates, candidates[1:])
                    if point != previous
                ),
            )
            for source, target in ((zone_id, "corridor"), ("corridor", zone_id)):
                edge_id = f"route_{source}_{target}"
                points = route_points if source == zone_id else tuple(reversed(route_points))
                distance_m = sum(
                    math.hypot(right.x - left.x, right.y - left.y)
                    for left, right in zip(points, points[1:])
                )
                edges.append(
                    ZoneEdgeSpec(
                        id=edge_id,
                        from_zone_id=source,
                        to_zone_id=target,
                        travel_minutes=max(1, math.ceil(distance_m / 60.0)),
                    )
                )
                routes.append(
                    PhysicalRoute(
                        edge_id=edge_id,
                        points=points,
                        width_m=route_width,
                        accessible=route_width >= ACCESSIBLE_ROUTE_MIN_WIDTH_M,
                    )
                )
        edges.sort(key=lambda edge: edge.id)
        routes.sort(key=lambda route: route.edge_id)
        geometry = FacilityGeometry(
            width_m=width_m,
            height_m=height_m,
            source_kind=source_kind,
            source_ids=sources,
            zones=tuple(physical_zones),
            facility_points=facility_points,
            routes=tuple(routes),
        )
        editable_geometry = _standard_editable_geometry(
            width_m=width_m,
            height_m=height_m,
            corridor_depth_m=corridor_depth_m,
            zone_design=zone_design,
            routes=tuple(routes),
            route_widths={route.edge_id: route.width_m for route in routes},
            source_ids=geometry_source_ids,
        )
        footprint_values = _equipment_footprints(
            request.equipment,
            facilities=facilities,
            facility_points=facility_points,
            zones=physical_zone_by_id,
            source_ids=geometry_source_ids,
        )
        editable_geometry = editable_geometry.model_copy(
            update={"equipment_footprints": footprint_values}
        )
        geometry = with_legacy_service_points(geometry, tuple(facilities))
        validate_editable_routes(tuple(routes), editable_geometry, physical_zones=geometry.zones)
        service_point_issues = validate_facility_service_points(
            geometry, tuple(facilities), tuple(edges), editable_geometry
        )
        if service_point_issues:
            raise ValueError(service_point_issues[0].message)
        observations: list[dict[str, object]] = []
        if request.gross_area_m2 is not None or area_source_ids:
            observations.append(
                {
                    "subject": "facility_gross_area_m2",
                    "value": gross_area,
                    "source_ids": list(area_source_ids),
                    "basis": "operator_entered"
                    if "SRC-OPERATOR-AREA" in area_source_ids
                    else "source_record",
                }
            )
        if request.stated_capacity is not None:
            observations.append(
                {
                    "subject": "published_facility_capacity",
                    "value": request.stated_capacity,
                    "source_id": request.capacity_source_ids[0],
                    "description": "Copied from the selected shelter record; current designation and applicability require confirmation.",
                }
            )
        assumptions = (
            {
                "id": SYNTHETIC_FACILITY_SOURCE_ID,
                "subject": "standard_shelter_layout",
                "value": {
                    "gross_area_m2": gross_area,
                    "area_basis": (
                        "operator_entered"
                        if "SRC-OPERATOR-AREA" in area_source_ids
                        else "sourced_area"
                        if area_source_ids
                        else "planning_heuristic"
                    ),
                    "sleeping_usable_area_m2": sleeping_usable_area_m2,
                    "corridor_depth_m": corridor_depth_m,
                    "aspect_ratio": request.aspect_ratio,
                    "zone_shares": dict((key, share) for key, _, share in zone_design),
                    "facility_conditions": list(request.facility_conditions),
                    "typed_zone_conditions": [
                        item.model_dump(mode="json") for item in request.zone_conditions
                    ],
                    "target_accessible_route_width_m": 1.8,
                    "minimum_zone_frontage_m": ACCESSIBLE_ROUTE_MIN_WIDTH_M,
                    "frontage_widened_zones": widened_zones,
                    "minimum_generated_route_width_m": min(route.width_m for route in routes),
                    "equipment_overrides": [
                        item.model_dump(mode="json") for item in request.equipment
                    ],
                    "route_speed_m_per_minute": 60,
                },
                "reason": "Generic rectangular synthetic model for editing; no official or real facility floor plan supplied.",
                "source_id": SYNTHETIC_FACILITY_SOURCE_ID,
            },
            {
                "id": SYNTHETIC_CAPACITY_SOURCE_ID,
                "subject": "planning_occupancy_density",
                "value": {
                    "area_density_m2_per_person": 3.5,
                    "sleeping_usable_area_m2": sleeping_usable_area_m2,
                    "area_based_capacity": area_capacity,
                    "capacity_used": capacity_supported,
                },
                "reason": "Usable sleeping area divided by a visible 3.5 m²/person planning heuristic; not a legal code or suitability determination. When a source capacity is supplied, the lower of the two values is used.",
                "source_id": SYNTHETIC_CAPACITY_SOURCE_ID,
            },
            {
                "id": SYNTHETIC_STAFFING_SOURCE_ID,
                "subject": "synthetic_staff_count",
                "value": {
                    "total": staff_count,
                    "general_staff": general_staff_count,
                    "station_staff": station_staff_count,
                    "station_staff_zone_id": "food",
                    "entered_total": request.staff_count,
                },
                "reason": "One general staff member per 100 requested occupants (minimum one) plus one station-based staff member per water or meal station; an entered total keeps at least one general staff member. Not an operational staffing standard.",
                "source_id": SYNTHETIC_STAFFING_SOURCE_ID,
            },
            {
                "id": SYNTHETIC_TOILET_SOURCE_ID,
                "subject": "synthetic_toilet_count",
                "value": {
                    "accessible_toilets": request.accessible_toilet_count,
                    "planning_standard_units": counts["toilet_standard"],
                    "demand_units": counts["toilet_demand"],
                    "total": toilets,
                    "maximum": 20,
                },
                "reason": "The larger of the entered accessible toilets plus one per 50 occupants (Cabinet Office initial planning figure) and the units the modeled daily use needs at the target load, capped at 20.",
                "source_id": SYNTHETIC_TOILET_SOURCE_ID,
            },
            {
                "id": SERVICE_STATION_SOURCE_ID,
                "subject": "synthetic_service_station_count",
                "value": {
                    "water_station": counts["water_station"],
                    "meal_distribution": counts["meal_distribution"],
                },
                "reason": "Units that keep the modeled daily water and meal demand within the target load (at least one each); no public standard sets station counts.",
                "source_id": SERVICE_STATION_SOURCE_ID,
            },
        )
        return FacilityDesign(
            zones=tuple(zones),
            zone_edges=tuple(edges),
            facilities=tuple(facilities),
            staff=staff,
            geometry=geometry,
            editable_geometry=editable_geometry,
            capacity_supported=capacity_supported,
            requested_population=request.population,
            observations=tuple(observations),
            assumptions=assumptions,
            warnings=tuple(warnings),
        )


def standard_service_counts(population: int) -> dict[str, int]:
    """Return default unit counts that meet the planning standard and modeled demand.

    Demand uses the default generated coefficients; a Scenario whose coefficients are
    changed later is checked by service_capacity_diagnostic instead.
    """
    from sheltersim.experiments.sanitation_diagnostics import (
        PEOPLE_PER_TOILET,
        PLANNING_TOILET_USES_PER_PERSON_DAY,
    )
    from sheltersim.experiments.scenario import PsychologyModelConfig, ToiletServiceConfig
    from sheltersim.experiments.service_capacity import (
        required_service_units,
        uses_per_person_day,
    )
    from sheltersim.region.conditions import default_resource_service_config

    toilet = ToiletServiceConfig()
    resource = default_resource_service_config()
    water_uses = uses_per_person_day(
        resource.hydration_growth_per_minute,
        relief=resource.hydration_relief,
        action_threshold=resource.hydration_action_threshold,
    )
    meal_uses = uses_per_person_day(
        PsychologyModelConfig().hunger_growth,
        relief=resource.hunger_relief,
        action_threshold=resource.hunger_action_threshold,
    )
    return {
        "toilet_standard": math.ceil(population / PEOPLE_PER_TOILET["initial"]),
        "toilet_demand": required_service_units(
            population
            * PLANNING_TOILET_USES_PER_PERSON_DAY
            * (toilet.minimum_minutes + toilet.maximum_minutes)
            / 2
        ),
        "water_station": max(
            1, required_service_units(population * water_uses * resource.service_minutes)
        ),
        "meal_distribution": max(
            1, required_service_units(population * meal_uses * resource.service_minutes)
        ),
    }


def _service_stations(kind: str, count: int) -> tuple[FacilitySpec, ...]:
    # The first station keeps its historical ID so single-station plans stay recognizable.
    return tuple(
        FacilitySpec(
            id=f"{kind}_main" if index == 1 else f"{kind}_{index:02d}",
            kind=kind,
            zone_id="food",
            capacity=1,
        )
        for index in range(1, count + 1)
    )


def _default_gross_area(
    population: int, *, aspect_ratio: float, zone_design: list[tuple[str, str, float]]
) -> float:
    """Provide enough heuristic sleeping area and doorway frontage by default."""
    requested_sleeping_share = next(
        share for zone_id, _, share in zone_design if zone_id == "sleeping"
    )
    gross_area = max(80.0, population * 3.5 / max(0.01, requested_sleeping_share * 0.9) * 1.1)
    for _ in range(160):
        # Round first so the checked plan is the one the generator builds.
        rounded_area = math.ceil(gross_area / 10.0) * 10.0
        width_m = math.sqrt(rounded_area * aspect_ratio)
        height_m = rounded_area / width_m
        corridor_depth_m = min(1.8, height_m * 0.25)
        design = _with_accessible_frontage(zone_design, width_m)
        sleeping_share = next(share for zone_id, _, share in design if zone_id == "sleeping")
        sleeping_usable_area = (
            rounded_area * sleeping_share * (height_m - corridor_depth_m) / height_m * 0.9
        )
        frontage_met = (
            min(width_m * share for _, _, share in design) >= ACCESSIBLE_ROUTE_MIN_WIDTH_M - 1e-9
        )
        if frontage_met and math.floor(sleeping_usable_area / 3.5) >= population:
            return rounded_area
        gross_area *= 1.05
    return math.ceil(gross_area / 10.0) * 10.0


def _with_accessible_frontage(
    zone_design: list[tuple[str, str, float]], width_m: float
) -> list[tuple[str, str, float]]:
    """Widen narrow rooms to an accessible doorway, taking the area from the sleeping zone.

    Rooms sit side by side along the corridor, so a room's frontage caps its doorway.
    When the building is too narrow to give every room the minimum, the requested
    shares are kept and the existing accessibility blocker reports the gap.
    """
    minimum_share = ACCESSIBLE_ROUTE_MIN_WIDTH_M / width_m
    widened = [
        (zone_id, label, share if zone_id == "sleeping" else max(share, minimum_share))
        for zone_id, label, share in zone_design
    ]
    others = sum(share for zone_id, _, share in widened if zone_id != "sleeping")
    sleeping_share = 1.0 - others
    if sleeping_share < minimum_share:
        return list(zone_design)
    return [
        (zone_id, label, sleeping_share if zone_id == "sleeping" else share)
        for zone_id, label, share in widened
    ]


def _apply_zone_area_shares(
    zone_design: list[tuple[str, str, float]], overrides: dict[str, float]
) -> list[tuple[str, str, float]]:
    if any(value <= 0 for value in overrides.values()):
        raise ValueError("every explicitly allocated standard zone must have a positive area share")
    allocated = sum(overrides.values())
    unspecified = [item for item in zone_design if item[0] not in overrides]
    remaining = max(0.0, 1.0 - allocated)
    if unspecified and remaining <= 1e-9:
        raise ValueError("zone area shares must leave positive area for every unspecified zone")
    if not unspecified and abs(remaining) > 1e-8:
        raise ValueError("zone area shares must total 1 when every standard zone is specified")
    base_total = sum(item[2] for item in unspecified)
    if unspecified and base_total <= 0 and remaining > 0:
        raise ValueError("unallocated standard zones have no remaining area weight")
    return [
        (
            zone_id,
            label,
            overrides[zone_id] if zone_id in overrides else remaining * share / base_total,
        )
        for zone_id, label, share in zone_design
    ]


def _apply_zone_conditions(
    zone_design: list[tuple[str, str, float]],
    conditions: tuple[FacilityZoneCondition, ...],
) -> list[tuple[str, str, float]]:
    """Apply typed optional-zone edits while preserving the total floor-area budget."""
    result = list(zone_design)
    required_zone_ids = {"sleeping", "quiet", "medical", "food", "toilet", "intake", "staff"}
    for condition in conditions:
        if condition.zone_id == "corridor" and condition.action == "include":
            raise ValueError(
                "the shared corridor is generated separately and cannot be redefined as a room"
            )
        existing_index = next(
            (index for index, item in enumerate(result) if item[0] == condition.zone_id), None
        )
        sleeping_index = next(index for index, item in enumerate(result) if item[0] == "sleeping")
        sleeping_id, sleeping_name, sleeping_share = result[sleeping_index]
        if condition.action == "exclude":
            if existing_index is None:
                raise ValueError(f"cannot exclude unknown standard zone {condition.zone_id!r}")
            if condition.zone_id in required_zone_ids:
                raise ValueError(
                    f"required standard shelter zone {condition.zone_id!r} cannot be removed"
                )
            removed_share = result.pop(existing_index)[2]
            sleeping_index = next(
                index for index, item in enumerate(result) if item[0] == "sleeping"
            )
            key, label, share = result[sleeping_index]
            result[sleeping_index] = (key, label, share + removed_share)
            continue

        assert condition.area_share is not None
        target_share = condition.area_share
        if existing_index is None:
            updated_sleeping_share = sleeping_share - target_share
            if updated_sleeping_share <= 0:
                raise ValueError("included facility zones must leave positive sleeping area")
            result[sleeping_index] = (sleeping_id, sleeping_name, updated_sleeping_share)
            result.append((condition.zone_id, condition.name or condition.zone_id, target_share))
        else:
            _zone_id, old_name, old_share = result[existing_index]
            delta = target_share - old_share
            updated_sleeping_share = sleeping_share - delta
            if updated_sleeping_share <= 0:
                raise ValueError("facility zone resize must leave positive sleeping area")
            result[sleeping_index] = (sleeping_id, sleeping_name, updated_sleeping_share)
            result[existing_index] = (
                condition.zone_id,
                condition.name or old_name,
                target_share,
            )
    if abs(sum(item[2] for item in result) - 1.0) > 1e-8 or any(item[2] <= 0 for item in result):
        raise ValueError("facility zone changes must preserve positive shares totaling one")
    return result


def _place_facilities(
    facilities: list[FacilitySpec], physical_zones: list[PhysicalZone]
) -> dict[str, Point2D]:
    zone_by_id = {item.zone_id: item for item in physical_zones}
    groups: dict[str, list[FacilitySpec]] = {}
    for facility in facilities:
        groups.setdefault(facility.zone_id, []).append(facility)
    positions: dict[str, Point2D] = {}
    for zone_id, items in groups.items():
        zone = zone_by_id[zone_id]
        for index, facility in enumerate(sorted(items, key=lambda item: item.id)):
            positions[facility.id] = facility_grid_point(zone.polygon, index, len(items))
    return positions


def facility_grid_point(polygon: tuple[Point2D, ...], index: int, count: int) -> Point2D:
    """Slot ``index`` of ``count`` facilities in a grid near the far wall of a zone."""
    min_x = min(point.x for point in polygon)
    max_x = max(point.x for point in polygon)
    min_y = min(point.y for point in polygon)
    max_y = max(point.y for point in polygon)
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    column = index % columns
    row = index // columns
    x_fraction = 0.35 if count == 1 else (column + 1) / (columns + 1)
    y_fraction = 0.68 + 0.28 * (row + 1) / (rows + 1)
    return Point2D(
        x=min_x + (max_x - min_x) * x_fraction,
        y=min_y + (max_y - min_y) * y_fraction,
    )


def _standard_editable_geometry(
    *,
    width_m: float,
    height_m: float,
    corridor_depth_m: float,
    zone_design: list[tuple[str, str, float]],
    routes: tuple[PhysicalRoute, ...],
    route_widths: dict[str, float],
    source_ids: tuple[str, ...],
) -> EditableFacilityGeometry:
    zero = Point2D(x=0, y=0)
    top_right = Point2D(x=width_m, y=0)
    bottom_right = Point2D(x=width_m, y=height_m)
    bottom_left = Point2D(x=0, y=height_m)
    corridor_wall_start = Point2D(x=0, y=corridor_depth_m)
    corridor_wall_end = Point2D(x=width_m, y=corridor_depth_m)
    walls = [
        FacilityWall(wall_id="exterior_north", start=zero, end=top_right, source_ids=source_ids),
        FacilityWall(
            wall_id="exterior_east", start=top_right, end=bottom_right, source_ids=source_ids
        ),
        FacilityWall(
            wall_id="exterior_south", start=bottom_right, end=bottom_left, source_ids=source_ids
        ),
        FacilityWall(wall_id="exterior_west", start=bottom_left, end=zero, source_ids=source_ids),
        FacilityWall(
            wall_id="wall_corridor_partition",
            start=corridor_wall_start,
            end=corridor_wall_end,
            source_ids=source_ids,
        ),
    ]
    openings: list[FacilityOpening] = []
    route_openings: list[RouteOpeningUse] = []
    x_cursor = 0.0
    for index, (zone_id, _, share) in enumerate(zone_design):
        zone_width = width_m * share
        if index > 0:
            x = x_cursor
            walls.append(
                FacilityWall(
                    wall_id=f"partition_zone_{index:02d}",
                    start=Point2D(x=x, y=corridor_depth_m),
                    end=Point2D(x=x, y=height_m),
                    source_ids=source_ids,
                )
            )
        center_x = x_cursor + zone_width / 2
        route_width = route_widths[f"route_{zone_id}_corridor"]
        opening_id = f"opening_{zone_id}_corridor"
        half_width = route_width / 2
        openings.append(
            FacilityOpening(
                opening_id=opening_id,
                wall_id="wall_corridor_partition",
                start=Point2D(x=max(0.0, center_x - half_width), y=corridor_depth_m),
                end=Point2D(x=min(width_m, center_x + half_width), y=corridor_depth_m),
                width_m=route_width,
                opening_kind="doorway",
                source_ids=source_ids,
            )
        )
        route_openings.extend(
            RouteOpeningUse(route_id=route.edge_id, opening_id=opening_id)
            for route in routes
            if route.edge_id in {f"route_{zone_id}_corridor", f"route_corridor_{zone_id}"}
        )
        x_cursor += zone_width
    return EditableFacilityGeometry(
        width_m=width_m,
        height_m=height_m,
        walls=tuple(walls),
        openings=tuple(openings),
        route_openings=tuple(route_openings),
    )


def _point_on_segment(point: Point2D, start: Point2D, end: Point2D) -> bool:
    cross = (point.x - start.x) * (end.y - start.y) - (point.y - start.y) * (end.x - start.x)
    within = (
        min(start.x, end.x) - 1e-8 <= point.x <= max(start.x, end.x) + 1e-8
        and min(start.y, end.y) - 1e-8 <= point.y <= max(start.y, end.y) + 1e-8
    )
    return abs(cross) <= 1e-8 and within


def _valid_simple_polygon(polygon: tuple[Point2D, ...]) -> bool:
    if (
        len(polygon) < 3
        or abs(sum(a.x * b.y - b.x * a.y for a, b in zip(polygon, polygon[1:] + polygon[:1])))
        <= 1e-9
    ):
        return False
    edges = tuple(zip(polygon, polygon[1:] + polygon[:1]))
    for index, (start, end) in enumerate(edges):
        for other_index, (other_start, other_end) in enumerate(edges[index + 1 :], index + 1):
            if other_index in {index + 1, (index - 1) % len(edges)}:
                continue
            if _segment_intersection_points(start, end, other_start, other_end):
                return False
    return True


def _point_on_polygon_boundary(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    return any(
        _point_on_segment(point, start, end)
        for start, end in zip(polygon, polygon[1:] + polygon[:1])
    )


def _point_strictly_inside_polygon(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    return point_in_polygon(point, polygon) and not _point_on_polygon_boundary(point, polygon)


def _properly_crosses(
    first_start: Point2D, first_end: Point2D, second_start: Point2D, second_end: Point2D
) -> bool:
    return any(
        all(
            math.hypot(point.x - endpoint.x, point.y - endpoint.y) > 1e-8
            for endpoint in (first_start, first_end, second_start, second_end)
        )
        for point in _segment_intersection_points(first_start, first_end, second_start, second_end)
    )


def _validate_physical_zone_polygons(zones: tuple[PhysicalZone, ...]) -> None:
    for zone in zones:
        if not _valid_simple_polygon(zone.polygon):
            raise ValueError(
                f"walkable zone {zone.zone_id!r} must have a simple positive-area polygon"
            )
    for index, first in enumerate(zones):
        first_polygon = first.polygon
        for second in zones[index + 1 :]:
            second_polygon = second.polygon
            same_vertices = {(point.x, point.y) for point in first_polygon} == {
                (point.x, point.y) for point in second_polygon
            }
            contained_vertex = any(
                _point_strictly_inside_polygon(point, second_polygon) for point in first_polygon
            ) or any(
                _point_strictly_inside_polygon(point, first_polygon) for point in second_polygon
            )
            crossing_edges = any(
                _properly_crosses(start, end, other_start, other_end)
                for start, end in zip(first_polygon, first_polygon[1:] + first_polygon[:1])
                for other_start, other_end in zip(
                    second_polygon, second_polygon[1:] + second_polygon[:1]
                )
            )
            if same_vertices or contained_vertex or crossing_edges:
                raise ValueError(
                    f"walkable zones {first.zone_id!r} and {second.zone_id!r} overlap in area"
                )


def _equipment_footprints(
    equipment: tuple[FacilityEquipmentCondition, ...],
    *,
    facilities: list[FacilitySpec],
    facility_points: dict[str, Point2D],
    zones: dict[str, PhysicalZone],
    source_ids: tuple[str, ...],
) -> tuple[FacilityEquipmentFootprint, ...]:
    facility_by_id = {item.id: item for item in facilities}
    footprints: list[FacilityEquipmentFootprint] = []
    rectangles: list[tuple[str, float, float, float, float]] = []
    for item in equipment:
        if item.footprint_m2 is None:
            continue
        facility = facility_by_id.get(item.facility_id)
        if facility is None:
            continue
        zone = zones[facility.zone_id]
        point = facility_points[facility.id]
        min_x = min(value.x for value in zone.polygon)
        max_x = max(value.x for value in zone.polygon)
        min_y = min(value.y for value in zone.polygon)
        max_y = max(value.y for value in zone.polygon)
        side = math.sqrt(item.footprint_m2)
        if side > max_x - min_x or side > max_y - min_y:
            raise ValueError(
                f"equipment {item.facility_id!r} footprint cannot fit inside its assigned zone"
            )
        left = min(max(point.x - side / 2, min_x), max_x - side)
        top = min(max(point.y - side / 2, min_y), max_y - side)
        polygon = (
            Point2D(x=left, y=top),
            Point2D(x=left + side, y=top),
            Point2D(x=left + side, y=top + side),
            Point2D(x=left, y=top + side),
        )
        if not all(point_in_polygon(vertex, zone.polygon) for vertex in polygon):
            raise ValueError(
                f"equipment {item.facility_id!r} footprint crosses its assigned zone boundary"
            )
        for other_id, other_left, other_top, other_right, other_bottom in rectangles:
            if (
                left < other_right
                and left + side > other_left
                and top < other_bottom
                and top + side > other_top
            ):
                raise ValueError(
                    f"equipment footprints {item.facility_id!r} and {other_id!r} overlap"
                )
        rectangles.append((item.facility_id, left, top, left + side, top + side))
        footprints.append(
            FacilityEquipmentFootprint(
                facility_id=item.facility_id,
                polygon=polygon,
                area_m2=item.footprint_m2,
                source_ids=tuple(sorted(set(source_ids) | {item.source_id})),
            )
        )
    return tuple(footprints)


def validate_editable_routes(
    routes: tuple[PhysicalRoute, ...],
    geometry: EditableFacilityGeometry,
    *,
    physical_zones: tuple[PhysicalZone, ...] = (),
) -> None:
    """Validate route polylines against walkable polygons, wall openings and clearance."""
    if physical_zones:
        _validate_physical_zone_polygons(physical_zones)
    openings = {item.opening_id: item for item in geometry.openings}
    walls = {item.wall_id: item for item in geometry.walls}
    route_openings: dict[str, set[str]] = {}
    route_ids = {item.edge_id for item in routes}
    for item in geometry.route_openings:
        if item.route_id not in route_ids:
            raise ValueError(f"opening mapping references unknown route {item.route_id!r}")
        if item.opening_id not in openings:
            raise ValueError(
                f"route opening mapping references unknown opening {item.opening_id!r}"
            )
        route_openings.setdefault(item.route_id, set()).add(item.opening_id)
    for route in routes:
        if any(
            not 0 <= point.x <= geometry.width_m or not 0 <= point.y <= geometry.height_m
            for point in route.points
        ):
            raise ValueError(f"route {route.edge_id!r} leaves the facility bounds")
        if any(start == end for start, end in zip(route.points, route.points[1:])):
            raise ValueError(f"route {route.edge_id!r} contains a zero-length segment")
        if route.accessible and route.width_m < ACCESSIBLE_ROUTE_MIN_WIDTH_M:
            raise ValueError(
                f"accessible route {route.edge_id!r} is narrower than "
                f"{ACCESSIBLE_ROUTE_MIN_WIDTH_M:g} m"
            )
        for opening_id in route_openings.get(route.edge_id, ()):
            opening = openings[opening_id]
            if route.width_m > opening.width_m + 1e-8:
                raise ValueError(f"route {route.edge_id!r} is wider than opening {opening_id!r}")
        for start, end in zip(route.points, route.points[1:]):
            if physical_zones and not _segment_inside_polygon_union(start, end, physical_zones):
                raise ValueError(
                    f"route {route.edge_id!r} leaves the facility's walkable zone polygons"
                )
            if physical_zones and not _segment_has_polygon_clearance(
                start, end, route.width_m / 2, physical_zones
            ):
                raise ValueError(
                    f"route {route.edge_id!r} does not fit within the walkable polygon boundary clearance"
                )
            for wall in geometry.walls:
                intersections = _segment_intersection_points(start, end, wall.start, wall.end)
                if not intersections:
                    clearance = _segment_distance(start, end, wall.start, wall.end)
                    if clearance < route.width_m / 2 - 1e-8:
                        raise ValueError(
                            f"route {route.edge_id!r} lacks width clearance from wall {wall.wall_id!r}"
                        )
                    continue
                valid_openings = [
                    opening
                    for opening in geometry.openings
                    if opening.wall_id == wall.wall_id
                    and opening.opening_id in route_openings.get(route.edge_id, set())
                    and all(
                        _point_on_segment(point, opening.start, opening.end)
                        for point in intersections
                    )
                ]
                if not valid_openings:
                    raise ValueError(
                        f"route {route.edge_id!r} crosses closed wall {wall.wall_id!r}"
                    )
                route_dx, route_dy = end.x - start.x, end.y - start.y
                wall_dx, wall_dy = wall.end.x - wall.start.x, wall.end.y - wall.start.y
                route_length = math.hypot(route_dx, route_dy)
                wall_length = math.hypot(wall_dx, wall_dy)
                crossing_sine = abs(route_dx * wall_dy - route_dy * wall_dx) / (
                    route_length * wall_length
                )
                if crossing_sine <= 1e-9:
                    raise ValueError(
                        f"route {route.edge_id!r} follows wall {wall.wall_id!r} instead of crossing it"
                    )
                required_opening = route.width_m / crossing_sine
                if not any(
                    opening.width_m + 1e-8 >= required_opening for opening in valid_openings
                ):
                    raise ValueError(
                        f"route {route.edge_id!r} is too oblique for the available opening in wall {wall.wall_id!r}"
                    )
            for obstacle in geometry.obstacles:
                if (
                    _segment_distance_to_polygon(start, end, obstacle.polygon)
                    < route.width_m / 2 - 1e-8
                ):
                    raise ValueError(
                        f"route {route.edge_id!r} lacks width clearance from obstacle {obstacle.obstacle_id!r}"
                    )
            for footprint in geometry.equipment_footprints:
                if (
                    _segment_distance_to_polygon(start, end, footprint.polygon)
                    < route.width_m / 2 - 1e-8
                ):
                    raise ValueError(
                        f"route {route.edge_id!r} lacks width clearance from equipment footprint "
                        f"{footprint.facility_id!r}"
                    )


def validate_facility_walkability(geometry: FacilityGeometry, raw_constraints: object) -> None:
    """Validate the shared FacilityGeometry + SpatialConstraints pair for Chat 2 consumers."""
    from sheltersim.contracts.spatial import SpatialConstraints

    constraints = (
        raw_constraints
        if isinstance(raw_constraints, SpatialConstraints)
        else SpatialConstraints.model_validate(raw_constraints)
    )
    walls = {item.id: item for item in constraints.walls}
    route_ids = {route.edge_id for route in geometry.routes}
    for opening in constraints.openings:
        if opening.wall_id not in walls:
            raise ValueError(f"opening {opening.id!r} references unknown wall {opening.wall_id!r}")
        if not set(opening.edge_ids) <= route_ids:
            raise ValueError(f"opening {opening.id!r} references an unknown route edge")
    editable = EditableFacilityGeometry(
        width_m=geometry.width_m,
        height_m=geometry.height_m,
        walls=tuple(
            FacilityWall(
                wall_id=item.id, start=item.start, end=item.end, source_ids=item.source_ids
            )
            for item in constraints.walls
        ),
        openings=tuple(
            FacilityOpening(
                opening_id=item.id,
                wall_id=item.wall_id,
                start=_opening_endpoint(
                    item.center,
                    walls[item.wall_id].start,
                    walls[item.wall_id].end,
                    -1,
                    item.width_m,
                ),
                end=_opening_endpoint(
                    item.center, walls[item.wall_id].start, walls[item.wall_id].end, 1, item.width_m
                ),
                width_m=item.width_m,
                source_ids=item.source_ids,
            )
            for item in constraints.openings
        ),
        obstacles=tuple(
            FacilityObstacle(obstacle_id=item.id, polygon=item.polygon, source_ids=item.source_ids)
            for item in constraints.obstacles
        ),
        route_openings=tuple(
            RouteOpeningUse(route_id=edge_id, opening_id=item.id)
            for item in constraints.openings
            for edge_id in item.edge_ids
        ),
        equipment_footprints=tuple(
            FacilityEquipmentFootprint(
                facility_id=item.facility_id,
                polygon=item.polygon,
                area_m2=item.area_m2,
                source_ids=item.source_ids,
            )
            for item in constraints.equipment_footprints
        ),
    )
    validate_editable_routes(geometry.routes, editable, physical_zones=geometry.zones)


def with_legacy_service_points(
    geometry: FacilityGeometry, facilities: tuple[FacilitySpec, ...]
) -> FacilityGeometry:
    """Keep one stable compatibility service point for each otherwise-unmapped facility."""
    mapped_facility_ids = {item.facility_id for item in geometry.service_points}
    missing = tuple(item for item in facilities if item.id not in mapped_facility_ids)
    if not missing:
        return geometry
    defaults = legacy_service_points(geometry, missing)
    return FacilityGeometry.model_validate(
        geometry.model_dump(mode="python")
        | {"service_points": (*geometry.service_points, *defaults)}
    )


def validate_facility_physical_layout(
    geometry: FacilityGeometry,
    facilities: tuple[FacilitySpec, ...],
    zone_edges: tuple[ZoneEdgeSpec, ...],
    editable_geometry: EditableFacilityGeometry,
) -> None:
    """Reject inconsistent imported or edited floor plans before Scenario mapping."""
    if not math.isclose(
        geometry.width_m, editable_geometry.width_m, rel_tol=0, abs_tol=1e-9
    ) or not math.isclose(geometry.height_m, editable_geometry.height_m, rel_tol=0, abs_tol=1e-9):
        raise ValueError("walkable and editable plan geometry must use the same metric dimensions")
    validate_editable_routes(geometry.routes, editable_geometry, physical_zones=geometry.zones)
    _validate_physical_zone_polygons(geometry.zones)
    if (
        sum(zone.usable_area_m2 for zone in geometry.zones)
        > geometry.width_m * geometry.height_m + 1e-6
    ):
        raise ValueError("summed usable zone area exceeds gross building area")
    zone_by_id = {item.zone_id: item for item in geometry.zones}
    facility_by_id = {item.id: item for item in facilities}
    if set(geometry.facility_points) != set(facility_by_id):
        raise ValueError("physical geometry must position every facility exactly once")
    for facility in facilities:
        zone = zone_by_id.get(facility.zone_id)
        point = geometry.facility_points.get(facility.id)
        if zone is None or point is None or not point_in_polygon(point, zone.polygon):
            raise ValueError(f"facility {facility.id!r} must be inside its assigned physical zone")
        if any(
            point_in_polygon(point, obstacle.polygon) for obstacle in editable_geometry.obstacles
        ):
            raise ValueError(f"facility {facility.id!r} position overlaps a blocked obstacle")
    footprint_by_id = {item.facility_id: item for item in editable_geometry.equipment_footprints}
    unknown_footprints = set(footprint_by_id) - set(facility_by_id)
    if unknown_footprints:
        raise ValueError(
            "equipment footprint references unknown facility " + sorted(unknown_footprints)[0]
        )
    for footprint in editable_geometry.equipment_footprints:
        facility = facility_by_id[footprint.facility_id]
        zone = zone_by_id.get(facility.zone_id)
        if zone is None or not all(
            point_in_polygon(vertex, zone.polygon) for vertex in footprint.polygon
        ):
            raise ValueError(f"equipment footprint {facility.id!r} leaves its assigned zone")
        if not point_in_polygon(geometry.facility_points[facility.id], footprint.polygon):
            raise ValueError(
                f"equipment footprint {facility.id!r} must contain its facility position"
            )
        if any(
            _polygons_overlap(footprint.polygon, obstacle.polygon)
            for obstacle in editable_geometry.obstacles
        ):
            raise ValueError(f"equipment footprint {facility.id!r} overlaps a blocked obstacle")
        if any(
            _segment_distance_to_polygon(wall.start, wall.end, footprint.polygon) <= 1e-8
            for wall in editable_geometry.walls
        ):
            raise ValueError(f"equipment footprint {facility.id!r} intersects a wall")
        if any(
            other.facility_id != footprint.facility_id
            and _polygons_overlap(footprint.polygon, other.polygon)
            for other in editable_geometry.equipment_footprints
        ):
            raise ValueError(f"equipment footprint {facility.id!r} overlaps another footprint")
    for index, first in enumerate(editable_geometry.obstacles):
        if any(
            _polygons_have_interior_overlap(first.polygon, second.polygon)
            for second in editable_geometry.obstacles[index + 1 :]
        ):
            raise ValueError(f"obstacle {first.obstacle_id!r} overlaps another obstacle")
    service_point_issues = validate_facility_service_points(
        geometry, facilities, zone_edges, editable_geometry
    )
    if service_point_issues:
        raise ValueError(service_point_issues[0].message)


def validate_facility_service_points(
    geometry: FacilityGeometry,
    facilities: tuple[FacilitySpec, ...],
    zone_edges: tuple[ZoneEdgeSpec, ...],
    editable_geometry: EditableFacilityGeometry,
) -> tuple[FacilityGeometryIssue, ...]:
    """Validate authored service points against their physical facility and routes.

    This checks structural facts available from the plan. It does not claim that a
    route is operationally open or that a point is accessible when its access value
    is unknown; runtime queueing and service completion remain simulator behavior.
    """
    facility_by_id = {item.id: item for item in facilities}
    zone_by_id = {item.zone_id: item for item in geometry.zones}
    footprint_by_id = {item.facility_id: item for item in editable_geometry.equipment_footprints}
    obstacle_by_id = {item.obstacle_id: item for item in editable_geometry.obstacles}
    walls = editable_geometry.walls
    route_by_id = {item.edge_id: item for item in geometry.routes}
    issues: list[FacilityGeometryIssue] = []
    service_capacity_by_facility: dict[str, int] = {}

    def add(code: str, message: str, target_id: str) -> None:
        issues.append(FacilityGeometryIssue(code=code, message=message, target_id=target_id))

    for service_point in geometry.service_points:
        target_id = service_point.id
        facility = facility_by_id.get(service_point.facility_id)
        if facility is None:
            add(
                "service_point_unknown_facility",
                f"Service point {target_id!r} references unknown facility {service_point.facility_id!r}.",
                target_id,
            )
            continue
        service_capacity_by_facility[facility.id] = (
            service_capacity_by_facility.get(facility.id, 0) + service_point.service_capacity
        )
        zone = zone_by_id.get(facility.zone_id)
        if zone is None or not point_in_polygon(service_point.point_m, zone.polygon):
            add(
                "service_point_outside_zone",
                f"Service point {target_id!r} must be inside facility {facility.id!r}'s zone {facility.zone_id!r}.",
                target_id,
            )
        if any(
            point_in_polygon(service_point.point_m, obstacle.polygon)
            for obstacle in obstacle_by_id.values()
        ):
            add(
                "service_point_in_obstacle",
                f"Service point {target_id!r} is inside or on a blocked obstacle.",
                target_id,
            )
        if any(
            _point_segment_distance(service_point.point_m, wall.start, wall.end) <= 1e-8
            for wall in walls
        ):
            add(
                "service_point_on_wall",
                f"Service point {target_id!r} lies on a wall; place the service location in walkable space.",
                target_id,
            )
        own_footprint = footprint_by_id.get(facility.id)
        if own_footprint is not None and not point_in_polygon(
            service_point.point_m, own_footprint.polygon
        ):
            add(
                "service_point_outside_equipment",
                f"Service point {target_id!r} must lie inside its facility equipment footprint.",
                target_id,
            )
        if any(
            other_id != facility.id and point_in_polygon(service_point.point_m, footprint.polygon)
            for other_id, footprint in footprint_by_id.items()
        ):
            add(
                "service_point_in_other_equipment",
                f"Service point {target_id!r} overlaps equipment assigned to another facility.",
                target_id,
            )

        if service_point.access.accessibility != "accessible" or zone is None:
            continue
        accessible_graph: dict[str, set[str]] = {}
        for edge in zone_edges:
            route = route_by_id.get(edge.id)
            if (
                route is None
                or edge.closed
                or not route.accessible
                or route.width_m < ACCESSIBLE_ROUTE_MIN_WIDTH_M
            ):
                continue
            accessible_graph.setdefault(edge.from_zone_id, set()).add(edge.to_zone_id)
            accessible_graph.setdefault(edge.to_zone_id, set()).add(edge.from_zone_id)
        corridor_id = "corridor" if "corridor" in zone_by_id else None
        seen = {facility.zone_id}
        pending = [facility.zone_id]
        while pending:
            current = pending.pop()
            for neighbor in accessible_graph.get(current, ()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    pending.append(neighbor)
        graph_reaches_shared_corridor = (
            corridor_id in seen
            if corridor_id is not None and facility.zone_id != corridor_id
            else bool(accessible_graph.get(facility.zone_id))
        )
        service_has_clear_approach = False
        for edge in zone_edges:
            route = route_by_id.get(edge.id)
            if (
                route is None
                or edge.closed
                or not route.accessible
                or route.width_m < ACCESSIBLE_ROUTE_MIN_WIDTH_M
            ):
                continue
            if edge.from_zone_id == facility.zone_id:
                adjacent_zone, route_endpoint = edge.to_zone_id, route.points[0]
            elif edge.to_zone_id == facility.zone_id:
                adjacent_zone, route_endpoint = edge.from_zone_id, route.points[-1]
            else:
                continue
            if adjacent_zone not in seen:
                continue
            if service_point.point_m == route_endpoint:
                service_has_clear_approach = True
                break
            approach_id = f"service-approach-{hashlib.sha256(target_id.encode()).hexdigest()[:16]}"
            approach = PhysicalRoute(
                edge_id=approach_id,
                points=(service_point.point_m, route_endpoint),
                width_m=ACCESSIBLE_ROUTE_MIN_WIDTH_M,
                accessible=True,
            )
            approach_openings: list[RouteOpeningUse] = []
            for wall in walls:
                intersections = _segment_intersection_points(
                    service_point.point_m, route_endpoint, wall.start, wall.end
                )
                if not intersections:
                    continue
                matching_openings = [
                    opening
                    for opening in editable_geometry.openings
                    if opening.wall_id == wall.wall_id
                    and all(
                        _point_on_segment(point, opening.start, opening.end)
                        for point in intersections
                    )
                ]
                if matching_openings:
                    approach_openings.append(
                        RouteOpeningUse(
                            route_id=approach_id, opening_id=matching_openings[0].opening_id
                        )
                    )
            approach_geometry = editable_geometry.model_copy(
                update={
                    "equipment_footprints": tuple(
                        footprint
                        for footprint in editable_geometry.equipment_footprints
                        if footprint.facility_id != facility.id
                    ),
                    "route_openings": (
                        *editable_geometry.route_openings,
                        *(approach_openings if len(approach_openings) <= 1 else ()),
                    ),
                }
            )
            try:
                validate_editable_routes((approach,), approach_geometry, physical_zones=(zone,))
            except ValueError:
                continue
            service_has_clear_approach = True
            break
        if not graph_reaches_shared_corridor or not service_has_clear_approach:
            add(
                "service_point_access_route_missing",
                f"Service point {target_id!r} is marked accessible but has no clear {ACCESSIBLE_ROUTE_MIN_WIDTH_M:g} m approach to a connected accessible route.",
                target_id,
            )
    for facility in facilities:
        declared_capacity = (
            facility.working_units if facility.working_units is not None else facility.capacity
        )
        if service_capacity_by_facility.get(facility.id, 0) > declared_capacity:
            add(
                "service_point_capacity_exceeds_facility",
                f"Service points for facility {facility.id!r} exceed its declared working-unit capacity {declared_capacity}.",
                facility.id,
            )
    return tuple(issues)


def revalidate_standard_facility(
    design: FacilityDesign | dict[str, object],
    *,
    geometry: FacilityGeometry | dict[str, object] | None = None,
    editable_geometry: EditableFacilityGeometry | dict[str, object] | None = None,
    edit_source_id: str = "SRC-OPERATOR-FACILITY-CORRECTION",
) -> StandardFacilityRevalidation:
    """Recheck a user-edited standard plan before it is sent for common Scenario approval.

    The candidate must include both the shared walkable FacilityGeometry and the editable
    wall/opening/obstacle/equipment geometry. This function deliberately does not repair
    bad routes, increase area, or raise capacity to fit the requested population.
    """
    design = design if isinstance(design, FacilityDesign) else FacilityDesign.model_validate(design)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", edit_source_id):
        raise ValueError("facility correction source ID must be a stable identifier")
    try:
        candidate_geometry = (
            design.geometry
            if geometry is None
            else geometry
            if isinstance(geometry, FacilityGeometry)
            else FacilityGeometry.model_validate(geometry)
        )
        candidate_editable = (
            design.editable_geometry
            if editable_geometry is None
            else editable_geometry
            if isinstance(editable_geometry, EditableFacilityGeometry)
            else EditableFacilityGeometry.model_validate(editable_geometry)
        )
    except ValueError as exc:
        return StandardFacilityRevalidation(
            status="invalid",
            design=design,
            issues=(
                FacilityGeometryIssue(
                    code="geometry_schema_invalid",
                    message=str(exc)[:1_000],
                ),
            ),
        )

    candidate_geometry = with_legacy_service_points(candidate_geometry, design.facilities)

    changed = (
        candidate_geometry != design.geometry or candidate_editable != design.editable_geometry
    )
    if changed:
        candidate_geometry = candidate_geometry.model_copy(
            update={
                "source_ids": tuple(sorted(set(candidate_geometry.source_ids) | {edit_source_id})),
                "source_kind": "operator",
                "zones": tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in candidate_geometry.zones
                ),
            }
        )
        candidate_editable = candidate_editable.model_copy(
            update={
                "walls": tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in candidate_editable.walls
                ),
                "openings": tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in candidate_editable.openings
                ),
                "obstacles": tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in candidate_editable.obstacles
                ),
                "equipment_footprints": tuple(
                    item.model_copy(
                        update={
                            "source_ids": tuple(sorted(set(item.source_ids) | {edit_source_id}))
                        }
                    )
                    for item in candidate_editable.equipment_footprints
                ),
            }
        )

    issues: list[FacilityGeometryIssue] = []

    def add_error(code: str, message: str, target_id: str | None = None) -> None:
        issues.append(
            FacilityGeometryIssue(code=code, message=message[:1_000], target_id=target_id)
        )

    if not math.isclose(
        candidate_geometry.width_m, candidate_editable.width_m, rel_tol=0, abs_tol=1e-9
    ) or not math.isclose(
        candidate_geometry.height_m, candidate_editable.height_m, rel_tol=0, abs_tol=1e-9
    ):
        add_error(
            "geometry_dimensions_mismatch",
            "Walkable and editable geometry must use the same metric dimensions.",
        )
    building_area = candidate_geometry.width_m * candidate_geometry.height_m
    zone_by_id = {zone.zone_id: zone for zone in candidate_geometry.zones}
    design_zone_ids = {zone.id for zone in design.zones}
    if set(zone_by_id) != design_zone_ids:
        add_error(
            "zone_set_mismatch", "Edited geometry must preserve the standard design's zone IDs."
        )
    for zone in candidate_geometry.zones:
        if any(
            not 0 <= point.x <= candidate_geometry.width_m
            or not 0 <= point.y <= candidate_geometry.height_m
            for point in zone.polygon
        ):
            add_error(
                "zone_out_of_bounds",
                f"Zone {zone.zone_id!r} leaves the building bounds.",
                zone.zone_id,
            )
        polygon_area = (
            abs(
                sum(
                    a.x * b.y - b.x * a.y
                    for a, b in zip(zone.polygon, zone.polygon[1:] + zone.polygon[:1])
                )
            )
            / 2
        )
        if (
            not _valid_simple_polygon(zone.polygon)
            or zone.usable_area_m2 <= 0
            or zone.usable_area_m2 > polygon_area + 1e-6
        ):
            add_error(
                "zone_area_invalid",
                f"Zone {zone.zone_id!r} has invalid polygon or usable area.",
                zone.zone_id,
            )
    try:
        _validate_physical_zone_polygons(candidate_geometry.zones)
    except ValueError as exc:
        add_error("zone_overlap_or_topology", str(exc))
    if sum(zone.usable_area_m2 for zone in candidate_geometry.zones) > building_area + 1e-6:
        add_error(
            "usable_area_exceeds_building", "Summed usable zone area exceeds gross building area."
        )

    route_by_id = {route.edge_id: route for route in candidate_geometry.routes}
    expected_route_ids = {edge.id for edge in design.zone_edges}
    if set(route_by_id) != expected_route_ids:
        add_error(
            "route_set_mismatch",
            "Edited geometry routes must match the standard design's directed zone edges.",
        )
    try:
        validate_editable_routes(
            candidate_geometry.routes,
            candidate_editable,
            physical_zones=candidate_geometry.zones,
        )
    except ValueError as exc:
        add_error("route_or_clearance_invalid", str(exc))

    issues.extend(
        validate_facility_service_points(
            candidate_geometry, design.facilities, design.zone_edges, candidate_editable
        )
    )

    required_access_zones = {"intake", "toilet", "medical", "food", "sleeping", "quiet", "staff"}
    edge_by_id = {edge.id: edge for edge in design.zone_edges}
    for route in candidate_geometry.routes:
        edge = edge_by_id.get(route.edge_id)
        if edge and route.points:
            from_zone = zone_by_id.get(edge.from_zone_id)
            to_zone = zone_by_id.get(edge.to_zone_id)
            if from_zone is None or not point_in_polygon(route.points[0], from_zone.polygon):
                add_error(
                    "route_start_outside_source_zone",
                    f"Route {route.edge_id!r} must start inside its declared source zone {edge.from_zone_id!r}.",
                    route.edge_id,
                )
            if to_zone is None or not point_in_polygon(route.points[-1], to_zone.polygon):
                add_error(
                    "route_end_outside_target_zone",
                    f"Route {route.edge_id!r} must end inside its declared target zone {edge.to_zone_id!r}.",
                    route.edge_id,
                )
        if (
            edge
            and ({edge.from_zone_id, edge.to_zone_id} & required_access_zones)
            and not route.accessible
        ):
            add_error(
                "accessible_route_deficit",
                f"Route {route.edge_id!r} is narrower than the required 1.8 m accessible-route width.",
                route.edge_id,
            )

    facility_by_id = {facility.id: facility for facility in design.facilities}
    if set(candidate_geometry.facility_points) != set(facility_by_id):
        add_error(
            "facility_position_set_mismatch",
            "Every generated facility must have exactly one edited position.",
        )
    for facility_id, point in candidate_geometry.facility_points.items():
        facility = facility_by_id.get(facility_id)
        zone = zone_by_id.get(facility.zone_id) if facility else None
        if zone is None or not point_in_polygon(point, zone.polygon):
            add_error(
                "facility_position_outside_zone",
                f"Facility {facility_id!r} lies outside its assigned zone.",
                facility_id,
            )
        if any(
            point_in_polygon(point, obstacle.polygon) for obstacle in candidate_editable.obstacles
        ):
            add_error(
                "facility_position_inside_obstacle",
                f"Facility {facility_id!r} overlaps an obstacle.",
                facility_id,
            )
    footprint_by_id = {item.facility_id: item for item in candidate_editable.equipment_footprints}
    for footprint in candidate_editable.equipment_footprints:
        facility = facility_by_id.get(footprint.facility_id)
        zone = zone_by_id.get(facility.zone_id) if facility else None
        if facility is None or zone is None:
            continue
        marker = candidate_geometry.facility_points.get(footprint.facility_id)
        if marker is None or not point_in_polygon(marker, footprint.polygon):
            add_error(
                "equipment_marker_outside_footprint",
                f"Facility {footprint.facility_id!r} position must remain inside its equipment footprint.",
                footprint.facility_id,
            )
        for start, end in zip(footprint.polygon, footprint.polygon[1:] + footprint.polygon[:1]):
            if not _segment_inside_polygon_union(start, end, (zone,)):
                add_error(
                    "equipment_outside_zone",
                    f"Equipment footprint {footprint.facility_id!r} leaves its assigned zone.",
                    footprint.facility_id,
                )
                break
        if any(
            _polygons_overlap(footprint.polygon, obstacle.polygon)
            for obstacle in candidate_editable.obstacles
        ):
            add_error(
                "equipment_overlaps_obstacle",
                f"Equipment footprint {footprint.facility_id!r} intersects an obstacle.",
                footprint.facility_id,
            )
        if any(
            _segment_distance_to_polygon(wall.start, wall.end, footprint.polygon) <= 1e-8
            for wall in candidate_editable.walls
        ):
            add_error(
                "equipment_overlaps_wall",
                f"Equipment footprint {footprint.facility_id!r} intersects a wall.",
                footprint.facility_id,
            )
        if any(
            other_id != footprint.facility_id
            and _polygons_overlap(footprint.polygon, other.polygon)
            for other_id, other in footprint_by_id.items()
        ):
            add_error(
                "equipment_footprints_overlap",
                f"Equipment footprint {footprint.facility_id!r} intersects another footprint.",
                footprint.facility_id,
            )
    unknown_footprints = {
        item.facility_id for item in candidate_editable.equipment_footprints
    } - set(facility_by_id)
    for facility_id in sorted(unknown_footprints):
        add_error(
            "unknown_equipment_footprint",
            f"Equipment footprint {facility_id!r} has no matching facility.",
            facility_id,
        )

    sleeping = zone_by_id.get("sleeping")
    excluded_sleeping_area_m2 = 0.0
    for index, first in enumerate(candidate_editable.obstacles):
        for second in candidate_editable.obstacles[index + 1 :]:
            if _polygons_have_interior_overlap(first.polygon, second.polygon):
                add_error(
                    "obstacles_overlap",
                    f"Obstacles {first.obstacle_id!r} and {second.obstacle_id!r} overlap; merge or correct their outlines before recalculating capacity.",
                    first.obstacle_id,
                )
    if sleeping is not None:
        for obstacle in candidate_editable.obstacles:
            if not _polygons_overlap(obstacle.polygon, sleeping.polygon):
                continue
            if not all(point_in_polygon(point, sleeping.polygon) for point in obstacle.polygon):
                add_error(
                    "obstacle_crosses_zone_boundary",
                    f"Obstacle {obstacle.obstacle_id!r} crosses a zone boundary; review its area assignment.",
                    obstacle.obstacle_id,
                )
                continue
            excluded_sleeping_area_m2 += (
                abs(
                    sum(
                        start.x * end.y - end.x * start.y
                        for start, end in zip(
                            obstacle.polygon, obstacle.polygon[1:] + obstacle.polygon[:1]
                        )
                    )
                )
                / 2
            )
        for footprint in candidate_editable.equipment_footprints:
            facility = facility_by_id.get(footprint.facility_id)
            if facility is not None and facility.zone_id == "sleeping":
                excluded_sleeping_area_m2 += footprint.area_m2
    effective_sleeping_area_m2 = max(
        0.0,
        (sleeping.usable_area_m2 if sleeping else 0.0) - excluded_sleeping_area_m2,
    )
    area_capacity = math.floor(effective_sleeping_area_m2 / 3.5)
    source_capacity = next(
        (
            int(observation["value"])
            for observation in design.observations
            if observation.get("subject") == "published_facility_capacity"
            and isinstance(observation.get("value"), int)
            and not isinstance(observation.get("value"), bool)
        ),
        None,
    )
    supported_capacity = (
        min(area_capacity, source_capacity) if source_capacity is not None else area_capacity
    )
    warnings = [
        warning
        for warning in design.warnings
        if "参考収容数" not in warning
        and "公開収容数" not in warning
        and "1.8 m未満" not in warning
    ]
    if supported_capacity < design.requested_population:
        issues.append(
            FacilityGeometryIssue(
                code="capacity_deficit",
                message=(
                    f"Net sleeping area {effective_sleeping_area_m2:.2f} m² supports at most {supported_capacity} "
                    "by the 3.5 m² planning heuristic; "
                    f"the request is {design.requested_population}. Occupancy is not increased to fit."
                ),
                severity="warning",
                target_id="sleeping",
            )
        )
        warnings.append(
            f"編集後の参考収容数{supported_capacity}人が入力人数{design.requested_population}人を下回ります。過密を避け、面積または人数を確認してください。"
        )
    updated_zones = tuple(
        zone.model_copy(update={"capacity": supported_capacity}) if zone.id == "sleeping" else zone
        for zone in design.zones
    )
    revised = design.model_copy(
        update={
            "zones": updated_zones,
            "geometry": candidate_geometry,
            "editable_geometry": candidate_editable,
            "capacity_supported": supported_capacity,
            "warnings": tuple(dict.fromkeys(warnings)),
        }
    )
    return StandardFacilityRevalidation(
        status="invalid" if any(issue.severity == "error" for issue in issues) else "valid",
        design=revised,
        issues=tuple(issues),
        edit_source_id=edit_source_id if changed else None,
        effective_sleeping_area_m2=effective_sleeping_area_m2,
    )


def _opening_endpoint(
    center: Point2D, start: Point2D, end: Point2D, direction: int, width: float
) -> Point2D:
    length = math.hypot(end.x - start.x, end.y - start.y)
    if length == 0:
        raise ValueError("opening references a zero-length wall")
    scale = direction * width / (2 * length)
    x = center.x + (end.x - start.x) * scale
    y = center.y + (end.y - start.y) * scale
    # Centre/width conversion can overshoot a boundary by a few floating-point
    # ulps. Snap only numerical boundary noise; genuinely outside openings still
    # fail the unchanged geometry bounds and host-wall validators.
    for bound in (start.x, end.x):
        if math.isclose(x, bound, rel_tol=0, abs_tol=1e-9):
            x = bound
    for bound in (start.y, end.y):
        if math.isclose(y, bound, rel_tol=0, abs_tol=1e-9):
            y = bound
    return Point2D(x=x, y=y)


def _segment_intersection_points(
    first_start: Point2D,
    first_end: Point2D,
    second_start: Point2D,
    second_end: Point2D,
) -> tuple[Point2D, ...]:
    def cross(ax: float, ay: float, bx: float, by: float) -> float:
        return ax * by - ay * bx

    rx, ry = first_end.x - first_start.x, first_end.y - first_start.y
    sx, sy = second_end.x - second_start.x, second_end.y - second_start.y
    denominator = cross(rx, ry, sx, sy)
    qpx, qpy = second_start.x - first_start.x, second_start.y - first_start.y
    if abs(denominator) <= 1e-9:
        if abs(cross(qpx, qpy, rx, ry)) > 1e-9:
            return ()
        shared = [
            point
            for point in (first_start, first_end, second_start, second_end)
            if _point_on_segment(point, first_start, first_end)
            and _point_on_segment(point, second_start, second_end)
        ]
        return tuple({(point.x, point.y): point for point in shared}.values())
    t = cross(qpx, qpy, sx, sy) / denominator
    u = cross(qpx, qpy, rx, ry) / denominator
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return (Point2D(x=first_start.x + t * rx, y=first_start.y + t * ry),)
    return ()


def _point_segment_distance(point: Point2D, start: Point2D, end: Point2D) -> float:
    dx, dy = end.x - start.x, end.y - start.y
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-18:
        return math.hypot(point.x - start.x, point.y - start.y)
    fraction = max(
        0.0, min(1.0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_squared)
    )
    closest = Point2D(x=start.x + fraction * dx, y=start.y + fraction * dy)
    return math.hypot(point.x - closest.x, point.y - closest.y)


def _segment_distance(
    start: Point2D, end: Point2D, other_start: Point2D, other_end: Point2D
) -> float:
    if _segment_intersection_points(start, end, other_start, other_end):
        return 0.0
    return min(
        _point_segment_distance(start, other_start, other_end),
        _point_segment_distance(end, other_start, other_end),
        _point_segment_distance(other_start, start, end),
        _point_segment_distance(other_end, start, end),
    )


def _segment_distance_to_polygon(
    start: Point2D, end: Point2D, polygon: tuple[Point2D, ...]
) -> float:
    if point_in_polygon(start, polygon) or point_in_polygon(end, polygon):
        return 0.0
    distances = [
        _segment_distance(start, end, first, second)
        for first, second in zip(polygon, polygon[1:] + polygon[:1])
    ]
    return min(distances, default=math.inf)


def _polygons_overlap(first: tuple[Point2D, ...], second: tuple[Point2D, ...]) -> bool:
    if any(point_in_polygon(point, second) for point in first) or any(
        point_in_polygon(point, first) for point in second
    ):
        return True
    return any(
        _segment_distance(start, end, other_start, other_end) <= 1e-8
        for start, end in zip(first, first[1:] + first[:1])
        for other_start, other_end in zip(second, second[1:] + second[:1])
    )


def _polygons_have_interior_overlap(
    first: tuple[Point2D, ...], second: tuple[Point2D, ...]
) -> bool:
    """Detect positive-area overlap while allowing obstacle outlines to touch."""
    first_coordinates = {(point.x, point.y) for point in first}
    second_coordinates = {(point.x, point.y) for point in second}
    if len(first) == len(second) and first_coordinates == second_coordinates:
        return True

    def strictly_inside(point: Point2D, polygon: tuple[Point2D, ...]) -> bool:
        if any(
            _point_on_segment(point, start, end)
            for start, end in zip(polygon, polygon[1:] + polygon[:1])
        ):
            return False
        return point_in_polygon(point, polygon)

    if any(strictly_inside(point, second) for point in first) or any(
        strictly_inside(point, first) for point in second
    ):
        return True
    first_edge_midpoints = tuple(
        Point2D(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2)
        for start, end in zip(first, first[1:] + first[:1])
    )
    second_edge_midpoints = tuple(
        Point2D(x=(start.x + end.x) / 2, y=(start.y + end.y) / 2)
        for start, end in zip(second, second[1:] + second[:1])
    )
    if any(strictly_inside(point, second) for point in first_edge_midpoints) or any(
        strictly_inside(point, first) for point in second_edge_midpoints
    ):
        return True

    def interior_centroid(polygon: tuple[Point2D, ...]) -> Point2D | None:
        cross_sum = sum(
            start.x * end.y - end.x * start.y
            for start, end in zip(polygon, polygon[1:] + polygon[:1])
        )
        if abs(cross_sum) <= 1e-12:
            return None
        centroid = Point2D(
            x=sum(
                (start.x + end.x) * (start.x * end.y - end.x * start.y)
                for start, end in zip(polygon, polygon[1:] + polygon[:1])
            )
            / (3 * cross_sum),
            y=sum(
                (start.y + end.y) * (start.x * end.y - end.x * start.y)
                for start, end in zip(polygon, polygon[1:] + polygon[:1])
            )
            / (3 * cross_sum),
        )
        return centroid if strictly_inside(centroid, polygon) else None

    first_centroid = interior_centroid(first)
    second_centroid = interior_centroid(second)
    if (first_centroid and strictly_inside(first_centroid, second)) or (
        second_centroid and strictly_inside(second_centroid, first)
    ):
        return True

    def crosses_interior(a: Point2D, b: Point2D, c: Point2D, d: Point2D) -> bool:
        first_side = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
        second_side = (b.x - a.x) * (d.y - a.y) - (b.y - a.y) * (d.x - a.x)
        third_side = (d.x - c.x) * (a.y - c.y) - (d.y - c.y) * (a.x - c.x)
        fourth_side = (d.x - c.x) * (b.y - c.y) - (d.y - c.y) * (b.x - c.x)
        return first_side * second_side < -1e-16 and third_side * fourth_side < -1e-16

    return any(
        crosses_interior(start, end, other_start, other_end)
        for start, end in zip(first, first[1:] + first[:1])
        for other_start, other_end in zip(second, second[1:] + second[:1])
    )


def _segment_inside_polygon_union(
    start: Point2D, end: Point2D, zones: tuple[PhysicalZone, ...]
) -> bool:
    """Check every interval between polygon crossings, not just route endpoints."""
    polygons = tuple(zone.polygon for zone in zones)
    dx, dy = end.x - start.x, end.y - start.y
    if not any(point_in_polygon(start, polygon) for polygon in polygons) or not any(
        point_in_polygon(end, polygon) for polygon in polygons
    ):
        return False
    parameters = [0.0, 1.0]
    for polygon in polygons:
        for first, second in zip(polygon, polygon[1:] + polygon[:1]):
            for point in _segment_intersection_points(start, end, first, second):
                if abs(dx) >= abs(dy) and abs(dx) > 1e-12:
                    fraction = (point.x - start.x) / dx
                elif abs(dy) > 1e-12:
                    fraction = (point.y - start.y) / dy
                else:
                    fraction = 0.0
                if -1e-9 <= fraction <= 1 + 1e-9:
                    parameters.append(max(0.0, min(1.0, fraction)))
    ordered = sorted(set(round(value, 12) for value in parameters))
    checks = [*ordered, *((left + right) / 2 for left, right in zip(ordered, ordered[1:]))]
    return all(
        any(
            point_in_polygon(Point2D(x=start.x + t * dx, y=start.y + t * dy), polygon)
            for polygon in polygons
        )
        for t in checks
    )


def _segment_has_polygon_clearance(
    start: Point2D, end: Point2D, clearance_m: float, zones: tuple[PhysicalZone, ...]
) -> bool:
    for zone in zones:
        polygon = zone.polygon
        for first, second in zip(polygon, polygon[1:] + polygon[:1]):
            dx, dy = second.x - first.x, second.y - first.y
            parameters = [0.0, 1.0]
            for other in zones:
                if other.zone_id == zone.zone_id:
                    continue
                for edge_start, edge_end in zip(
                    other.polygon, other.polygon[1:] + other.polygon[:1]
                ):
                    for crossing in _segment_intersection_points(
                        first, second, edge_start, edge_end
                    ):
                        if abs(dx) >= abs(dy) and abs(dx) > 1e-12:
                            fraction = (crossing.x - first.x) / dx
                        elif abs(dy) > 1e-12:
                            fraction = (crossing.y - first.y) / dy
                        else:
                            fraction = 0.0
                        if -1e-9 <= fraction <= 1 + 1e-9:
                            parameters.append(max(0.0, min(1.0, fraction)))
            ordered = sorted(set(round(value, 12) for value in parameters))
            for lower, upper in zip(ordered, ordered[1:]):
                midpoint_t = (lower + upper) / 2
                midpoint = Point2D(x=first.x + midpoint_t * dx, y=first.y + midpoint_t * dy)
                shared_boundary = any(
                    other.zone_id != zone.zone_id and point_in_polygon(midpoint, other.polygon)
                    for other in zones
                )
                if shared_boundary:
                    continue
                boundary_start = Point2D(x=first.x + lower * dx, y=first.y + lower * dy)
                boundary_end = Point2D(x=first.x + upper * dx, y=first.y + upper * dy)
                if _segment_distance(start, end, boundary_start, boundary_end) < clearance_m - 1e-8:
                    return False
    return True


def _segment_intersects_polygon(start: Point2D, end: Point2D, polygon: tuple[Point2D, ...]) -> bool:
    if point_in_polygon(start, polygon) or point_in_polygon(end, polygon):
        return True
    return any(
        _segment_intersection_points(start, end, first, second)
        for first, second in zip(polygon, polygon[1:] + polygon[:1])
    )
