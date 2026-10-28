"""Compose region research, synthetic residents, and a shelter draft into ScenarioSpec."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from sheltersim.contracts.extensions import FacilityGeometry
from sheltersim.contracts.policies import CORE_MODEL_SUITE_MODELS, CORE_MODEL_SUITE_V2
from sheltersim.domain.models import Mobility
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.experiments.scenario import (
    TOILET_SERVICE_SOURCE_ID,
    FacilitySpec,
    PsychologyModelConfig,
    ScenarioSpec,
    ZoneEdgeSpec,
)
from sheltersim.region.conditions import (
    RESOURCE_SERVICE_ASSUMPTION_ID,
    STANDARD_PROVISIONING_SOURCE_ID,
    OperationalConditions,
    with_standard_provisioning,
)
from sheltersim.region.facility import (
    EditableFacilityGeometry,
    FacilityDesign,
    FacilityDesignRequest,
    FacilityZoneCondition,
    StandardShelterGenerator,
    shelter_staff,
    shelter_staff_assumption,
)
from sheltersim.region.models import (
    ACCESSIBLE_ROUTE_MIN_WIDTH_M,
    RegionModel,
    RegionResearchResult,
    ShelterCandidate,
)
from sheltersim.region.plan_import import ImportedPlanGeometry
from sheltersim.region.population import (
    DEFAULT_SYNTHETIC_HOUSEHOLD_COMPOSITION,
    OPERATOR_POPULATION_SOURCE_ID,
    AgeBand,
    EvacuationSelection,
    GeneratedPopulation,
    HouseholdSizeMix,
    PopulationConditionInput,
    SyntheticPopulationGenerator,
    population_statistics_notes,
)


class InitialScenarioRequest(RegionModel):
    municipality: str = Field(min_length=1, max_length=160)
    municipality_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    disaster: str = Field(min_length=1, max_length=80)
    evacuees: int = Field(ge=1, le=10_000)
    duration_days: float = Field(default=7, gt=0, le=90, allow_inf_nan=False)
    seed: int = Field(default=2026, ge=0)
    behavior_policy_id: Literal[
        "utility-softmax-v1", "sleep-aware-utility-softmax-v2", "priority-rule-v1"
    ] = "sleep-aware-utility-softmax-v2"
    social_policy_id: Literal[
        "sparse-family-zone-contact-v1", "degroot-contact-v1", "expressiveness-channel-openness-v1"
    ] = "expressiveness-channel-openness-v1"
    information_policy_id: Literal[
        "sparse-information-contact-v1", "hazard-information-v1", "evidence-aware-rumor-v1"
    ] = "evidence-aware-rumor-v1"
    social_propagation_enabled: bool = True
    information_spread_enabled: bool = True
    queue_policy_id: Literal["need-weighted-physical-queue-v1", "strict-fifo-queue-v1"] = (
        "need-weighted-physical-queue-v1"
    )
    relationship_policy_id: Literal[
        "distance-duration-repeat-trust-v1", "duration-contact-trust-v1"
    ] = "distance-duration-repeat-trust-v1"
    social_proximity_policy_id: Literal[
        "disabled-proximity-v1", "distance-sampled-proximity-v1"
    ] = "disabled-proximity-v1"
    body_dynamics_policy_id: Literal[
        "body-feedback-complete-v1", "body-feedback-complete-v2", "body-feedback-sleep-debt-v1"
    ] = "body-feedback-complete-v2"
    staff_operations_policy_id: Literal["staff-urgency-first-v1", "staff-travel-first-v1"] = (
        "staff-urgency-first-v1"
    )
    staff_fatigue_policy_id: Literal["staff-fatigue-linear-v1", "staff-fatigue-saturating-v1"] = (
        "staff-fatigue-linear-v1"
    )
    zone_temperature_policy_id: Literal[
        "zone-temperature-comfort-band-v1", "zone-temperature-midpoint-v1"
    ] = "zone-temperature-comfort-band-v1"
    zone_rest_policy_id: Literal["zone-rest-multiplicative-v1", "zone-rest-limiting-v1"] = (
        "zone-rest-multiplicative-v1"
    )
    zone_hygiene_policy_id: Literal["zone-hygiene-linear-v1", "zone-hygiene-quadratic-v1"] = (
        "zone-hygiene-linear-v1"
    )
    zone_privacy_policy_id: Literal["zone-privacy-threshold-v1", "zone-privacy-proportional-v1"] = (
        "zone-privacy-threshold-v1"
    )
    care_outcome_policy_id: Literal[
        "hygiene-support-fixed-relief-v1",
        "hygiene-support-proportional-relief-v1",
        "typed-care-support-response-v1",
    ] = "typed-care-support-response-v1"
    model_parameter_overrides: dict[str, Any] = Field(default_factory=dict, max_length=64)
    gross_area_m2: float | None = Field(default=None, gt=0, le=100_000, allow_inf_nan=False)
    accessible_toilet_count: int = Field(default=1, ge=0, le=20)
    staff_count: int | None = Field(default=None, ge=0, le=100)
    # Day on which outside supply starts when no inventory is entered (national
    # guidance: three days of stock, then supply from day 4).
    default_supply_start_day: int = Field(default=4, ge=1, le=91)
    season: str | None = Field(default=None, max_length=40)
    target_date: date | None = None
    weather_area_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    facility_conditions: tuple[str, ...] = ()
    facility_zones: tuple[FacilityZoneCondition, ...] = ()
    zone_area_shares: dict[str, float] = Field(default_factory=dict)
    operational_conditions: OperationalConditions = Field(default_factory=OperationalConditions)
    evacuation_selection: EvacuationSelection = Field(default_factory=EvacuationSelection)
    population_conditions: PopulationConditionInput = Field(
        default_factory=PopulationConditionInput
    )
    field_origins: dict[str, Literal["user", "llm_extracted", "default"]] = Field(
        default_factory=dict
    )

    @field_validator("model_parameter_overrides", mode="before")
    @classmethod
    def overrides_are_finite_json_numbers(cls, raw: Any) -> Any:
        if raw is None:
            return {}
        if not isinstance(raw, dict) or len(raw) > 64:
            raise ValueError(
                "model parameter overrides must contain at most 64 finite JSON numbers or "
                "bounded entity maps"
            )
        try:
            encoded = json.dumps(raw, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "model parameter overrides must contain finite JSON numbers or bounded entity maps"
            ) from exc
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("model parameter overrides exceed the 16 KiB request limit")
        shift_path = "staff_dynamics.shift_windows_by_staff.*"
        for path, value in raw.items():
            if path == shift_path:
                continue
            if type(value) in (int, float) and math.isfinite(value):
                continue
            if (
                isinstance(value, dict)
                and len(value) <= 100
                and all(
                    isinstance(entity_id, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", entity_id)
                    and (
                        type(entity_value) in (int, float)
                        and math.isfinite(entity_value)
                        or isinstance(entity_value, dict)
                        and 1 <= len(entity_value) <= 4
                        and all(
                            isinstance(field, str)
                            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field)
                            and type(field_value) in (int, float)
                            and math.isfinite(field_value)
                            for field, field_value in entity_value.items()
                        )
                    )
                    for entity_id, entity_value in value.items()
                )
            ):
                continue
            raise ValueError(
                "model parameter overrides must contain finite JSON numbers or bounded entity maps"
            )
        if shift_path in raw:
            from sheltersim.domain.staff_dynamics import validate_staff_shift_windows_override

            validate_staff_shift_windows_override(raw[shift_path])
        return raw

    @model_validator(mode="after")
    def duration_is_whole_minutes(self) -> InitialScenarioRequest:
        if round(self.duration_days * 1440) < 1:
            raise ValueError("duration must be at least one minute")
        allowed = {
            "municipality",
            "municipality_code",
            "disaster",
            "evacuees",
            "duration_days",
            "gross_area_m2",
            "accessible_toilet_count",
            "staff_count",
            "default_supply_start_day",
            "season",
            "facility_conditions",
            "seed",
            "behavior_policy_id",
            "social_policy_id",
            "information_policy_id",
            "social_propagation_enabled",
            "information_spread_enabled",
            "facility_zones",
            "zone_area_shares",
            "operational_conditions",
            "evacuation_selection",
            "population_conditions",
            "target_date",
            "weather_area_code",
            "queue_policy_id",
            "relationship_policy_id",
            "social_proximity_policy_id",
            "body_dynamics_policy_id",
            "staff_operations_policy_id",
            "staff_fatigue_policy_id",
            "zone_temperature_policy_id",
            "zone_rest_policy_id",
            "zone_hygiene_policy_id",
            "zone_privacy_policy_id",
            "care_outcome_policy_id",
            "model_parameter_overrides",
        }
        if set(self.field_origins) - allowed:
            raise ValueError("field_origins contains an unknown request field")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*|\.\*)+", path)
            for path in self.model_parameter_overrides
        ):
            raise ValueError(
                "policy parameter overrides require namespaced catalog paths, including a "
                "declared wildcard segment where applicable"
            )
        return self


class InitialScenarioDraft(RegionModel):
    scenario: ScenarioSpec
    facility_design: FacilityDesign | None
    plan_geometry: ImportedPlanGeometry | None = None
    editable_geometry: EditableFacilityGeometry | None = None
    population_generation: GeneratedPopulation
    warnings: tuple[str, ...]
    review_required: bool = True
    approval_blockers: tuple[str, ...] = ()
    can_start: bool = True
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InitialScenarioBuilder:
    """Build a detached preview; callers must obtain operator approval before a run."""

    def __init__(
        self,
        *,
        facility_generator: StandardShelterGenerator | None = None,
        population_generator: SyntheticPopulationGenerator | None = None,
    ) -> None:
        self.facility_generator = facility_generator or StandardShelterGenerator()
        self.population_generator = population_generator or SyntheticPopulationGenerator()

    def build(
        self,
        request: InitialScenarioRequest,
        *,
        research: RegionResearchResult | None = None,
        age_bands: tuple[AgeBand, ...] | None = None,
        household_mix: HouseholdSizeMix | None = None,
        plan_override: ImportedPlanGeometry | None = None,
        selected_shelter_id: str | None = None,
        standard_design: FacilityDesign | None = None,
    ) -> InitialScenarioDraft:
        if not isinstance(request, InitialScenarioRequest):
            request = InitialScenarioRequest.model_validate(request)
        if research is not None and (
            research.request.municipality_query.casefold() != request.municipality.casefold()
        ):
            raise ValueError("regional research query does not match the scenario municipality")
        if research is not None and research.request.municipality_code != request.municipality_code:
            raise ValueError(
                "regional research municipality code does not match the scenario request"
            )
        if research is not None and _disaster_families(
            research.request.disaster
        ) != _disaster_families(request.disaster):
            raise ValueError("regional research disaster does not match the scenario disaster")
        if research is not None and research.request.estat_area_code:
            wrong_areas = {
                item.area_code
                for item in research.population_observations
                if item.area_code != research.request.estat_area_code
            }
            if wrong_areas:
                raise ValueError(
                    "population observations include an area code different from the "
                    "selected e-Stat area"
                )
        selected_shelter: ShelterCandidate | None = None
        if selected_shelter_id is not None:
            if research is None:
                raise ValueError("selecting a real facility requires its sourced research result")
            selected_shelter = next(
                (item for item in research.shelters if item.id == selected_shelter_id), None
            )
            if selected_shelter is None:
                raise ValueError(
                    "selected shelter ID is not present in the sourced research result"
                )
            if selected_shelter.designation_type == "emergency_site":
                raise ValueError(
                    "a designated emergency site cannot be selected as an overnight shelter"
                )
        selected_area = request.gross_area_m2 or (
            selected_shelter.area_m2 if selected_shelter is not None else None
        )
        area_source_ids = ()
        if request.gross_area_m2 is not None:
            area_source_ids = ("SRC-OPERATOR-AREA",)
        elif selected_shelter is not None and selected_shelter.area_m2 is not None:
            area_source_ids = (selected_shelter.source_id,)
        capacity_source_ids = (
            (selected_shelter.source_id,)
            if selected_shelter is not None and selected_shelter.capacity is not None
            else ()
        )
        design = standard_design or self.facility_generator.generate(
            FacilityDesignRequest(
                population=request.evacuees,
                gross_area_m2=selected_area,
                accessible_toilet_count=request.accessible_toilet_count,
                staff_count=request.staff_count,
                facility_conditions=request.facility_conditions,
                zone_conditions=request.facility_zones,
                zone_area_shares=request.zone_area_shares,
                equipment=request.operational_conditions.equipment,
                area_source_ids=area_source_ids,
                stated_capacity=selected_shelter.capacity if selected_shelter is not None else None,
                capacity_source_ids=capacity_source_ids,
            )
        )
        if standard_design is not None and (
            plan_override is not None or design.requested_population != request.evacuees
        ):
            raise ValueError(
                "standard correction must preserve requested population and cannot replace "
                "an uploaded plan"
            )
        zones = design.zones
        edges = design.zone_edges
        facilities = design.facilities
        staff = design.staff
        geometry = design.geometry
        editable_geometry = design.editable_geometry
        design_warnings = [] if plan_override is not None else list(design.warnings)
        approval_blockers: list[str] = []
        if plan_override is not None:
            zones = plan_override.zones
            edges = plan_override.zone_edges
            facilities = plan_override.facilities
            geometry = plan_override.geometry
            editable_geometry = plan_override.editable_geometry
            zone_ids = {zone.id for zone in zones}
            if not zones or "sleeping" not in zone_ids:
                raise ValueError("an imported plan must contain a scenario zone named 'sleeping'")
            # The plan's own water and meal stations each get a member in their zone, as in
            # the standard design; otherwise the service never starts (#201).
            staff = shelter_staff(
                population=request.evacuees,
                staff_count=request.staff_count,
                station_zone_ids=tuple(
                    facility.zone_id
                    for kind in ("water_station", "meal_distribution")
                    for facility in facilities
                    if facility.kind == kind
                ),
                general_zone_id="staff" if "staff" in zone_ids else "sleeping",
            )
            design_warnings.append(
                "施設図面Geometryを使用しています。ゾーン名・避難者容量・経路の実測性と対応は現場担当者が確認してください。"
            )
            if editable_geometry is None or not editable_geometry.walls:
                blocker = (
                    "図面の壁・開口が未入力です。施設内の通行可能領域を検証するまで承認できません。"
                )
                design_warnings.append(blocker)
                approval_blockers.append(blocker)
            if request.facility_zones:
                design_warnings.append(
                    "標準仮想施設向けのゾーン編集は、選択された実在図面には適用していません。図面側で編集してください。"
                )
        # New previews expose stable service-point IDs even when Geometry came
        # from an uploaded-plan candidate. Preserve the one-Facility/one-FIFO
        # compatibility adapter until the simulator adds per-point reservations.
        if not geometry.service_points:
            from sheltersim.contracts.spatial import legacy_service_points

            geometry = geometry.model_copy(
                update={"service_points": legacy_service_points(geometry, facilities)}
            )
            design = design.model_copy(update={"geometry": geometry})
        if selected_shelter is not None:
            design_warnings.append(
                f"選択施設「{selected_shelter.name}」の公開記録を参照しています。現行の指定状況・災害別適合・現地利用可否を管理者が確認してください。"
            )
            if selected_shelter.freshness != "current":
                design_warnings.append("選択施設記録の現行性は確認できていません。")
            if selected_shelter.shelter_subtype == "welfare":
                design_warnings.append(
                    "選択施設は福祉避難所として登録されています。受入対象者、開設状況、本人の支援ニーズとの適合を自治体に確認してください。"
                )
            if selected_shelter.capacity is not None:
                if plan_override is None:
                    zones = tuple(
                        zone.model_copy(update={"capacity": design.capacity_supported})
                        if zone.id == "sleeping"
                        else zone
                        for zone in zones
                    )

        plan_capacity_assumptions: list[dict[str, object]] = []
        if plan_override is not None:
            sleeping_zone = next(zone for zone in zones if zone.id == "sleeping")
            sleeping_geometry = next(zone for zone in geometry.zones if zone.zone_id == "sleeping")
            area_capacity = math.floor(sleeping_geometry.usable_area_m2 / 3.5)
            capacity_limits = [area_capacity]
            if sleeping_zone.capacity is not None:
                capacity_limits.append(sleeping_zone.capacity)
            if selected_shelter is not None and selected_shelter.capacity is not None:
                capacity_limits.append(selected_shelter.capacity)
            reconciled_capacity = min(capacity_limits)
            if len(set(capacity_limits)) > 1:
                design_warnings.append(
                    "図面の避難スペース面積、図面入力容量、施設公開容量に差があります。シナリオでは確認できる数値のうち最も小さい容量を使っています。"
                )
            if reconciled_capacity < request.evacuees:
                blocker = (
                    f"入力人数{request.evacuees}人が図面から見積もった避難スペース容量"
                    f"{reconciled_capacity}人を超えています。人数または図面を確認してください。"
                )
                design_warnings.append(blocker)
                approval_blockers.append(blocker)
            plan_capacity_assumptions.append(
                {
                    "id": "ASM-PLAN-CAPACITY-RECONCILIATION-V1",
                    "subject": "sleeping_zone_capacity",
                    "value": {
                        "capacity_used": reconciled_capacity,
                        "input_zone_capacity": sleeping_zone.capacity,
                        "area_based_capacity": area_capacity,
                        "published_capacity": selected_shelter.capacity
                        if selected_shelter
                        else None,
                    },
                    "reason": (
                        "For imported geometry, the lower of the uploaded zone capacity, "
                        "area-based 3.5 m²/person heuristic, and selected source capacity is "
                        "retained for the draft."
                    ),
                    "source_id": "ASM-PLAN-CAPACITY-RECONCILIATION-V1",
                }
            )
            zones = tuple(
                zone.model_copy(update={"capacity": reconciled_capacity})
                if zone.id == "sleeping"
                else zone
                for zone in zones
            )

        zone_ids = tuple(zone.id for zone in zones)
        if "sleeping" not in zone_ids:
            raise ValueError("scenario must include a 'sleeping' starting zone")
        if plan_override is None and design.capacity_supported < request.evacuees:
            blocker = (
                f"入力人数{request.evacuees}人が標準モデルの避難スペース容量"
                f"{design.capacity_supported}人を超えています。人数または面積を調整してください。"
            )
            approval_blockers.append(blocker)
        population = self.population_generator.generate(
            count=request.evacuees,
            seed=request.seed,
            zone_ids=("sleeping",),
            age_bands=age_bands,
            household_mix=household_mix,
            evacuation_selection=request.evacuation_selection,
            conditions=request.population_conditions,
            # Without entered household shapes, minors would have no linked adult
            # and every draft would stop at the guardian review blocker.
            household_composition=(
                None
                if request.population_conditions.household_composition is not None
                else DEFAULT_SYNTHETIC_HOUSEHOLD_COMPOSITION
            ),
        )
        unaccompanied_minors = tuple(
            item.person_id
            for item in population.person_requirements
            if item.guardian_status == "unaccompanied"
        )
        if unaccompanied_minors:
            blocker = (
                f"同居世帯内の年齢・支援情報から保護者を確認できない未成年者が{len(unaccompanied_minors)}人います。"
                "家族関係を確認するまでシミュレーションを開始できません。"
            )
            design_warnings.append(blocker)
            approval_blockers.append(blocker)
        if any(
            profile.mobility is not Mobility.INDEPENDENT
            for profile in population.profile_set.profiles
        ):
            inaccessible_zones = _inaccessible_service_zones(
                edges=edges,
                facilities=facilities,
                geometry=geometry,
            )
            missing_accessible_toilet = not any(
                facility.kind == "toilet" and facility.id.startswith("accessible_toilet_")
                for facility in facilities
            )
            if inaccessible_zones or missing_accessible_toilet:
                reasons = []
                if inaccessible_zones:
                    reasons.append(
                        "アクセシブルな往復経路を確認できない設備ゾーン: "
                        + "、".join(inaccessible_zones)
                    )
                if missing_accessible_toilet:
                    reasons.append("アクセシブルトイレとして識別された設備がありません")
                blocker = (
                    "移動支援ニーズのある避難者が生成されています。"
                    + "。".join(reasons)
                    + "。経路幅と設備を確認するまでシミュレーションを開始できません。"
                )
                design_warnings.append(blocker)
                approval_blockers.append(blocker)
        research_source_by_id = (
            {source.id: source.model_dump(mode="json") for source in research.sources}
            if research is not None
            else {}
        )
        extra_source_records = {str(source["id"]): source for source in population.sources}
        for condition in request.facility_zones:
            record = {
                "id": condition.source_id,
                "title": "Operator-edited standard shelter zone composition",
                "source_kind": condition.source_kind,
                "description": (
                    (
                        "Zone changes were entered by the scenario operator; geometry and "
                        "capacity remain synthetic and require review."
                    )
                    if condition.source_kind == "operator"
                    else (
                        "Zone changes are explicit scenario assumptions; geometry and capacity "
                        "remain synthetic."
                    )
                ),
            }
            prior = extra_source_records.get(condition.source_id)
            if prior is not None and prior.get("source_kind") != condition.source_kind:
                approval_blockers.append(
                    f"施設ゾーン条件の出典 {condition.source_id!r} が別の出典種別と衝突しています。"
                )
            else:
                extra_source_records[condition.source_id] = record
        facility_assumptions = (
            design.assumptions
            if plan_override is None
            else (shelter_staff_assumption(staff, entered_total=request.staff_count),)
        )
        model_assumptions: tuple[dict[str, object], ...] = (
            {
                "id": (
                    "ASM-PRIORITY-RULE-BEHAVIOR-V1"
                    if request.behavior_policy_id == "priority-rule-v1"
                    else (
                        "ASM-UTILITY-BEHAVIOR-V1"
                        if request.behavior_policy_id == "utility-softmax-v1"
                        else "ASM-SLEEP-AWARE-BEHAVIOR-V2"
                    )
                ),
                "subject": "behavior_model_defaults",
                "value": {
                    "decision_interval_minutes": 15,
                    "mode": "offline",
                    "policy": request.behavior_policy_id,
                    **(
                        {"sleep_debt_utility_weight": 1.0}
                        if request.behavior_policy_id == "sleep-aware-utility-softmax-v2"
                        else {}
                    ),
                },
                "reason": (
                    "Default simulation behavior configuration; not calibrated to local "
                    "observations."
                ),
                "source_id": (
                    "ASM-PRIORITY-RULE-BEHAVIOR-V1"
                    if request.behavior_policy_id == "priority-rule-v1"
                    else (
                        "ASM-UTILITY-BEHAVIOR-V1"
                        if request.behavior_policy_id == "utility-softmax-v1"
                        else "ASM-SLEEP-AWARE-BEHAVIOR-V2"
                    )
                ),
            },
            {
                "id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
                "subject": "staff_support_lifecycle",
                "value": {
                    "support_task_minutes": 10,
                    "unresolved_after_minutes": 90,
                    "support_cooldown_minutes": 30,
                    "support_fairness_minutes": 30,
                },
                "reason": (
                    "Generic support timing and fairness-aging delay used by the simulation "
                    "model; not an operational staffing standard."
                ),
                "source_id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
            },
            {
                "id": "ASM-TOILET-SERVICE-DURATION-V1",
                "subject": "toilet_service_duration",
                "value": {
                    "distribution": "discrete_uniform_inclusive",
                    "minimum_minutes": 4,
                    "maximum_minutes": 8,
                },
                "reason": (
                    "Generic toilet service timing used by the simulation model; not measured at "
                    "the selected facility."
                ),
                "source_id": TOILET_SERVICE_SOURCE_ID,
            },
        )
        for assumption in (*facility_assumptions, *model_assumptions):
            extra_source_records[str(assumption["source_id"])] = {
                "id": assumption["source_id"],
                "title": str(assumption["subject"]),
                "source_kind": "assumption",
                "description": str(assumption["reason"]),
            }
        source_records = research_source_by_id | extra_source_records
        # These request fields carry operator-declared identifiers, not complete
        # public-source records. Keep their references resolvable without promoting
        # them to verified statistics or replacing actual research citations.
        operator_population_sources = (
            *request.evacuation_selection.source_ids,
            *request.population_conditions.source_ids,
        )
        for source_id in operator_population_sources:
            source_records.setdefault(
                source_id,
                {
                    "id": source_id,
                    "title": "Operator-declared population input reference",
                    "source_kind": "operator",
                    "description": (
                        "Identifier supplied with operator population/selection input; external "
                        "citation and applicability have not been verified."
                    ),
                },
            )
        if OPERATOR_POPULATION_SOURCE_ID in population.population.source_ids:
            source_records.setdefault(
                OPERATOR_POPULATION_SOURCE_ID,
                {
                    "id": OPERATOR_POPULATION_SOURCE_ID,
                    "title": "Operator-entered population generation conditions",
                    "source_kind": "operator",
                    "description": (
                        "Rates and categories were entered by the scenario operator and are not "
                        "regional statistics."
                    ),
                },
            )
        for record in request.operational_conditions.provenance_sources():
            source_records[str(record["id"])] = record
        for assumption in plan_capacity_assumptions:
            source_records[str(assumption["source_id"])] = {
                "id": assumption["source_id"],
                "title": str(assumption["subject"]),
                "source_kind": "assumption",
                "description": str(assumption["reason"]),
            }
        missing_population_sources = set(population.population.source_ids) - set(source_records)
        if missing_population_sources:
            approval_blockers.append(
                "人口・世帯入力の出典が調査結果に見つかりません。実データと仮定を確認してください: "
                + ", ".join(sorted(missing_population_sources))
            )
        missing_condition_sources = set(
            request.operational_conditions.referenced_source_ids()
        ) - set(source_records)
        if missing_condition_sources:
            approval_blockers.append(
                "運営条件の出典IDが調査結果または入力者・仮定の出典記録にありません: "
                + ", ".join(sorted(missing_condition_sources))
            )
        population_assumptions = population.assumptions
        duration_minutes = max(1, round(request.duration_days * 1440))
        operational_conditions, provisioning = with_standard_provisioning(
            request.operational_conditions,
            population=request.evacuees,
            duration_minutes=duration_minutes,
            supply_start_day=request.default_supply_start_day,
        )
        initial_stock, scheduled_events, operational_model_configs, condition_blockers = (
            operational_conditions.compile(
                duration_minutes=duration_minutes,
                zone_ids=set(zone_ids),
                facilities=facilities,
                staff=staff,
            )
        )
        approval_blockers.extend(condition_blockers)
        for record in operational_conditions.provenance_sources():
            source_records[str(record["id"])] = record
        if provisioning is not None:
            source_records[STANDARD_PROVISIONING_SOURCE_ID] = {
                "id": STANDARD_PROVISIONING_SOURCE_ID,
                "title": "Standard food and water provisioning",
                "source_kind": "assumption",
                "description": (
                    "Drinking water of 3 L per person per day and three days of stock follow "
                    "national household guidance; outside supply starts on the chosen day."
                ),
            }
            model_assumptions = (*model_assumptions, provisioning)
        if "resource_service" in operational_model_configs:
            source_records[RESOURCE_SERVICE_ASSUMPTION_ID] = {
                "id": RESOURCE_SERVICE_ASSUMPTION_ID,
                "title": "Generic water and meal service conversion",
                "source_kind": "assumption",
                "description": (
                    "The opt-in service model uses five-minute service duration, 250 ml water, "
                    "and one meal unit per completed service; calibrate these assumptions before "
                    "operational use."
                ),
            }
            model_assumptions = (
                *model_assumptions,
                {
                    "id": RESOURCE_SERVICE_ASSUMPTION_ID,
                    "subject": "resource_service_conversion",
                    "value": {
                        "service_minutes": 5,
                        "water_ml_per_service": 250,
                        "food_meal_units_per_service": 1,
                    },
                    "reason": (
                        "Generic service conversion coefficients are assumptions, not measured "
                        "staffing or consumption data."
                    ),
                    "source_id": RESOURCE_SERVICE_ASSUMPTION_ID,
                },
            )
        if "infrastructure" in operational_model_configs:
            infrastructure_config = operational_model_configs["infrastructure"]
            infrastructure_source_id = str(infrastructure_config["source_id"])
            source_records[infrastructure_source_id] = {
                "id": infrastructure_source_id,
                "title": "Scenario infrastructure configuration",
                "source_kind": "assumption",
                "description": (
                    "Infrastructure dependencies and environmental coefficients are explicitly "
                    "configured assumptions, not measured facility engineering data."
                ),
            }
            model_assumptions = (
                *model_assumptions,
                {
                    "id": infrastructure_source_id,
                    "subject": "infrastructure_dependencies_and_coefficients",
                    "value": infrastructure_config,
                    "reason": (
                        "Configured dependency edges and stress coefficients are assumptions. "
                        "Event timing and targets come from separate condition inputs."
                    ),
                    "source_id": infrastructure_source_id,
                },
            )
        for assumption in model_assumptions:
            if assumption["source_id"] not in {item["source_id"] for item in facility_assumptions}:
                source_records.setdefault(
                    str(assumption["source_id"]),
                    {
                        "id": assumption["source_id"],
                        "title": str(assumption["subject"]),
                        "source_kind": "assumption",
                        "description": str(assumption["reason"]),
                    },
                )
        provenance: dict[str, Any] = {
            "generator": "region-initial-scenario-v1",
            "natural_language_input": request.model_dump(mode="json"),
            "sources": [source_records[key] for key in sorted(source_records)],
            "observations": [
                *design.observations,
                *(
                    [
                        {
                            "subject": "uploaded_sleeping_zone_capacity",
                            "value": next(
                                zone.capacity
                                for zone in plan_override.zones
                                if zone.id == "sleeping"
                            ),
                            "source_id": plan_override.asset.source_id,
                            "description": (
                                "Capacity supplied in the operator-uploaded scenario mapping; "
                                "not independently verified."
                            ),
                        }
                    ]
                    if plan_override is not None
                    and next(zone for zone in plan_override.zones if zone.id == "sleeping").capacity
                    is not None
                    else []
                ),
            ],
            "assumptions": [
                *population_assumptions,
                *facility_assumptions,
                *model_assumptions,
                *plan_capacity_assumptions,
            ],
            "profile_generation": population.profile_set.manifest.model_dump(mode="json"),
            "population_generation": {
                "generator": population.generator,
                "household_sizes": population.household_sizes,
                "age_bands": [band.model_dump(mode="json") for band in population.age_bands],
                "household_mix": population.household_mix.model_dump(mode="json"),
                "source_ids": population.population.source_ids,
                "resident_distribution": [
                    band.model_dump(mode="json") for band in population.age_bands
                ],
                "resident_age_distribution": population.resident_age_distribution,
                "evacuee_age_distribution": population.evacuee_age_distribution,
                "evacuation_selection": population.evacuation_selection.model_dump(mode="json"),
                "household_construction": [
                    item.model_dump(mode="json") for item in population.household_relationships
                ],
                "person_requirements_preview": [
                    item.model_dump(mode="json") for item in population.person_requirements
                ],
                "unaccompanied_minor_ids": unaccompanied_minors,
                "shared_population_contract_note": (
                    "Caregiver, pet, medical-support and information-access requirements are "
                    "shown in the region preview. PopulationSpec v1 cannot persist all of them "
                    "as engine traits; see the Round 2 result handoff for the coordinated v2 "
                    "contract."
                ),
            },
            "operational_conditions": request.operational_conditions.model_dump(mode="json"),
            "editable_facility_geometry": editable_geometry.model_dump(mode="json")
            if editable_geometry
            else None,
            "review_required": True,
            "approval_blockers": approval_blockers,
            "unknown_initial_stock": request.operational_conditions.initial_resources.status
            == "unknown",
            "notes": [
                *(
                    [
                        "No starting food or water inventory was entered; the standard "
                        "provisioning assumption was applied."
                    ]
                    if request.operational_conditions.initial_resources.status == "unknown"
                    else []
                ),
                "Synthetic profiles do not represent identifiable real people.",
            ],
        }
        if research is not None:
            provenance["region_research"] = research.model_dump(mode="json")
        defaulted = {
            field: getattr(request, field)
            for field in ("duration_days", "seed", "accessible_toilet_count")
            if request.field_origins.get(field) not in {"user", "llm_extracted"}
        }
        if defaulted:
            provenance["assumptions"] = [
                *provenance["assumptions"],
                {
                    "id": "ASM-INITIAL-SCENARIO-DEFAULTS-V1",
                    "subject": "scenario_default_fields",
                    "value": defaulted,
                    "reason": (
                        "These setup values were not explicitly entered in the initial scenario "
                        "text."
                    ),
                    "source_id": "ASM-INITIAL-SCENARIO-DEFAULTS-V1",
                },
            ]
            source_records["ASM-INITIAL-SCENARIO-DEFAULTS-V1"] = {
                "id": "ASM-INITIAL-SCENARIO-DEFAULTS-V1",
                "title": "Initial scenario builder defaults",
                "source_kind": "assumption",
                "description": "Recorded defaults for duration, seed, and accessible toilet count.",
            }
            provenance["sources"] = [source_records[key] for key in sorted(source_records)]
        if request.gross_area_m2 is not None:
            source_records["SRC-OPERATOR-AREA"] = {
                "id": "SRC-OPERATOR-AREA",
                "title": "Operator-entered gross shelter area",
                "source_kind": "operator",
                "area_m2": request.gross_area_m2,
                "description": "Entered by the scenario operator; not independently verified.",
            }
            provenance["sources"] = [source_records[key] for key in sorted(source_records)]
        if selected_shelter is not None:
            provenance["selected_shelter"] = selected_shelter.model_dump(mode="json")
        if plan_override is not None:
            provenance["plan_geometry_notes"] = list(plan_override.notes)
            provenance["facility_geometry_source"] = "operator-traced uploaded plan"
            provenance["plan_upload"] = plan_override.asset.model_dump(mode="json")
            if plan_override.source_provenance is not None:
                provenance["plan_geometry_source_provenance"] = (
                    plan_override.source_provenance.model_dump(mode="json")
                )
            source_records[plan_override.asset.source_id] = {
                "id": plan_override.asset.source_id,
                "title": plan_override.asset.file_name,
                "source_kind": "operator",
                "url": plan_override.asset.source_url,
                "sha256": plan_override.asset.sha256,
                "description": plan_override.asset.notice,
            }
            if plan_override.extraction_proposal is not None:
                extraction = plan_override.extraction_proposal
                provenance["plan_extraction_review"] = extraction.model_dump(mode="json")
                source_records[extraction.extraction_source_id] = {
                    "id": extraction.extraction_source_id,
                    "title": "External PDF/image extraction proposal",
                    "source_kind": "assumption",
                    "description": (
                        "Extraction output is untrusted source evidence. Only separately "
                        "human-accepted features were mapped into the facility trace."
                    ),
                }
                for feature in extraction.features:
                    source_records.setdefault(
                        feature.extraction_source_id,
                        {
                            "id": feature.extraction_source_id,
                            "title": f"Plan feature extraction: {feature.feature_kind}",
                            "source_kind": "assumption",
                            "description": (
                                "Feature-level extractor output; confidence is not a human "
                                "confirmation."
                            ),
                        },
                    )
            provenance["sources"] = [source_records[key] for key in sorted(source_records)]
        scenario_identity = {
            "request": request.model_dump(mode="json"),
            "research_time": research.researched_at.isoformat() if research else None,
            "geometry": geometry.model_dump(mode="json"),
            "zones": [zone.model_dump(mode="json") for zone in zones],
            "zone_edges": [edge.model_dump(mode="json") for edge in edges],
            "facilities": [facility.model_dump(mode="json") for facility in facilities],
            "staff": [member.model_dump(mode="json") for member in staff],
            "population": population.population.model_dump(mode="json"),
            "profiles": [
                profile.model_dump(mode="json") for profile in population.profile_set.profiles
            ],
            "selected_shelter_id": selected_shelter_id,
            "selected_shelter": selected_shelter.model_dump(mode="json")
            if selected_shelter
            else None,
        }
        scenario_id = (
            "region-"
            + hashlib.sha256(
                json.dumps(scenario_identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16]
        )
        model_configs: dict[str, dict[str, Any]] = {
            "behavior": {
                "decision_interval_minutes": 15,
                "temperature": 0.2,
                **(
                    {"sleep_debt_utility_weight": 1.0}
                    if request.behavior_policy_id == "sleep-aware-utility-softmax-v2"
                    else {}
                ),
                "mode": "offline",
                "policy": request.behavior_policy_id,
                "source_type": "assumption",
                "source_id": (
                    "ASM-PRIORITY-RULE-BEHAVIOR-V1"
                    if request.behavior_policy_id == "priority-rule-v1"
                    else (
                        "ASM-UTILITY-BEHAVIOR-V1"
                        if request.behavior_policy_id == "utility-softmax-v1"
                        else "ASM-SLEEP-AWARE-BEHAVIOR-V2"
                    )
                ),
            },
            "staff": {
                "support_task_minutes": 10,
                "unresolved_after_minutes": 90,
                "support_cooldown_minutes": 30,
                "support_fairness_minutes": 30,
                "source_type": "assumption",
                "source_id": "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
            },
            "toilet_service": {
                "duration_distribution": "discrete_uniform_inclusive",
                "minimum_minutes": 4,
                "maximum_minutes": 8,
                "source_type": "assumption",
                "source_id": "ASM-TOILET-SERVICE-DURATION-V1",
            },
            "facility_queues": {
                "schema_version": 1,
                "retry_delay_minutes": 15,
                "persistence_urgency_threshold": 0.8,
            },
            "facility_geometry": geometry.model_dump(mode="json"),
            "population": population.population.model_dump(mode="json"),
        }
        from sheltersim.domain.body_state import (
            BODY_SOURCE_ID,
            canonical_body_config,
        )
        from sheltersim.domain.movement import MOVEMENT_SOURCE_ID, canonical_movement_config
        from sheltersim.domain.staff_operations import (
            STAFF_OPERATIONS_SOURCE_ID,
            canonical_staff_operations_config,
        )
        from sheltersim.experiments.scenario_connections import connect_generated_context

        model_configs.update(connect_generated_context(population, editable_geometry, geometry))
        from dataclasses import asdict

        from sheltersim.domain.models import InformationSpreadConfig
        from sheltersim.policies.social import SocialPropagationParameters

        social_parameters = asdict(SocialPropagationParameters())
        social_parameters["enabled"] = request.social_propagation_enabled
        social_parameters.update(
            {
                "source_type": "assumption",
                "source_id": (
                    "ASM-BOSSE-AFFECT-CONTAGION-V1"
                    if request.social_policy_id == "expressiveness-channel-openness-v1"
                    else "ASM-SOCIAL-PROPAGATION-V1"
                ),
            }
        )
        information_config = InformationSpreadConfig().to_dict()
        information_config["enabled"] = request.information_spread_enabled
        from sheltersim.policies.psychology import (
            IntegratedDistressLifeStatePolicy,
            PsychologyParameters,
        )

        # The research mean-reversion terms are written out so the Scenario shows
        # (and its hash binds) every coefficient the selected model consumes.
        research_defaults = PsychologyParameters()
        model_configs["psychology"] = PsychologyModelConfig(
            natural_recovery_rate_per_minute=research_defaults.natural_recovery_rate_per_minute,
            chronic_retention=research_defaults.chronic_retention,
            chronic_acute_weight=research_defaults.chronic_acute_weight,
            chronic_high_distress_weight=research_defaults.chronic_high_distress_weight,
            high_distress_threshold=research_defaults.high_distress_threshold,
        ).to_model_config()
        model_configs["social_propagation"] = social_parameters
        model_configs["information_spread"] = information_config
        core_suite_selected = (
            request.behavior_policy_id == CORE_MODEL_SUITE_MODELS["behavior"]
            and request.social_policy_id == CORE_MODEL_SUITE_MODELS["social"]
            and request.information_policy_id == CORE_MODEL_SUITE_MODELS["information"]
            and request.queue_policy_id == CORE_MODEL_SUITE_MODELS["queue"]
            and request.relationship_policy_id == CORE_MODEL_SUITE_MODELS["relationship"]
            and request.social_proximity_policy_id == CORE_MODEL_SUITE_MODELS["social_proximity"]
            and request.body_dynamics_policy_id == CORE_MODEL_SUITE_MODELS["body_dynamics"]
        )
        # The suite pins the tested policy set. Custom registry choices remain
        # explicit and versioned without the suite label.
        model_configs["policy_registry"] = {
            "schema_version": 3 if core_suite_selected else 2,
            **({"suite_id": CORE_MODEL_SUITE_V2} if core_suite_selected else {}),
            # Schema 2 binds formula metadata; schema 3 additionally pins a model suite.
            # New drafts use the research model; legacy models remain for replay/controls.
            "psychology": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
            "social": request.social_policy_id,
            "information": request.information_policy_id,
            "queue": request.queue_policy_id,
            "relationship": request.relationship_policy_id,
            "social_proximity": request.social_proximity_policy_id,
            "body_dynamics": request.body_dynamics_policy_id,
            "staff_operations": request.staff_operations_policy_id,
            "staff_fatigue": request.staff_fatigue_policy_id,
            "zone_environment": request.zone_temperature_policy_id,
            "zone_rest": request.zone_rest_policy_id,
            "zone_hygiene": request.zone_hygiene_policy_id,
            "zone_privacy": request.zone_privacy_policy_id,
            "care_outcome": request.care_outcome_policy_id,
        }
        if request.social_proximity_policy_id == "distance-sampled-proximity-v1":
            from sheltersim.domain.social_network import PROXIMITY_CONTACT_POLICY_VERSION

            model_configs["social_proximity"] = {
                "schema_version": 1,
                "enabled": True,
                "policy_version": PROXIMITY_CONTACT_POLICY_VERSION,
                "sample_interval_minutes": 15,
                "max_distance_m": 1.5,
                "max_pairs_per_sample": 500,
            }
        model_configs["policy_registry"]["affect"] = "linear-positive-negative-v1"
        model_configs["state_contracts"] = {
            "schema_version": 1,
            "namespaces": {
                "population_presence_v1": {
                    "implementation_id": "population-presence-ledger-v1",
                    "implementation_version": "1",
                    "schema_version": 1,
                }
            },
        }
        model_configs["body_dynamics"] = canonical_body_config(None)
        if core_suite_selected:
            model_configs["body_dynamics"]["awake_sleep_debt_minutes_per_minute"] = 0.5
            model_configs["body_dynamics"]["rest_sleep_debt_recovery_per_minute"] = 0.0
        model_configs["staff_operations"] = canonical_staff_operations_config(None)
        model_configs["shared_operations"] = {
            "schema_version": 1,
            "policy_version": "shared-operations-v1",
        }
        model_configs["movement_model"] = canonical_movement_config(None)
        from sheltersim.domain.geometry_overlay import (
            INSTALLED_FACILITY_PLACEMENT_SOURCE_ID,
            TEMPORARY_ROOM_BED_SOURCE_ID,
            TEMPORARY_ROOM_INTAKE_SOURCE_ID,
        )

        # v4 also places facilities installed during a Run on the floor plan.
        model_configs["geometry_overlay"] = {"schema_version": 4}
        # The only extension this adds beyond the configs above is temporary_spaces_v1,
        # so an operator can approve add_temporary_space (#80). Saved Scenarios
        # without it keep running without that state.
        model_configs["simulator_extensions"] = {"enabled": True}
        # Water outages and staff absences need operational_events_v1, which a
        # Scenario otherwise has only once a delivery is planned (day 4 on) (#65).
        model_configs["operational_events"] = {"enabled": True}
        from sheltersim.domain.temporary_spaces import TEMPORARY_SPACES_SOURCE_ID

        for source_id, title in (
            (BODY_SOURCE_ID, "Experimental life-state feedback"),
            (STAFF_OPERATIONS_SOURCE_ID, "Experimental staff operations"),
            (MOVEMENT_SOURCE_ID, "Experimental spatial movement"),
            (TEMPORARY_ROOM_BED_SOURCE_ID, "Exclusive temporary sleeping places"),
            (TEMPORARY_ROOM_INTAKE_SOURCE_ID, "Physical temporary room intake"),
            (
                INSTALLED_FACILITY_PLACEMENT_SOURCE_ID,
                "Floor-plan placement of installed facilities",
            ),
            (TEMPORARY_SPACES_SOURCE_ID, "Zone-only temporary capacity"),
        ):
            if not any(source["id"] == source_id for source in provenance["sources"]):
                provenance["sources"].append(
                    {
                        "id": source_id,
                        "title": title,
                        "source_kind": "assumption",
                        "description": (
                            "Installed synthetic coefficients; not empirically calibrated."
                        ),
                    }
                )
            provenance["assumptions"].append(
                {
                    "id": source_id,
                    "subject": title,
                    "source_id": source_id,
                    "reason": (
                        "New generated Scenario explicitly enables the installed experimental "
                        "model."
                    ),
                }
            )
        from sheltersim.domain.agent_dynamics import (
            ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION,
            INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID,
            ORIENTATION_FACILITY_KINDS,
            ORIENTATION_FACILITY_SOURCE_ID,
            canonical_agent_dynamics_config,
        )

        # Without this, Agents start knowing only facilities in the sleeping zone
        # and never learn the separate toilet, water or meal zones. v3 also tells
        # present Agents about shared facilities an intervention installs later.
        model_configs["agent_dynamics"] = canonical_agent_dynamics_config(
            {"policy_version": ANNOUNCED_FACILITY_AGENT_DYNAMICS_POLICY_VERSION}
        )
        provenance["sources"].append(
            {
                "id": ORIENTATION_FACILITY_SOURCE_ID,
                "title": "Intake orientation to shared shelter facilities",
                "source_kind": "assumption",
                "description": (
                    "Every Agent is assumed to be shown the shared service facilities at intake; "
                    "not observed at a real shelter."
                ),
            }
        )
        provenance["assumptions"].append(
            {
                "id": ORIENTATION_FACILITY_SOURCE_ID,
                "subject": "initial_facility_knowledge",
                "value": {"known_facility_kinds": sorted(ORIENTATION_FACILITY_KINDS)},
                "source_id": ORIENTATION_FACILITY_SOURCE_ID,
                "reason": (
                    "Agents choose only facilities they know; shared services are known from the "
                    "start, group-specific spaces are learned on arrival."
                ),
            }
        )
        provenance["sources"].append(
            {
                "id": INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID,
                "title": "Announcement of shared facilities installed during a Run",
                "source_kind": "assumption",
                "description": (
                    "Every present Agent is assumed to be told at once about a shared service "
                    "facility installed during the Run; not observed at a real shelter."
                ),
            }
        )
        provenance["assumptions"].append(
            {
                "id": INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID,
                "subject": "installed_facility_knowledge",
                "value": {
                    "announced_facility_kinds": sorted(ORIENTATION_FACILITY_KINDS),
                    "delay_minutes": 0,
                    "reach": "all_present_agents",
                },
                "source_id": INSTALLED_FACILITY_ANNOUNCEMENT_SOURCE_ID,
                "reason": (
                    "There is no information-spread path for facility knowledge, so an installed "
                    "shared facility would otherwise stay unknown to everyone already admitted."
                ),
            }
        )
        provenance["sources"].append(
            {
                "id": IntegratedDistressLifeStatePolicy.source_id,
                "title": "Mean-reverting acute distress with life-state feedback",
                "source_kind": "assumption",
                "description": (
                    "Literature-motivated mean-reversion structure (Kuppens et al. 2010) "
                    "with uncalibrated coefficients; not fitted to shelter data."
                ),
            }
        )
        provenance["assumptions"].append(
            {
                "id": IntegratedDistressLifeStatePolicy.source_id,
                "subject": "psychology_model",
                "value": {"policy_version": INTEGRATED_PSYCHOLOGY_POLICY_VERSION},
                "source_id": IntegratedDistressLifeStatePolicy.source_id,
                "reason": (
                    "New generated Scenarios select the research psychology model; legacy models "
                    "remain only for saved inputs and explicit comparisons."
                ),
            }
        )
        from sheltersim.policies.queueing import (
            NeedWeightedPhysicalQueuePolicy,
            StrictFIFOQueuePolicy,
        )
        from sheltersim.policies.relationships import (
            DistanceDurationRepeatTrustPolicy,
            DurationOnlyTrustPolicy,
        )

        selected_policy_sources = {
            "utility-softmax-v1": (
                "ASM-UTILITY-BEHAVIOR-V1",
                "Utility softmax Agent behavior",
            ),
            "sleep-aware-utility-softmax-v2": (
                "ASM-SLEEP-AWARE-BEHAVIOR-V2",
                "Sleep-debt-aware utility softmax Agent behavior",
            ),
            "priority-rule-v1": (
                "ASM-PRIORITY-RULE-BEHAVIOR-V1",
                "Priority-rule Agent behavior",
            ),
            NeedWeightedPhysicalQueuePolicy.model_id: (
                NeedWeightedPhysicalQueuePolicy.source_id,
                "Facility queue priority and abandonment",
            ),
            StrictFIFOQueuePolicy.model_id: (
                StrictFIFOQueuePolicy.source_id,
                "Strict FIFO facility queue",
            ),
            DistanceDurationRepeatTrustPolicy.model_id: (
                DistanceDurationRepeatTrustPolicy.source_id,
                "Distance and repeated-contact relationship trust",
            ),
            DurationOnlyTrustPolicy.model_id: (
                DurationOnlyTrustPolicy.source_id,
                "Duration-only relationship trust",
            ),
            "body-feedback-complete-v1": (
                "ASM-BODY-FEEDBACK-COMPLETE-V1",
                "Body state complete feedback equation",
            ),
            "body-feedback-complete-v2": (
                "ASM-SLEEP-DEBT-BALANCE-V1",
                "Body state complete feedback with balanced sleep-debt accounting",
            ),
            "staff-urgency-first-v1": (
                "ASM-STAFF-URGENCY-FIRST-V1",
                "Urgency-first routed staff assignment",
            ),
            "zone-temperature-comfort-band-v1": (
                "ASM-ZONE-TEMP-COMFORT-BAND-V1",
                "Temperature comfort-band response",
            ),
            "staff-fatigue-linear-v1": (
                "ASM-STAFF-FATIGUE-LINEAR-V1",
                "Linear staff duty fatigue equation",
            ),
            "zone-rest-multiplicative-v1": (
                "ASM-ZONE-REST-MULTIPLICATIVE-V1",
                "Combined noise and lighting rest response",
            ),
            "zone-hygiene-linear-v1": (
                "ASM-ZONE-HYGIENE-LINEAR-V1",
                "Linear ventilation and hygiene response",
            ),
            "zone-privacy-threshold-v1": (
                "ASM-ZONE-PRIVACY-THRESHOLD-V1",
                "Threshold privacy and discontent response",
            ),
            "body-feedback-sleep-debt-v1": (
                "ASM-BODY-FEEDBACK-SLEEP-DEBT-V1",
                "Sleep-debt-only body stress feedback equation",
            ),
            "staff-travel-first-v1": (
                "ASM-STAFF-TRAVEL-FIRST-V1",
                "Travel-first routed staff assignment",
            ),
            "staff-fatigue-saturating-v1": (
                "ASM-STAFF-FATIGUE-SATURATING-V1",
                "Saturating staff duty fatigue equation",
            ),
            "zone-temperature-midpoint-v1": (
                "ASM-ZONE-TEMP-MIDPOINT-V1",
                "Temperature midpoint response",
            ),
            "zone-rest-limiting-v1": (
                "ASM-ZONE-REST-LIMITING-V1",
                "Limiting-factor noise and lighting rest response",
            ),
            "zone-hygiene-quadratic-v1": (
                "ASM-ZONE-HYGIENE-QUADRATIC-V1",
                "Quadratic ventilation and hygiene response",
            ),
            "zone-privacy-proportional-v1": (
                "ASM-ZONE-PRIVACY-PROPORTIONAL-V1",
                "Proportional privacy and discontent response",
            ),
            "hygiene-support-fixed-relief-v1": (
                "ASM-CARE-HYGIENE-FIXED-RELIEF-V1",
                "Fixed experimental hygiene-support response",
            ),
            "hygiene-support-proportional-relief-v1": (
                "ASM-CARE-HYGIENE-PROPORTIONAL-RELIEF-V1",
                "Proportional experimental hygiene-support response",
            ),
            "typed-care-support-response-v1": (
                "ASM-CARE-TYPED-SUPPORT-RESPONSE-V1",
                "Subtype-specific experimental psychological care response",
            ),
            "sparse-family-zone-contact-v1": (
                "ASM-SOCIAL-PROPAGATION-V1",
                "Sparse family and zone stress propagation",
            ),
            "expressiveness-channel-openness-v1": (
                "ASM-BOSSE-AFFECT-CONTAGION-V1",
                "Expressiveness and channel openness affect contagion",
            ),
            "degroot-contact-v1": (
                "ASM-DEGROOT-CONTACT-V1",
                "DeGroot contact stress averaging",
            ),
            "sparse-information-contact-v1": (
                "ASM-INFORMATION-SPREAD-V1",
                "Sparse contact information transmission",
            ),
            "hazard-information-v1": (
                "ASM-HAZARD-INFORMATION-V1",
                "Hazard-based contact information transmission",
            ),
            "evidence-aware-rumor-v1": (
                "ASM-EVIDENCE-AWARE-RUMOR-V1",
                "Evidence-aware rumor belief and correction",
            ),
        }
        for model_id in (
            request.behavior_policy_id,
            request.queue_policy_id,
            request.relationship_policy_id,
            request.body_dynamics_policy_id,
            request.staff_operations_policy_id,
            request.zone_temperature_policy_id,
            request.staff_fatigue_policy_id,
            request.zone_rest_policy_id,
            request.zone_hygiene_policy_id,
            request.zone_privacy_policy_id,
            request.care_outcome_policy_id,
            request.social_policy_id,
            request.information_policy_id,
        ):
            source_id, title = selected_policy_sources[model_id]
            if not any(source["id"] == source_id for source in provenance["sources"]):
                provenance["sources"].append(
                    {
                        "id": source_id,
                        "title": title,
                        "source_kind": "assumption",
                        "description": (
                            "Installed experimental operator; coefficients are not empirically "
                            "calibrated."
                        ),
                    }
                )
            provenance["assumptions"].append(
                {
                    "id": source_id,
                    "subject": title,
                    "source_id": source_id,
                    "reason": (
                        f"Scenario selected installed model {model_id}; coefficients remain "
                        "experimental assumptions."
                    ),
                }
            )
        if request.social_proximity_policy_id == "distance-sampled-proximity-v1":
            from sheltersim.domain.social_network import PROXIMITY_CONTACT_SOURCE_ID

            if not any(
                source.get("id") == PROXIMITY_CONTACT_SOURCE_ID for source in provenance["sources"]
            ):
                provenance["sources"].append(
                    {
                        "id": PROXIMITY_CONTACT_SOURCE_ID,
                        "title": "Distance-sampled physical proximity",
                        "source_kind": "assumption",
                        "description": (
                            "The configured physical distance and sampling frequency are "
                            "experimental assumptions; proximity is not dialogue and is not "
                            "calibrated to shelter observations."
                        ),
                    }
                )
            provenance["assumptions"].append(
                {
                    "id": PROXIMITY_CONTACT_SOURCE_ID,
                    "subject": "Physical proximity sampling",
                    "source_id": PROXIMITY_CONTACT_SOURCE_ID,
                    "reason": (
                        "Scenario selected distance-sampled-proximity-v1 with a 15-minute "
                        "interval, 1.5-metre threshold and 500-pair sample cap."
                    ),
                }
            )
        provenance["population_generation"]["shared_population_contract_note"] = (
            "Requirements persist in model_configs.population_context alongside PopulationSpec v1; "
            "recorded requirements do not imply that every corresponding behavior is simulated."
        )
        model_configs.update(operational_model_configs)
        from sheltersim.domain.staff_dynamics import (
            SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
            canonical_staff_dynamics_config,
        )

        if request.operational_conditions.staff_shifts:
            windows: dict[str, list[dict[str, int]]] = {}
            for shift in request.operational_conditions.staff_shifts:
                if shift.staff_id not in {member.id for member in staff}:
                    raise ValueError("unknown_staff_id")
                windows.setdefault(shift.staff_id, []).append(
                    {"start_minute": shift.start_minute, "end_minute": shift.end_minute}
                )
            model_configs["staff_dynamics"] = canonical_staff_dynamics_config(
                {"shift_windows_by_staff": windows}
            )
        else:
            model_configs["staff_dynamics"] = canonical_staff_dynamics_config(None)
        if "staff_dynamics.shift_windows_by_staff.*" in request.model_parameter_overrides:
            model_configs["staff_dynamics"] = canonical_staff_dynamics_config(
                {
                    "policy_version": SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
                    "shift_windows_by_staff": model_configs["staff_dynamics"].get(
                        "shift_windows_by_staff", {}
                    ),
                }
            )
        from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig

        model_configs.setdefault(
            "zone_environment",
            ScenarioZoneEnvironmentConfig.model_validate(
                {"policy_version": "zone-environment-v2"}
            ).model_dump(mode="json"),
        )
        applied_overrides = _apply_selected_policy_parameter_overrides(
            model_configs,
            request.model_parameter_overrides,
            entity_ids={
                "per_agent_id": {profile.id for profile in population.profile_set.profiles},
                "per_staff_id": {member.id for member in staff},
                "per_zone_id": {zone.id for zone in zones},
            },
        )
        for item in applied_overrides:
            source_id = item["source_id"]
            if not any(source.get("id") == source_id for source in provenance["sources"]):
                provenance["sources"].append(
                    {
                        "id": source_id,
                        "source_kind": "assumption",
                        "title": f"{item['model_id']} coefficient metadata",
                        "description": (
                            "Installed experimental coefficient metadata; no empirical "
                            "calibration claim."
                        ),
                    }
                )
            assumption_record = {
                "id": "ASM-POLICY-OVERRIDE-"
                + hashlib.sha256(item["path"].encode("utf-8")).hexdigest()[:12].upper(),
                "subject": item["path"],
                "value": item["value"],
                "unit": item["unit"],
                "evidence_grade": item["evidence_grade"],
                "applicability": item["applicability"],
                "source_id": source_id,
                "reason": (
                    f"Operator-selected value for the selected installed model {item['model_id']}; "
                    "validated against Model Catalog bounds and included in the Scenario "
                    "configuration hash. "
                    "It is not empirically calibrated."
                ),
            }
            for metadata_field in ("scope", "field_units", "field_bounds"):
                if metadata_field in item:
                    assumption_record[metadata_field] = item[metadata_field]
            provenance["assumptions"].append(assumption_record)
        for key in ("zone_environment", "staff_dynamics"):
            config = model_configs.get(key, {})
            if config.get("source_id") and not any(
                source["id"] == config["source_id"] for source in provenance["sources"]
            ):
                provenance["sources"].append(
                    {
                        "id": config["source_id"],
                        "source_kind": "assumption",
                        "title": "Installed experimental " + key,
                        "description": (
                            "Uncalibrated operator coefficients; no empirical validity claim."
                        ),
                    }
                )
        if core_suite_selected:
            from sheltersim.experiments.model_suite import (
                CORE_MODEL_SUITE_SOURCE_ID,
                SLEEP_DEBT_BALANCE_SOURCE_ID,
            )

            for source_id, title, description in (
                (
                    CORE_MODEL_SUITE_SOURCE_ID,
                    CORE_MODEL_SUITE_V2,
                    (
                        "Versioned combination of installed operators; internally tested, "
                        "not empirically validated."
                    ),
                ),
                (
                    SLEEP_DEBT_BALANCE_SOURCE_ID,
                    "Balanced sleep-debt accounting assumption",
                    (
                        "Required-sleep balance and no-recovery-during-rest coefficients "
                        "are synthetic assumptions, not clinical estimates."
                    ),
                ),
            ):
                if not any(source["id"] == source_id for source in provenance["sources"]):
                    provenance["sources"].append(
                        {
                            "id": source_id,
                            "source_kind": "assumption",
                            "title": title,
                            "description": description,
                        }
                    )
                if not any(item.get("id") == source_id for item in provenance["assumptions"]):
                    provenance["assumptions"].append(
                        {
                            "id": source_id,
                            "subject": title,
                            "source_id": source_id,
                            "reason": description,
                        }
                    )
        provenance["sources"].append(
            {
                "id": "ASM-AFFECT-TRANSFER-V1",
                "source_kind": "assumption",
                "title": "Installed affect transfer",
                "description": (
                    "Synthetic contact transfer equation, not calibrated to measured shelter "
                    "outcomes."
                ),
            }
        )
        scenario = ScenarioSpec(
            schema_version=3,
            id=scenario_id,
            duration_minutes=duration_minutes,
            seed=request.seed,
            zones=zones,
            zone_edges=edges,
            facilities=facilities,
            staff=staff,
            profiles=population.profile_set.profiles,
            scheduled_events=scheduled_events,
            initial_stock=initial_stock,
            model_configs=model_configs,
            provenance=provenance,
        )
        from sheltersim.experiments.service_capacity import service_capacity_diagnostic

        warnings = tuple(
            [
                *design_warnings,
                *(plan_override.notes if plan_override is not None else ()),
                *_research_warnings(research),
                *service_capacity_diagnostic(scenario)["warnings"],
            ]
        )
        return InitialScenarioDraft(
            scenario=scenario,
            facility_design=design if plan_override is None else None,
            plan_geometry=plan_override,
            editable_geometry=editable_geometry,
            population_generation=population,
            warnings=warnings,
            review_required=True,
            approval_blockers=tuple(approval_blockers),
            can_start=not approval_blockers,
        )


def _research_warnings(research: RegionResearchResult | None) -> tuple[str, ...]:
    if research is None:
        return ("地域調査結果が添付されていません。自治体・災害情報は未確認です。",)
    return (
        *research.unknowns,
        *research.warnings,
        *population_statistics_notes(research.population_observations),
    )


def _apply_selected_policy_parameter_overrides(
    model_configs: dict[str, dict[str, Any]],
    overrides: dict[str, Any],
    *,
    entity_ids: dict[str, set[str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Apply catalogued coefficients consumed by selected formulas or operators."""
    if not overrides:
        return ()
    from sheltersim.contracts.policies import model_catalog, selected_models
    from sheltersim.domain.body_state import canonical_body_config
    from sheltersim.domain.environment import ScenarioZoneEnvironmentConfig
    from sheltersim.domain.facilities import TOILET_SERVICE_POLICY_VERSION
    from sheltersim.domain.movement import canonical_movement_config
    from sheltersim.domain.staff import STAFF_SUPPORT_POLICY_VERSION
    from sheltersim.domain.staff_dynamics import (
        SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
        STAFF_DYNAMICS_POLICY_VERSION,
        canonical_staff_dynamics_config,
    )
    from sheltersim.domain.staff_operations import canonical_staff_operations_config
    from sheltersim.experiments.scenario import (
        RESOURCE_SERVICE_POLICY_ID,
        ResourceServiceConfig,
        ToiletServiceConfig,
    )

    formula_domains = {
        "behavior",
        "psychology",
        "social",
        "information",
        "affect",
        "relationship",
        "queue",
        "body_dynamics",
        "care_outcome",
        "staff_fatigue",
        "staff_operations",
        "zone_environment",
        "zone_rest",
        "zone_hygiene",
        "zone_privacy",
        "social_proximity",
    }
    selected = selected_models(model_configs)
    catalog = model_catalog()
    operator_catalog: dict[str, Any] | None = None
    from sheltersim.domain.simulator_state import simulator_signatures

    simulator_models = simulator_signatures(model_configs)
    active_simulator_model_ids: list[str] = []
    if "operator:facility_awareness" in simulator_models:
        from sheltersim.domain.facility_awareness import FACILITY_AWARENESS_POLICY_VERSION

        active_simulator_model_ids.append(FACILITY_AWARENESS_POLICY_VERSION)
    if "simulator:movement_v1" in simulator_models:
        from sheltersim.domain.movement import MOVEMENT_POLICY_VERSION

        active_simulator_model_ids.append(MOVEMENT_POLICY_VERSION)
    if "simulator:staff_operations_v1" in simulator_models:
        from sheltersim.domain.staff_operations import STAFF_OPERATIONS_POLICY_VERSION

        active_simulator_model_ids.append(STAFF_OPERATIONS_POLICY_VERSION)
    if "resource_service" in model_configs:
        active_simulator_model_ids.append(RESOURCE_SERVICE_POLICY_ID)
    if "toilet_service" in model_configs:
        active_simulator_model_ids.append(TOILET_SERVICE_POLICY_VERSION)
    if "staff" in model_configs:
        active_simulator_model_ids.append(STAFF_SUPPORT_POLICY_VERSION)
    staff_dynamics_version = model_configs.get("staff_dynamics", {}).get("policy_version")
    if staff_dynamics_version in {
        STAFF_DYNAMICS_POLICY_VERSION,
        SHIFT_STAFF_DYNAMICS_POLICY_VERSION,
    }:
        active_simulator_model_ids.append(staff_dynamics_version)
    applied: list[dict[str, Any]] = []
    touched_sections: set[str] = set()
    for path, raw_value in sorted(overrides.items()):
        consumers = [
            (domain, model_id, catalog[model_id]["parameters"].get(path))
            for domain, model_id in selected.items()
            if domain in formula_domains
            and path in catalog[model_id]["parameters"]
            and (
                type(catalog[model_id]["parameters"][path].get("default")) in (int, float)
                or catalog[model_id]["parameters"][path].get("scope")
                in {
                    "per_staff_id",
                    "per_service",
                    "per_zone_id",
                    "per_agent_id",
                    "per_task_kind",
                    "per_staff_shift_windows",
                }
            )
        ]
        section = path.split(".", 1)[0]
        if (
            not consumers
            and section
            in {"movement_model", "resource_service", "toilet_service", "staff_dynamics", "staff"}
            and section in model_configs
        ):
            if operator_catalog is None:
                from sheltersim.contracts.evidence import operator_evidence_catalog

                operator_catalog = operator_evidence_catalog()
            for model_id in active_simulator_model_ids:
                if (
                    model_id not in operator_catalog
                    or path not in operator_catalog[model_id]["parameters"]
                ):
                    continue
                definition = dict(operator_catalog[model_id]["parameters"][path])
                if section in {"resource_service", "toilet_service", "staff_dynamics", "staff"}:
                    definition["source_id"] = model_configs[section]["source_id"]
                consumers.append((section, model_id, definition))
        owner_priority = (
            ("body_dynamics", "care_outcome", "affect", "relationship", "psychology")
            if section == "body_dynamics"
            else (section,)
        )
        for owner in owner_priority:
            owner_consumers = [row for row in consumers if row[0] == owner]
            if owner_consumers:
                # Broad life-state metadata shares the body config with the
                # dedicated body, care, affect and relationship operators. Prefer
                # the narrow operator that owns the coefficient when one declares it.
                consumers = owner_consumers
                break
        if not consumers:
            raise ValueError(
                f"policy parameter {path!r} is not consumed by a selected installed formula"
            )
        # A shared runtime config can be fingerprinted by more than one model. The
        # selected coefficient owner below supplies the unit/bounds metadata; the
        # config change is included in every model signature that reads that section.
        units = {definition["unit"] for _, _, definition in consumers}
        if len(units) != 1:
            raise ValueError(f"selected policies have incompatible metadata for {path!r}")
        domain, model_id, definition = next(
            (row for row in consumers if row[0] == section), consumers[0]
        )
        path_parts = path.split(".")
        structured_scopes = {
            "per_staff_id",
            "per_service",
            "per_zone_id",
            "per_agent_id",
            "per_task_kind",
            "per_staff_shift_windows",
        }
        if (
            len(path_parts) not in (2, 3)
            or len(path_parts) == 3
            and path_parts[2] == "*"
            and definition.get("scope") not in structured_scopes
            or definition.get("scope") in structured_scopes
            and (len(path_parts) != 3 or path_parts[2] != "*")
        ):
            raise ValueError(f"policy parameter {path!r} has an unsupported structured path")
        section_config = model_configs.get(section)
        scope = definition.get("scope")
        scalar_config = section_config
        if scope is None and len(path_parts) == 3:
            scalar_config = section_config
            for part in path_parts[1:-1]:
                if not isinstance(scalar_config, dict):
                    raise ValueError(
                        f"selected policy parameter {path!r} has no object runtime config field"
                    )
                scalar_config = scalar_config.get(part)
        field = path_parts[1] if scope is not None or len(path_parts) == 2 else path_parts[-1]
        if not isinstance(section_config, dict) or (
            scope is None
            and (not isinstance(scalar_config, dict) or field not in scalar_config)
            or scope is not None
            and field not in section_config
            and scope not in structured_scopes
        ):
            raise ValueError(f"selected policy parameter {path!r} has no runtime config field")
        if scope in structured_scopes:
            if len(path_parts) != 3 or not isinstance(raw_value, dict):
                raise ValueError(f"policy parameter {path!r} requires a bounded entity map")
            known_ids = (
                (entity_ids or {}).get("per_staff_id", set())
                if scope in {"per_staff_id", "per_staff_shift_windows"}
                else {"toilet", "water", "meal"}
                if scope == "per_service"
                else {"cleaning", "maintenance", "information_outreach", "intake", "patrol"}
                if scope == "per_task_kind"
                else (entity_ids or {}).get("per_zone_id", set())
                if scope == "per_zone_id"
                else (entity_ids or {}).get("per_agent_id", set())
            )
            if set(raw_value) - known_ids:
                reason = (
                    "unknown staff IDs"
                    if scope in {"per_staff_id", "per_staff_shift_windows"}
                    else "unsupported service names"
                    if scope == "per_service"
                    else "unknown task kinds"
                    if scope == "per_task_kind"
                    else "unknown zone IDs"
                    if scope == "per_zone_id"
                    else "unknown Agent IDs"
                )
                raise ValueError(f"policy parameter {path!r} references {reason}")
            if (
                scope
                in {
                    "per_service",
                    "per_zone_id",
                    "per_agent_id",
                    "per_task_kind",
                    "per_staff_shift_windows",
                }
                and not raw_value
            ):
                label = (
                    "service"
                    if scope == "per_service"
                    else "task kind"
                    if scope == "per_task_kind"
                    else "staff shift schedule"
                    if scope == "per_staff_shift_windows"
                    else "zone"
                    if scope == "per_zone_id"
                    else "Agent"
                )
                raise ValueError(f"policy parameter {path!r} requires at least one {label} value")
            current = section_config.get(field, {})
            if not isinstance(current, dict):
                raise ValueError(
                    f"selected policy parameter {path!r} has no object runtime config field"
                )
            if scope == "per_zone_id":
                field_units = definition.get("field_units")
                field_bounds = definition.get("field_bounds")
                if not isinstance(field_units, dict) or not isinstance(field_bounds, dict):
                    raise ValueError(
                        f"selected policy parameter {path!r} lacks typed per-zone metadata"
                    )
                normalized_value: dict[str, dict[str, float]] = {}
                for zone_id, raw_fields in sorted(raw_value.items()):
                    if not isinstance(raw_fields, dict) or not raw_fields:
                        raise ValueError(
                            f"policy parameter {path!r} requires a non-empty field map for "
                            "each zone"
                        )
                    if set(raw_fields) - set(field_units):
                        raise ValueError(
                            f"policy parameter {path!r} contains unsupported per-zone fields"
                        )
                    normalized_fields: dict[str, float] = {}
                    for field_name, raw_field_value in sorted(raw_fields.items()):
                        if type(raw_field_value) not in (int, float) or not math.isfinite(
                            raw_field_value
                        ):
                            raise ValueError(
                                f"policy parameter {path!r} requires finite numeric zone fields"
                            )
                        field_value = float(raw_field_value)
                        bounds = field_bounds.get(field_name)
                        if (
                            not isinstance(bounds, list)
                            or len(bounds) != 2
                            or field_value < bounds[0]
                            or field_value > bounds[1]
                        ):
                            raise ValueError(
                                f"policy parameter {path!r} is outside its Model Catalog bounds"
                            )
                        normalized_fields[field_name] = field_value
                    zone_baseline = dict(current.get(zone_id, {}))
                    zone_baseline.update(normalized_fields)
                    current[zone_id] = zone_baseline
                    normalized_value[zone_id] = normalized_fields
            elif scope == "per_staff_shift_windows":
                from sheltersim.domain.staff_dynamics import validate_staff_shift_windows_override

                normalized_value = validate_staff_shift_windows_override(
                    raw_value,
                    known_staff_ids=(entity_ids or {}).get("per_staff_id", set()),
                )
            elif scope == "per_agent_id":
                normalized_value: dict[str, float] = {}
                for agent_id, raw_rate in sorted(raw_value.items()):
                    if type(raw_rate) not in (int, float) or not math.isfinite(raw_rate):
                        raise ValueError(f"policy parameter {path!r} requires finite Agent rates")
                    rate = float(raw_rate)
                    if (
                        definition.get("lower") is not None
                        and rate < definition["lower"]
                        or definition.get("upper") is not None
                        and rate > definition["upper"]
                    ):
                        raise ValueError(
                            f"policy parameter {path!r} is outside its Model Catalog bounds"
                        )
                    if definition.get("value_type") == "integer":
                        if not rate.is_integer():
                            raise ValueError(
                                f"policy parameter {path!r} requires integer Agent values"
                            )
                        normalized_value[agent_id] = int(rate)
                    else:
                        normalized_value[agent_id] = rate
            else:
                normalized_value: dict[str, int] = {}
                for entity_id, raw_minutes in sorted(raw_value.items()):
                    if type(raw_minutes) not in (int, float) or not math.isfinite(raw_minutes):
                        label = (
                            "service"
                            if scope == "per_service"
                            else "task-kind"
                            if scope == "per_task_kind"
                            else "staff-specific"
                        )
                        raise ValueError(
                            f"policy parameter {path!r} requires finite {label} minutes"
                        )
                    minutes = float(raw_minutes)
                    if (
                        definition.get("lower") is not None
                        and minutes < definition["lower"]
                        or definition.get("upper") is not None
                        and minutes > definition["upper"]
                    ):
                        raise ValueError(
                            f"policy parameter {path!r} is outside its Model Catalog bounds"
                        )
                    if not minutes.is_integer():
                        label = (
                            "service"
                            if scope == "per_service"
                            else "task-kind"
                            if scope == "per_task_kind"
                            else "staff-specific"
                        )
                        raise ValueError(
                            f"policy parameter {path!r} requires integer {label} minutes"
                        )
                    normalized_value[entity_id] = int(minutes)
            if scope != "per_zone_id":
                current.update(normalized_value)
            section_config[field] = current
        else:
            if type(raw_value) not in (int, float) or not math.isfinite(raw_value):
                raise ValueError(f"policy parameter {path!r} requires a finite numeric value")
            value = float(raw_value)
            outside_bounds = any(
                (
                    candidate.get("lower") is not None
                    and (
                        value < candidate["lower"]
                        or candidate.get("lower_exclusive")
                        and value == candidate["lower"]
                    )
                )
                or (candidate.get("upper") is not None and value > candidate["upper"])
                for _, _, candidate in consumers
            )
            if outside_bounds:
                raise ValueError(f"policy parameter {path!r} is outside its Model Catalog bounds")
            if type(scalar_config[field]) is int:
                if not value.is_integer():
                    raise ValueError(f"policy parameter {path!r} requires an integer value")
                normalized_value = int(value)
            else:
                normalized_value = value
            scalar_config[field] = normalized_value
        touched_sections.add(section)
        applied_record = {
            "path": path,
            "value": normalized_value,
            "unit": definition["unit"],
            "source_id": definition["source_id"] or catalog[model_id]["assumption_source_id"],
            "model_id": model_id,
            "consumer_model_ids": [candidate_id for _, candidate_id, _ in consumers],
            "consumer_source_ids": sorted(
                {
                    candidate["source_id"] or catalog[candidate_id]["assumption_source_id"]
                    for _, candidate_id, candidate in consumers
                }
            ),
            "evidence_grade": definition["evidence_grade"],
            "applicability": definition["applicability"],
        }
        if scope is not None:
            applied_record["scope"] = scope
        if scope == "per_zone_id":
            applied_record["field_units"] = dict(definition["field_units"])
            applied_record["field_bounds"] = {
                field_name: list(bounds)
                for field_name, bounds in definition["field_bounds"].items()
            }
        applied.append(applied_record)

    if "body_dynamics" in touched_sections:
        model_configs["body_dynamics"] = canonical_body_config(model_configs["body_dynamics"])
    if "staff_dynamics" in touched_sections:
        model_configs["staff_dynamics"] = canonical_staff_dynamics_config(
            model_configs["staff_dynamics"]
        )
    if "staff_operations" in touched_sections:
        model_configs["staff_operations"] = canonical_staff_operations_config(
            model_configs["staff_operations"]
        )
    if "movement_model" in touched_sections:
        model_configs["movement_model"] = canonical_movement_config(model_configs["movement_model"])
    if "resource_service" in touched_sections:
        model_configs["resource_service"] = ResourceServiceConfig.model_validate(
            model_configs["resource_service"]
        ).model_dump(mode="json")
    if "toilet_service" in touched_sections:
        model_configs["toilet_service"] = ToiletServiceConfig.model_validate(
            model_configs["toilet_service"]
        ).model_dump(mode="json")
    if "zone_environment" in touched_sections:
        model_configs["zone_environment"] = ScenarioZoneEnvironmentConfig.model_validate(
            model_configs["zone_environment"]
        ).model_dump(mode="json")
    if "psychology" in touched_sections:
        model_configs["psychology"] = PsychologyModelConfig.model_validate(
            model_configs["psychology"]
        ).to_model_config()
    if "social_propagation" in touched_sections:
        from dataclasses import asdict

        from sheltersim.policies.social import SocialPropagationParameters

        parsed_social = SocialPropagationParameters.from_mapping(
            model_configs["social_propagation"]
        )
        model_configs["social_propagation"] = {
            **asdict(parsed_social),
            "source_type": "assumption",
            "source_id": "ASM-SOCIAL-PROPAGATION-V1",
        }
    if "information_spread" in touched_sections:
        from sheltersim.domain.models import InformationSpreadConfig

        model_configs["information_spread"] = InformationSpreadConfig.from_mapping(
            model_configs["information_spread"]
        ).to_dict()
    if "facility_queues" in touched_sections:
        from sheltersim.policies.queueing import create_queue_policy

        create_queue_policy(model_configs)
    return tuple(applied)


