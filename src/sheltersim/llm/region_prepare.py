"""Bounded PREPARE-stage extraction for initial shelter scenario requests."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioDraft,
    InitialScenarioRequest,
)
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.provider import LLMRouter
from sheltersim.region.conditions import (
    OPERATOR_CONDITION_SOURCE_ID,
    InitialResourceCondition,
    OperationalConditions,
)
from sheltersim.region.facility import FacilityZoneCondition
from sheltersim.region.models import RegionModel, RegionResearchResult
from sheltersim.region.plan_import import ImportedPlanGeometry
from sheltersim.region.population import (
    AgeBand,
    EvacuationSelection,
    HouseholdSizeMix,
    PopulationConditionInput,
    age_bands_from_observations,
    household_mix_from_observations,
)

_MAX_INPUT_LENGTH = 4_000
_DISASTER_TERMS = (
    ("flood", ("洪水", "水害", "浸水", "氾濫")),
    ("earthquake", ("地震", "震災")),
    ("tsunami", ("津波",)),
    ("landslide", ("土砂災害", "土砂崩れ", "がけ崩れ")),
    ("storm_surge", ("高潮",)),
    ("typhoon", ("台風",)),
    ("heavy_snow", ("大雪", "豪雪")),
    ("heatwave", ("猛暑", "熱波")),
)


class ScenarioIntentParse(RegionModel):
    request: InitialScenarioRequest | None
    missing_fields: tuple[str, ...] = ()
    source: str
    warnings: tuple[str, ...] = ()
    input_sha256: str


def parse_scenario_text(text: str, *, router: LLMRouter | None = None) -> ScenarioIntentParse:
    """Extract only stated fields; unknown required values remain missing for operator entry."""
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_INPUT_LENGTH:
        raise ValueError("scenario text must contain 1..4000 characters")
    explicit = _parse_explicit_fields(text)
    rule_fields = set(explicit)
    source = "rules"
    warnings: list[str] = []
    if router is not None:
        task = LLMTask(
            stage=LLMStage.PREPARE,
            input_text=(
                "Extract scenario values only when they are explicitly stated in the user's text. "
                "Do not invent municipality, disaster, population, duration, floor area, facility counts, initial stock, schedules, trait percentages, hazard exposure, or official statistics. "
                "Unknown water or food stock must stay null; never convert unknown to zero. "
                "Treat season as context and do not infer temperature effects from season alone. "
                "For an explicitly stated operating schedule, use minutes from scenario start and include only supported typed condition kinds in the schema; convert stated hours or days exactly, and leave unstated start/end times unknown. "
                "Power outages require an explicitly supplied component_id plus infrastructure dependency_edges; equipment failures require a known facility_id and units_out. Never invent IDs or dependency paths. "
                "Use facility_zones only for explicitly requested optional/custom zones; do not remove sleeping, toilet, food, water, intake, medical, staff, or corridor functions. "
                "A disaster name alone is context, not evidence of a facility outage, route closure, heat/cold stress, or damage event. "
                "Use population rates and distributions only when the user stated them or attached source data. "
                "Return an empty string for every unstated scalar and empty arrays or default objects for unstated structured conditions.\n\n"
                + text
            ),
            state_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            prompt_version="region-scenario-extraction-v1",
            schema_version=1,
            output_schema=_intent_json_schema(),
            fallback_text=json.dumps(
                _to_llm_contract(explicit), ensure_ascii=False, separators=(",", ":")
            ),
            max_output_tokens=1_600,
        )
        try:
            prior_record_count = len(router.records)
            output = json.loads(router.complete(task))
            extracted = _parse_llm_contract(output)
            explicit = _merge_explicit(explicit, extracted)
            if len(router.records) > prior_record_count and router.records[-1].fallback_used:
                source = "rules_fallback"
                warnings.append("AIの入力整理を利用できず、明示された値のみを使用します。")
            else:
                source = "prepare_llm"
        except Exception:
            # Extraction is an assistive input boundary; deterministic values remain available.
            warnings.append("AIの入力整理を利用できず、明示された値のみを使用します。")
    required = ("municipality", "disaster", "evacuees")
    missing = tuple(field for field in required if not explicit.get(field))
    request = None
    if not missing:
        field_origins = {field: "user" for field in rule_fields}
        field_origins.update({field: "llm_extracted" for field in set(explicit) - rule_fields})
        for field in ("duration_days", "seed", "accessible_toilet_count"):
            field_origins.setdefault(field, "default")
        request = InitialScenarioRequest.model_validate(
            {
                "municipality": explicit["municipality"],
                "municipality_code": explicit.get("municipality_code"),
                "disaster": explicit["disaster"],
                "evacuees": explicit["evacuees"],
                "duration_days": explicit.get("duration_days")
                if explicit.get("duration_days") is not None
                else 7,
                "seed": explicit.get("seed") if explicit.get("seed") is not None else 2026,
                "gross_area_m2": explicit.get("gross_area_m2"),
                "accessible_toilet_count": explicit.get("accessible_toilet_count")
                if explicit.get("accessible_toilet_count") is not None
                else 1,
                "staff_count": explicit.get("staff_count"),
                "season": explicit.get("season"),
                "facility_conditions": tuple(explicit.get("facility_conditions") or ()),
                "facility_zones": tuple(explicit.get("facility_zones") or ()),
                "operational_conditions": explicit.get("operational_conditions", {}),
                "population_conditions": explicit.get("population_conditions", {}),
                "evacuation_selection": explicit.get("evacuation_selection", {}),
                "zone_area_shares": explicit.get("zone_area_shares", {}),
                "field_origins": field_origins,
            }
        )
    if router is not None:
        warnings.append(
            "生成された条件は下書きです。地域・人数・期間・設備を確認してから確定してください。"
        )
    return ScenarioIntentParse(
        request=request,
        missing_fields=missing,
        source=source,
        warnings=tuple(warnings),
        input_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def build_scenario_from_text(
    text: str,
    *,
    router: LLMRouter | None = None,
    builder: InitialScenarioBuilder | None = None,
    research: RegionResearchResult | None = None,
    research_service: Any | None = None,
    age_bands: tuple[AgeBand, ...] | None = None,
    household_mix: HouseholdSizeMix | None = None,
    selected_shelter_id: str | None = None,
    plan_override: ImportedPlanGeometry | None = None,
) -> tuple[ScenarioIntentParse, InitialScenarioDraft | None]:
    """Build a detached preview after required scope fields are explicitly present."""
    parsed = parse_scenario_text(text, router=router)
    if parsed.request is None:
        return parsed, None
    if research is None:
        from sheltersim.region.models import RegionResearchRequest
        from sheltersim.region.research import RegionResearchService

        active_research_service = research_service or RegionResearchService()
        research = active_research_service.research(
            RegionResearchRequest(
                municipality_query=parsed.request.municipality,
                municipality_code=parsed.request.municipality_code,
                selected_municipality_name=parsed.request.municipality,
                disaster=parsed.request.disaster,
                season=parsed.request.season,
            )
        )
    if age_bands is None:
        age_bands = age_bands_from_observations(research.population_observations)
    if household_mix is None:
        household_mix = household_mix_from_observations(research.population_observations)
    draft = (builder or InitialScenarioBuilder()).build(
        parsed.request,
        research=research,
        age_bands=age_bands,
        household_mix=household_mix,
        selected_shelter_id=selected_shelter_id,
        plan_override=plan_override,
    )
    document = draft.scenario.model_dump(mode="json")
    document["provenance"]["scenario_input"] = {
        "sha256": parsed.input_sha256,
        "extraction_source": parsed.source,
        "extraction_warnings": list(parsed.warnings),
    }
    draft = draft.model_copy(update={"scenario": ScenarioSpec.model_validate(document)})
    return parsed, draft


def _parse_explicit_fields(text: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    municipality_code = re.search(r"(?:自治体|市町村)コード\s*[:：=]?\s*(\d{5})(?!\d)", text)
    if municipality_code:
        values["municipality_code"] = municipality_code.group(1)
    population = re.search(r"([0-9][0-9,]*)\s*(?:人|名|people|evacuees)", text, re.IGNORECASE)
    if population:
        values["evacuees"] = int(population.group(1).replace(",", ""))
    duration = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:日間|日|days?|d)", text, re.IGNORECASE)
    if duration:
        values["duration_days"] = float(duration.group(1))
    else:
        hours = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:時間|hours?|h)", text, re.IGNORECASE)
        weeks = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:週間|weeks?|w)", text, re.IGNORECASE)
        if hours:
            values["duration_days"] = float(hours.group(1)) / 24
        elif weeks:
            values["duration_days"] = float(weeks.group(1)) * 7
    area = re.search(r"([0-9][0-9,.]*)\s*(?:m2|m²|㎡|平方メートル)", text, re.IGNORECASE)
    if area:
        values["gross_area_m2"] = float(area.group(1).replace(",", ""))
    accessible = re.search(
        r"([0-9]+)\s*(?:基|台)\s*(?:の)?\s*(?:車いす|車椅子|バリアフリー)?\s*(?:トイレ|便所)", text
    )
    if accessible is None:
        accessible = re.search(
            r"(?:車いす|車椅子|バリアフリー)?\s*(?:トイレ|便所)\s*([0-9]+)\s*(?:基|台)",
            text,
        )
    if accessible:
        values["accessible_toilet_count"] = int(accessible.group(1))
    staff = re.search(r"([0-9]+)\s*(?:人|名)\s*(?:の)?\s*(?:職員|スタッフ)", text)
    if staff:
        values["staff_count"] = int(staff.group(1))
    water = re.search(
        r"(?:飲料水|備蓄水|水(?:の在庫|在庫)?|water(?: stock)?)\s*[:：=]?\s*([0-9][0-9,.]*)\s*(ml|mL|リットル|ℓ|L|l)",
        text,
        re.IGNORECASE,
    )
    food = re.search(
        r"(?:食料|食糧|食事在庫|food(?: stock)?|meal(?: stock)?)\s*[:：=]?\s*([0-9][0-9,]*)\s*(?:食分|食|meal units?)",
        text,
        re.IGNORECASE,
    )
    water_ml = None
    if water:
        amount = float(water.group(1).replace(",", ""))
        unit = water.group(2).casefold()
        water_ml = int(round(amount * (1_000 if unit in {"l", "ℓ", "リットル"} else 1)))
    food_meal_units = int(food.group(1).replace(",", "")) if food else None
    if water or food:
        status = "entered" if water is not None and food is not None else "incomplete"
        values["operational_conditions"] = OperationalConditions(
            initial_resources=InitialResourceCondition(
                status=status,
                water_ml=water_ml,
                food_meal_units=food_meal_units,
                source_ids=(OPERATOR_CONDITION_SOURCE_ID,),
            )
        ).model_dump(mode="python")
    seed = re.search(r"(?:seed|シード)\s*[:=]?\s*([0-9]+)", text, re.IGNORECASE)
    if seed:
        values["seed"] = int(seed.group(1))
    for season, terms in (
        ("spring", ("春", "spring")),
        ("summer", ("夏", "summer")),
        ("autumn", ("秋", "autumn", "fall")),
        ("winter", ("冬", "winter")),
    ):
        if any(term.casefold() in text.casefold() for term in terms):
            values["season"] = season
            break
    for canonical, terms in _DISASTER_TERMS:
        if any(term in text.casefold() for term in (canonical, *terms)):
            values["disaster"] = canonical
            break
    location = _extract_municipality(text)
    if location:
        values["municipality"] = location
    condition_map = (
        ("ペット", ("ペット", "pet")),
        ("授乳・乳幼児", ("授乳", "乳幼児", "infant")),
        ("個室・プライバシー", ("個室", "プライバシー", "privacy")),
        ("車いす対応", ("車椅子", "車いす", "wheelchair", "バリアフリー")),
    )
    conditions = [
        label
        for label, terms in condition_map
        if any(term.casefold() in text.casefold() for term in terms)
    ]
    if conditions:
        values["facility_conditions"] = conditions
    return values


def _extract_municipality(text: str) -> str | None:
    region_match = re.search(
        r"(?:北海道|東京都|京都府|大阪府|[一-龥ぁ-んァ-ヴー]{2,4}県)"
        r"(?:[一-龥ぁ-んァ-ヴー0-9]+(?:市|区|町|村))?",
        text,
    )
    if region_match:
        return region_match.group(0)
    municipality_match = re.search(r"[一-龥ぁ-んァ-ヴー0-9]{2,24}(?:市|区|町|村)", text)
    if municipality_match:
        return municipality_match.group(0)
    english_match = re.search(
        r"\bin\s+([A-Za-z][A-Za-z .'-]{1,40}?)(?=\s+(?:for|during|with|disaster|flood|earthquake)|[,.;]|$)",
        text,
        re.IGNORECASE,
    )
    return english_match.group(1).strip(" .,'") if english_match else None


def _merge_explicit(explicit: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    merged = dict(explicit)
    for key, value in extracted.items():
        if (
            key == "operational_conditions"
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            merged[key] = _merge_operational_conditions(merged[key], value)
        elif (
            key == "population_conditions"
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            merged[key] = _merge_condition_object(merged[key], value)
        elif (
            key == "evacuation_selection"
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            merged[key] = _merge_condition_object(merged[key], value)
        elif (
            key == "zone_area_shares"
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            merged[key] = merged[key] | value
        elif key not in merged and value is not None:
            merged[key] = value
    return merged


def _merge_operational_conditions(
    explicit: dict[str, Any], extracted: dict[str, Any]
) -> dict[str, Any]:
    merged = _merge_condition_object(explicit, extracted)
    explicit_stock = explicit.get("initial_resources", {})
    extracted_stock = extracted.get("initial_resources", {})
    if isinstance(explicit_stock, dict) and isinstance(extracted_stock, dict):
        stock = _merge_condition_object(explicit_stock, extracted_stock)
        water = explicit_stock.get("water_ml")
        if water is None:
            water = extracted_stock.get("water_ml")
        food = explicit_stock.get("food_meal_units")
        if food is None:
            food = extracted_stock.get("food_meal_units")
        stock["water_ml"] = water
        stock["food_meal_units"] = food
        if water is not None and food is not None:
            stock["status"] = "entered"
        elif water is not None or food is not None:
            stock["status"] = "incomplete"
        stock["source_ids"] = sorted(
            set(explicit_stock.get("source_ids", ())) | set(extracted_stock.get("source_ids", ()))
        )
        merged["initial_resources"] = stock
    for list_key in ("equipment", "scheduled"):
        left = explicit.get(list_key, [])
        right = extracted.get(list_key, [])
        if isinstance(left, list) and isinstance(right, list):
            identity_field = "facility_id" if list_key == "equipment" else None
            if identity_field:
                by_id = {
                    item[identity_field]: item
                    for item in right
                    if isinstance(item, dict) and identity_field in item
                }
                by_id.update(
                    {
                        item[identity_field]: item
                        for item in left
                        if isinstance(item, dict) and identity_field in item
                    }
                )
                merged[list_key] = [by_id[key] for key in sorted(by_id)]
            else:
                merged[list_key] = [*left, *right]
    return merged


def _merge_condition_object(explicit: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    merged = dict(extracted)
    for key, value in explicit.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_condition_object(value, merged[key])
        elif isinstance(value, list) and value and isinstance(merged.get(key), list):
            merged[key] = value
        elif value not in (None, "", [], {}):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def _intent_json_schema() -> dict[str, Any]:
    definitions: dict[str, Any] = {}

    def reference(model: type[RegionModel]) -> dict[str, str]:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
        return {"$ref": f"#/$defs/{model.__name__}"}

    operational_schema = reference(OperationalConditions)
    population_schema = reference(PopulationConditionInput)
    selection_schema = reference(EvacuationSelection)
    facility_zone_schema = reference(FacilityZoneCondition)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "$defs": definitions,
        "properties": {
            "municipality": {"type": "string", "maxLength": 160},
            "disaster": {"type": "string", "maxLength": 80},
            "evacuees": {"type": "string", "maxLength": 12},
            "duration_days": {"type": "string", "maxLength": 16},
            "gross_area_m2": {"type": "string", "maxLength": 16},
            "accessible_toilet_count": {"type": "string", "maxLength": 4},
            "staff_count": {"type": "string", "maxLength": 4},
            "seed": {"type": "string", "maxLength": 12},
            "season": {"type": "string", "maxLength": 40},
            "facility_conditions": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "facility_zones": {"type": "array", "items": facility_zone_schema, "maxItems": 40},
            "operational_conditions": operational_schema,
            "population_conditions": population_schema,
            "evacuation_selection": selection_schema,
            "zone_area_shares": {
                "type": "object",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        "required": [
            "municipality",
            "disaster",
            "evacuees",
            "duration_days",
            "gross_area_m2",
            "accessible_toilet_count",
            "staff_count",
            "seed",
            "season",
            "facility_conditions",
            "facility_zones",
            "operational_conditions",
            "population_conditions",
            "evacuation_selection",
            "zone_area_shares",
        ],
    }
    return schema


def _to_llm_contract(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "municipality": str(values.get("municipality") or ""),
        "disaster": str(values.get("disaster") or ""),
        "evacuees": str(values.get("evacuees") or ""),
        "duration_days": str(values.get("duration_days") or ""),
        "gross_area_m2": str(values.get("gross_area_m2") or ""),
        "accessible_toilet_count": str(values.get("accessible_toilet_count") or ""),
        "staff_count": str(values.get("staff_count") or ""),
        "seed": str(values.get("seed") or ""),
        "season": str(values.get("season") or ""),
        "facility_conditions": list(values.get("facility_conditions") or []),
        "facility_zones": values.get("facility_zones", []),
        "operational_conditions": values.get(
            "operational_conditions", OperationalConditions().model_dump(mode="json")
        ),
        "population_conditions": values.get(
            "population_conditions", PopulationConditionInput().model_dump(mode="json")
        ),
        "evacuation_selection": values.get(
            "evacuation_selection", EvacuationSelection().model_dump(mode="json")
        ),
        "zone_area_shares": values.get("zone_area_shares", {}),
    }


def _parse_llm_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("intent output must be an object")
    parsed: dict[str, Any] = {}
    for field, converter in (
        ("municipality", str),
        ("disaster", str),
        ("evacuees", int),
        ("duration_days", float),
        ("gross_area_m2", float),
        ("accessible_toilet_count", int),
        ("staff_count", int),
        ("seed", int),
        ("season", str),
    ):
        raw = value.get(field, "")
        if raw not in ("", None):
            parsed[field] = converter(raw)
    conditions = value.get("facility_conditions", [])
    if conditions:
        if not isinstance(conditions, list) or any(
            not isinstance(item, str) for item in conditions
        ):
            raise ValueError("facility_conditions must be an array of strings")
        parsed["facility_conditions"] = conditions
    zone_conditions = value.get("facility_zones", [])
    if zone_conditions:
        if not isinstance(zone_conditions, list):
            raise ValueError("facility_zones must be an array")
        parsed["facility_zones"] = [
            FacilityZoneCondition.model_validate(item).model_dump(mode="python")
            for item in zone_conditions
        ]
    if value.get("operational_conditions"):
        parsed["operational_conditions"] = OperationalConditions.model_validate(
            value["operational_conditions"]
        ).model_dump(mode="python")
    if value.get("population_conditions"):
        parsed["population_conditions"] = PopulationConditionInput.model_validate(
            value["population_conditions"]
        ).model_dump(mode="python")
    if value.get("evacuation_selection"):
        parsed["evacuation_selection"] = EvacuationSelection.model_validate(
            value["evacuation_selection"]
        ).model_dump(mode="python")
    if value.get("zone_area_shares"):
        shares = value["zone_area_shares"]
        if not isinstance(shares, dict) or any(
            not isinstance(key, str)
            or not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            for key, weight in shares.items()
        ):
            raise ValueError("zone_area_shares must map standard zone IDs to numeric fractions")
        parsed["zone_area_shares"] = shares
    return parsed
