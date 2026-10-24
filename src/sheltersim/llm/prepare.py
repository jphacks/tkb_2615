"""Constrained, reviewable synthetic scenario generation through the LLM boundary."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.config import LLMMode
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.natural_language import _sanitize_narrative
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import scenario_draft_schema

_PROMPT_VERSION = "synthetic-scenario-draft-v2"
_MAX_GENERATED_EVENTS = 20


def generate_scenario_draft(
    scenario: ScenarioSpec,
    router: LLMRouter,
    *,
    operator_request: str = "",
) -> tuple[ScenarioSpec, dict[str, Any]]:
    """Draft profile and exogenous facility-event inputs, without freezing or running them."""
    if router.mode not in (LLMMode.RECORD, LLMMode.REPLAY):
        raise ValueError(
            "scenario generation requires Claude record mode or a matching replay tape"
        )
    ordered_profiles = sorted(scenario.profiles, key=lambda profile: profile.id)
    ordered_zones = sorted(scenario.zones, key=lambda zone: zone.id)
    ordered_facilities = sorted(scenario.facilities, key=lambda facility: facility.id)
    payload = {
        "task": "Draft a fully synthetic shelter scenario input from anonymous slots.",
        "rules": [
            "All generated values are software assumptions, not forecasts or measurements.",
            "Use every profile_index from 0 through profile_count-1 exactly once.",
            "Use family groups of at most four profile slots; keep each family in one zone.",
            "Use a supplied zone_index and respect each finite zone capacity.",
            "Use only normalized numbers from 0 to 1 and the listed mobility categories.",
            "Generate at most 20 facility_outage or facility_recovery events.",
            "Use the operator request only when its requirements fit these supplied fields and event types.",
            "Do not invent a region, geography, physical hazard mechanics, measurements, or unsupported event types.",
            "Unrepresentable parts of the request must not change the scenario output.",
            "Events must use valid facility_index values and fit the scenario time horizon.",
            "Use only the supplied numeric indexes; do not include names, prose, or personal data.",
            "Return the required JSON object only.",
        ],
        "scenario": {
            "duration_minutes": scenario.duration_minutes,
            "profile_count": len(ordered_profiles),
            "maximum_family_size": 4,
            "zones": [
                {"zone_index": index, "capacity": zone.capacity}
                for index, zone in enumerate(ordered_zones)
            ],
            "facilities": [
                {
                    "facility_index": index,
                    "capacity": facility.capacity,
                    "working_units": (
                        facility.capacity
                        if facility.working_units is None
                        else facility.working_units
                    ),
                }
                for index, facility in enumerate(ordered_facilities)
            ],
            "max_events": _MAX_GENERATED_EVENTS,
        },
        "operator_request": _sanitize_narrative(operator_request)[:1200],
    }
    input_text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    fallback_text = _fallback_output(scenario, ordered_profiles, ordered_zones)
    task = LLMTask(
        stage=LLMStage.PREPARE,
        input_text=input_text,
        state_hash=_digest(scenario.canonical_json()),
        prompt_version=_PROMPT_VERSION,
        schema_version=1,
        output_schema=scenario_draft_schema(scenario, max_events=_MAX_GENERATED_EVENTS),
        fallback_text=fallback_text,
        max_output_tokens=min(8192, max(512, len(ordered_profiles) * 72 + 256)),
    )
    response = router.complete(task)
    record = router.records[-1]
    generation_manifest: dict[str, Any] = {
        "schema_version": 1,
        "generator": "claude-opus-synthetic-scenario-v1",
        "model_id": record.model_id,
        "prompt_version": task.prompt_version,
        "output_schema_version": task.schema_version,
        "input_hash": task.input_hash,
        "output_hash": _digest(response),
        "estimated_cost_usd": record.estimated_cost_usd,
        "cache_hit": record.cache_hit,
        "fallback_used": record.fallback_used,
        "assumption": "synthetic draft values; not empirical population or event probabilities",
        "request_supplied": bool(operator_request.strip()),
        "request_scope": (
            "Only synthetic household/profile fields and supported facility outage/recovery events can be drafted."
            if operator_request.strip()
            else "No natural-language scenario request was supplied."
        ),
    }
    if record.fallback_used:
        generation_manifest["status"] = "rule_fallback"
        return scenario, generation_manifest

    result = json.loads(response)
    profiles_by_index = _validate_profiles(result["profiles"], scenario, ordered_profiles)
    generated_events = _validated_events(result["events"], scenario, ordered_facilities)
    document = json.loads(scenario.canonical_json())
    groups: dict[int, str] = {}
    for index, output in profiles_by_index.items():
        family_index = output["family_index"]
        group_id = groups.setdefault(family_index, f"G{family_index + 1:03d}")
        profile = document["profiles"][index]
        profile["group_id"] = group_id
        profile["initial_zone_id"] = ordered_zones[output["zone_index"]].id
        profile["mobility"] = output["mobility"]
        profile["support_need"] = output["support_need"]
        profile["stress_sensitivity"] = output["stress_sensitivity"]
        profile["initial_needs"] = output["initial_needs"]
        profile["provenance"] = {
            "source_kind": "llm",
            "generator": "claude-opus-synthetic-scenario-v1",
            "seed": scenario.seed,
        }
    existing_events = [event.model_dump(mode="json") for event in scenario.scheduled_events]
    existing_ids = {event["id"] for event in existing_events}
    if any(event["id"] in existing_ids for event in generated_events):
        raise ValueError("generated event ID collides with an existing Scenario event")
    next_sequence = max((event["sequence"] for event in existing_events), default=-1) + 1
    for offset, event in enumerate(generated_events):
        event["sequence"] = next_sequence + offset
    document["scheduled_events"] = sorted(
        [*existing_events, *generated_events],
        key=lambda event: (event["due_minute"], event.get("priority", 0), event["sequence"]),
    )
    generation_manifest["status"] = "generated"
    generation_manifest["scenario_source_hash"] = _digest(scenario.canonical_json())
    generated = ScenarioSpec.model_validate(document)
    generation_manifest["generated_scenario_hash"] = _digest(generated.canonical_json())
    provenance = dict(generated.provenance)
    provenance["llm_scenario_generation"] = _stable_provenance(generation_manifest)
    generated = generated.model_copy(update={"provenance": provenance})
    # Validate the final ScenarioSpec after its generation manifest has been added.
    generated = ScenarioSpec.model_validate(generated.model_dump(mode="python"))
    return generated, generation_manifest


def _validate_profiles(
    outputs: list[dict[str, Any]],
    scenario: ScenarioSpec,
    profiles: list[Any],
) -> dict[int, dict[str, Any]]:
    count = len(profiles)
    indexed: dict[int, dict[str, Any]] = {}
    for output in outputs:
        profile_index = output["profile_index"]
        if profile_index >= count or profile_index in indexed:
            raise ValueError("scenario draft must include each profile slot exactly once")
        indexed[profile_index] = output
    if set(indexed) != set(range(count)):
        raise ValueError("scenario draft must include each profile slot exactly once")

    family_zones: dict[int, int] = {}
    family_sizes: Counter[int] = Counter()
    zone_sizes: Counter[int] = Counter()
    for profile_index, output in indexed.items():
        family_index = output["family_index"]
        zone_index = output["zone_index"]
        if zone_index >= len(scenario.zones):
            raise ValueError("scenario draft references an unknown zone slot")
        previous_zone = family_zones.setdefault(family_index, zone_index)
        if previous_zone != zone_index:
            raise ValueError("members of one synthetic family must share an initial zone")
        family_sizes[family_index] += 1
        zone_sizes[zone_index] += 1
    if any(size > 4 for size in family_sizes.values()):
        raise ValueError("synthetic family groups cannot exceed four profiles")
    for zone_index, size in zone_sizes.items():
        capacity = sorted(scenario.zones, key=lambda zone: zone.id)[zone_index].capacity
        if capacity is not None and size > capacity:
            raise ValueError(f"scenario draft exceeds capacity of zone slot {zone_index}")
    return indexed


def _validated_events(
    outputs: list[dict[str, Any]],
    scenario: ScenarioSpec,
    facilities: list[Any],
) -> list[dict[str, Any]]:
    if outputs and not facilities:
        raise ValueError("scenario draft cannot schedule facility events without facilities")
    projected = [
        facility.capacity if facility.working_units is None else facility.working_units
        for facility in facilities
    ]
    generated: list[dict[str, Any]] = []
    ordered_outputs = sorted(enumerate(outputs), key=lambda pair: (pair[1]["due_minute"], pair[0]))
    for sequence, (_, output) in enumerate(ordered_outputs):
        facility_index = output["facility_index"]
        if facility_index >= len(facilities):
            raise ValueError("scenario draft references an unknown facility slot")
        facility = facilities[facility_index]
        units = output["units"]
        if output["event_type"] == "facility_outage":
            if units > projected[facility_index]:
                raise ValueError("generated facility outage exceeds available working units")
            projected[facility_index] -= units
            payload = {"facility_id": facility.id, "units_out": units}
        else:
            if projected[facility_index] + units > facility.capacity:
                raise ValueError("generated facility recovery exceeds installed capacity")
            projected[facility_index] += units
            payload = {"facility_id": facility.id, "units_restored": units}
        generated.append(
            {
                "id": f"opus-event-{sequence + 1:03d}",
                "kind": "exogenous",
                "due_minute": output["due_minute"],
                "priority": 0,
                "sequence": sequence,
                "event_type": output["event_type"],
                "payload": payload,
                "source_id": "opus-scenario-generation-v1",
                "causation_id": None,
                "provenance": {
                    "source_kind": "assumption",
                    "generator": "claude-opus-synthetic-scenario-v1",
                    "modelled_probability": False,
                },
            }
        )
    return generated


def _fallback_output(scenario: ScenarioSpec, profiles: list[Any], zones: list[Any]) -> str:
    zone_indexes = {zone.id: index for index, zone in enumerate(zones)}
    family_names = sorted(
        {profile.group_id for profile in profiles if profile.group_id is not None}
    )
    family_indexes = {name: index for index, name in enumerate(family_names)}
    result = {
        "profiles": [
            {
                "profile_index": index,
                "family_index": family_indexes.get(profile.group_id, index),
                "zone_index": zone_indexes[profile.initial_zone_id],
                "mobility": profile.mobility.value,
                "support_need": profile.support_need,
                "stress_sensitivity": profile.stress_sensitivity,
                "initial_needs": profile.initial_needs.model_dump(mode="json"),
            }
            for index, profile in enumerate(profiles)
        ],
        "events": [],
    }
    del scenario
    return json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stable_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    stable_keys = (
        "schema_version",
        "generator",
        "model_id",
        "prompt_version",
        "output_schema_version",
        "input_hash",
        "output_hash",
        "status",
        "scenario_source_hash",
        "generated_scenario_hash",
        "assumption",
    )
    return {key: manifest[key] for key in stable_keys if key in manifest}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