def _inaccessible_service_zones(
    *,
    edges: tuple[ZoneEdgeSpec, ...],
    facilities: tuple[FacilitySpec, ...],
    geometry: FacilityGeometry,
) -> tuple[str, ...]:
    accessible_route_ids = {
        route.edge_id
        for route in geometry.routes
        if route.accessible and route.width_m >= ACCESSIBLE_ROUTE_MIN_WIDTH_M
    }
    open_accessible_edges = tuple(
        edge for edge in edges if not edge.closed and edge.id in accessible_route_ids
    )
    reachable = {"sleeping"}
    while True:
        updated = reachable | {
            edge.to_zone_id for edge in open_accessible_edges if edge.from_zone_id in reachable
        }
        if updated == reachable:
            break
        reachable = updated
    returnable = {"sleeping"}
    while True:
        updated = returnable | {
            edge.from_zone_id for edge in open_accessible_edges if edge.to_zone_id in returnable
        }
        if updated == returnable:
            break
        returnable = updated
    core_zones = {
        facility.zone_id
        for facility in facilities
        if facility.kind
        in {"toilet", "water_station", "meal_distribution", "medical_support", "intake"}
    }
    return tuple(sorted(core_zones - (reachable & returnable)))


def _disaster_families(value: str) -> frozenset[str]:
    text = value.casefold()
    groups = (
        ("flood", ("flood", "水害", "洪水", "浸水", "氾濫")),
        ("earthquake", ("earthquake", "地震", "震災")),
        ("tsunami", ("tsunami", "津波")),
        ("landslide", ("landslide", "土砂", "崖崩れ", "がけ崩れ")),
        ("storm_surge", ("storm_surge", "storm surge", "高潮")),
        ("typhoon", ("typhoon", "台風")),
        ("heavy_snow", ("heavy_snow", "heavy snow", "大雪", "豪雪")),
        ("heatwave", ("heatwave", "heat wave", "猛暑", "熱波")),
    )
    matches = {family for family, terms in groups if any(term in text for term in terms)}
    return frozenset(matches or {text.strip()})
