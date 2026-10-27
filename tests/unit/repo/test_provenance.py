"""Check source metadata and keep every baseline value labeled as evidence or hypothesis.

That a Run writes the source IDs into its manifest and decision trace is checked in
tests/unit/experiments/test_metrics.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.core.world import WorldState
from sheltersim.domain.models import InformationSpreadConfig
from sheltersim.experiments.profiles import ProfileTemplate
from sheltersim.experiments.scenario import PsychologyModelConfig, ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.report import scenario_assumption_ids
from sheltersim.llm.runtime import SocialRuntimeLimits, SupportPriorityLimits
from sheltersim.policies.behavior import UtilityBehaviorPolicy
from sheltersim.policies.social import SocialPropagationParameters

ROOT = Path(__file__).parents[3]
MANIFEST_PATH = ROOT / "fixtures" / "source-manifest.json"
REGRESSION_CONTRACT_PATH = ROOT / "fixtures" / "regression-contract.json"
BASELINE_PATH = ROOT / "fixtures" / "scenarios" / "baseline_100_7d.json"
FAILURE_PATH = ROOT / "fixtures" / "scenarios" / "failure_day3.json"


def test_source_manifest_is_versioned_and_points_to_primary_sources() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    allowed_types = set(manifest["source_type_values"])
    sources = manifest["sources"]
    assumptions = manifest["assumptions"]

    assert manifest["schema_version"] == 1
    assert allowed_types == {"measured", "literature", "interview", "assumption"}
    assert len({source["id"] for source in sources}) == len(sources)
    assert len({assumption["id"] for assumption in assumptions}) == len(assumptions)
    assert all(
        source["source_type"] in {"literature", "interview", "measured"} for source in sources
    )
    assert all(source["url"].startswith("https://") for source in sources)
    assert all(source.get("edition") and source.get("published_on") for source in sources)
    assert all(assumption["source_type"] == "assumption" for assumption in assumptions)
    assert all(
        assumption.get("target") and assumption.get("calibration_status")
        for assumption in assumptions
    )
    assert all(assumption.get("notes") for assumption in assumptions)

    source_ids = {source["id"] for source in sources}
    assert all(
        set(assumption.get("context_sources", [])) <= source_ids for assumption in assumptions
    )
    assumption_ids = {assumption["id"] for assumption in assumptions}
    assert {
        "ASM-SYNTHETIC-PROFILES-V1",
        "ASM-SIMPLE-PSYCHOLOGY-RATES-V1",
        "ASM-H04-CAUSAL-STRESS-V1",
        "ASM-UTILITY-BEHAVIOR-V1",
        "ASM-SLEEP-AWARE-BEHAVIOR-V2",
        "ASM-PRIORITY-RULE-BEHAVIOR-V1",
        "ASM-TOILET-SERVICE-DURATION-V1",
        "ASM-DAY3-TOILET-OUTAGE-V1",
        "ASM-H00-REGRESSION-OUTAGE-V1",
        "ASM-SPACE-AND-STAFF-CAPACITY-V1",
        "ASM-STAFF-SUPPORT-LIFECYCLE-V1",
        "ASM-SOCIAL-PROPAGATION-V1",
        "ASM-MEAN-REVERTING-LIFE-STATE-V1",
        "ASM-INFORMATION-SPREAD-V1",
        "ASM-A07-SENSITIVITY-CASES-V1",
        "ASM-ZONE-ROUTING-V1",
        "ASM-A05-ROUTE-EDIT-V1",
        "ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1",
        "ASM-SUPPORT-PRIORITY-LIMITS-V1",
        "ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1",
        "ASM-SYNTHETIC-ZONE-DISPLAY-V1",
        "ASM-H02-RESOURCE-SERVICE-V1",
        "ASM-H02-TEST-INITIAL-STOCK-V1",
        "ASM-H02-QUEUE-STALLS-V1",
        "ASM-H03-OPERATIONAL-EVENTS-V1",
        "ASM-H03-TEST-STOCK-V1",
        "ASM-H03-TEST-EVENT-TIMING-V1",
        "ASM-H05-OPERATIONAL-CAPACITY-V1",
        "ASM-H05-OPERATIONAL-STOCK-V1",
        "ASM-H05-OPERATIONAL-SCHEDULE-V1",
        "ASM-H05-INTERVENTION-V1",
        "ASM-CORE-MODEL-SUITE-V1",
        "ASM-CORE-MODEL-SUITE-V2",
        "ASM-SLEEP-DEBT-BALANCE-V1",
    } <= assumption_ids
    route_edit = next(
        assumption for assumption in assumptions if assumption["id"] == "ASM-A05-ROUTE-EDIT-V1"
    )
    assert route_edit["model_version"] == "a05-route-edit-v1"
    assert route_edit["values"]["maximum_capacity"] == 100
    assert "not measured" in route_edit["notes"]
    operational = next(
        assumption
        for assumption in assumptions
        if assumption["id"] == "ASM-H03-OPERATIONAL-EVENTS-V1"
    )
    assert operational["values"]["water_outage_service_stall_reason"] == "water_outage"
    assert operational["values"]["water_outage"].endswith("active services finish")
    assert "not measured" in operational["notes"]
    test_stock = next(
        assumption for assumption in assumptions if assumption["id"] == "ASM-H03-TEST-STOCK-V1"
    )
    assert test_stock["values"]["water_ml"] == 1000
    assert test_stock["values"]["food_meal_units"] == 5


def test_psychology_behavior_and_profile_hypotheses_match_the_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assumptions = {item["id"]: item for item in manifest["assumptions"]}
    legacy_psychology = assumptions["ASM-SIMPLE-PSYCHOLOGY-RATES-V1"]
    psychology = assumptions["ASM-H04-CAUSAL-STRESS-V1"]
    behavior = assumptions["ASM-UTILITY-BEHAVIOR-V1"]
    priority_behavior = assumptions["ASM-PRIORITY-RULE-BEHAVIOR-V1"]
    profiles = assumptions["ASM-SYNTHETIC-PROFILES-V1"]

    expected_psychology_values = PsychologyModelConfig().coefficient_values()
    assert psychology["values"] == expected_psychology_values
    assert psychology["model_version"] == "causal-stress-v1"
    assert legacy_psychology["model_version"] == "simple-psychology-v1"
    policy = UtilityBehaviorPolicy()
    assert behavior["model_version"] == policy.policy_version
    assert behavior["values"]["temperature"] == policy.temperature
    assert behavior["values"]["fixture_decision_interval_minutes"] == 60
    assert behavior["values"]["eligibility_triggers"]["request_toilet"] == (
        "toilet_urgency >= 0.25"
    )
    assert behavior["values"]["utility_weights"]["seek_help"] == {
        "intercept": 0.15,
        "distress": 0.65,
        "support_need": 0.25,
        "trust": 0.10,
        "urgent_priority_threshold": 0.80,
        "priority_bonus": 0.5,
    }
    assert priority_behavior["model_version"] == "priority-rule-v1"
    assert priority_behavior["values"]["candidate_eligibility_and_utility"] == (
        "shared with UtilityBehaviorPolicy"
    )
    assert priority_behavior["calibration_status"] == "uncalibrated deterministic rule alternative"

    baseline = ScenarioSpec.load(BASELINE_PATH)
    generation = baseline.provenance["profile_generation"]
    template = ProfileTemplate.model_validate(generation["template"])
    values = profiles["values"]
    assert generation["generator"] == "counter-rng-profile-generator-v1"
    assert template.template_id == profiles["model_version"]
    assert values["generator"] == generation["generator"]
    assert values["rng_algorithm"] == generation["rng_algorithm"]
    assert values["template"] == template.model_dump(mode="json")
    assert values["template"]["group_size"] == template.group_size
    assert values["template"]["initial_zone_ids"] == list(template.initial_zone_ids)
    assert values["template"]["independent_weight"] == template.independent_weight
    assert values["template"]["assisted_weight"] == template.assisted_weight
    assert values["template"]["wheelchair_weight"] == template.wheelchair_weight
    assert all(profile.provenance.source_kind == "synthetic" for profile in baseline.profiles)


def test_optional_and_runtime_assumptions_match_the_implemented_defaults() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assumptions = {item["id"]: item for item in manifest["assumptions"]}
    social = assumptions["ASM-SOCIAL-PROPAGATION-V1"]["values"]
    information = assumptions["ASM-INFORMATION-SPREAD-V1"]["values"]
    active_social_defaults = asdict(SocialPropagationParameters())
    active_information_defaults = InformationSpreadConfig().to_dict()
    assert social["enabled_by_default"] is False
    assert social["coefficients"] == {
        key: value for key, value in active_social_defaults.items() if key != "source_id"
    }
    assert information["enabled_by_default"] is False
    expected_information = {
        key: value
        for key, value in active_information_defaults.items()
        if key not in {"enabled", "schema_version", "policy_version", "source_type", "source_id"}
    }
    assert {
        key: value
        for key, value in information.items()
        if key != "enabled_by_default" and key != "maximum_messages_per_run"
    } == expected_information

    staff = assumptions["ASM-STAFF-SUPPORT-LIFECYCLE-V1"]["values"]
    assert staff["support_task_minutes"] == 10
    assert staff["unresolved_after_minutes"] == 90
    assert staff["support_cooldown_minutes"] == 30
    assert staff["support_fairness_minutes"] == 30
    assert assumptions["ASM-STAFF-SUPPORT-LIFECYCLE-V1"]["model_version"] == (
        "staff-support-fifo-aging-v2"
    )

    runtime_limits = assumptions["ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1"]["values"]
    assert runtime_limits == asdict(SocialRuntimeLimits())
    priority_limits = assumptions["ASM-SUPPORT-PRIORITY-LIMITS-V1"]
    assert priority_limits["values"] == asdict(SupportPriorityLimits())
    assert priority_limits["model_version"] == "support-priority-v2"

    zone_layout = assumptions["ASM-SYNTHETIC-ZONE-DISPLAY-V1"]["values"]
    assert zone_layout["grid_columns"] == 4
    assert zone_layout["x_step"] == 140
    assert zone_layout["y_step"] == 110


def test_packaged_failure_fixture_stays_in_sync_with_source_fixture() -> None:
    source = FAILURE_PATH.read_bytes()
    packaged = ROOT / "src" / "sheltersim" / "web" / "examples" / "failure_day3.json"
    assert packaged.read_bytes() == source


def test_scenario_models_and_failure_events_are_explicitly_hypothetical() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assumptions = {item["id"]: item for item in manifest["assumptions"]}
    toilet_assumption = assumptions["ASM-TOILET-SERVICE-DURATION-V1"]
    outage_assumption = assumptions["ASM-DAY3-TOILET-OUTAGE-V1"]
    baseline = ScenarioSpec.load(BASELINE_PATH)
    failure = ScenarioSpec.load(FAILURE_PATH)

    assert baseline.model_configs["toilet_service"]["source_type"] == "assumption"
    for config_name, assumption_id in (
        ("behavior", "ASM-UTILITY-BEHAVIOR-V1"),
        ("toilet_service", "ASM-TOILET-SERVICE-DURATION-V1"),
        ("staff", "ASM-STAFF-SUPPORT-LIFECYCLE-V1"),
        ("social_propagation", "ASM-SOCIAL-PROPAGATION-V1"),
    ):
        assert baseline.model_configs[config_name]["source_type"] == "assumption"
        assert baseline.model_configs[config_name]["source_id"] == assumption_id
        assert assumption_id in baseline.provenance["model_assumption_ids"]
    service_config = baseline.model_configs["toilet_service"]
    assert service_config["minimum_minutes"] == toilet_assumption["values"]["minimum_minutes"]
    assert service_config["maximum_minutes"] == toilet_assumption["values"]["maximum_minutes"]
    assert all(
        config.get("source_type") == "assumption" for config in baseline.model_configs.values()
    )

    outage = outage_assumption["values"]
    assert len(failure.scheduled_events) == len(outage["unavailable_facilities"])
    for event in failure.scheduled_events:
        assert event.provenance["source_type"] == "assumption"
        assert event.provenance["source_id"] == "ASM-DAY3-TOILET-OUTAGE-V1"
        assert event.due_minute == outage["due_minute"]
        assert event.payload["facility_id"] in outage["unavailable_facilities"]
        assert event.payload["units_out"] == outage["units_out_per_facility"]
    assert "ASM-DAY3-TOILET-OUTAGE-V1" in scenario_assumption_ids(failure)


def test_h00_baseline_contract_freezes_seed_models_and_source_categories(
    tmp_path: Path,
) -> None:
    contract = json.loads(REGRESSION_CONTRACT_PATH.read_text(encoding="utf-8"))["baseline_100_7d"]
    baseline = ScenarioSpec.load(BASELINE_PATH)
    scenario_hash = hashlib.sha256(baseline.canonical_json().encode("utf-8")).hexdigest()
    model_configs_hash = hashlib.sha256(
        json.dumps(
            baseline.model_configs,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    assert baseline.id == contract["scenario_id"]
    assert baseline.schema_version == contract["scenario_schema_version"] == 3
    assert baseline.seed == contract["seed"]
    assert baseline.duration_minutes == contract["duration_minutes"]
    assert len(baseline.profiles) == contract["profile_count"] == 100
    assert scenario_hash == contract["scenario_sha256"]
    assert model_configs_hash == contract["model_configs_sha256"]
    assert list(baseline.provenance["model_assumption_ids"]) == contract["model_assumption_ids"]
    assert {
        "profiles": sorted({profile.provenance.source_kind for profile in baseline.profiles}),
        "model_configs": sorted(
            {config.get("source_type", "unspecified") for config in baseline.model_configs.values()}
        ),
        "planned_events": sorted(
            {
                event.provenance.get("source_type", "unspecified")
                for event in baseline.scheduled_events
            }
        ),
    } == contract["source_categories"]
    assert [event.id for event in baseline.scheduled_events] == contract["planned_event_ids"]

    initial_world = WorldState.from_scenario(baseline)
    checkpoint = Checkpoint.capture(initial_world)
    assert checkpoint.schema_version == contract["checkpoint_schema_version"] == 1
    assert checkpoint.world_state_schema_version == contract["world_state_schema_version"] == 5

    writer = RunArtifactWriter(baseline, tmp_path / "initial-artifacts", trace_level=TraceLevel.OFF)
    writer.write_initial(initial_world, execution_mode="offline")
    manifest = json.loads(
        (tmp_path / "initial-artifacts" / "manifest.json").read_text(encoding="utf-8")
    )
    policy_versions = {
        name: assumption.get("policy_version", assumption.get("generator"))
        for name, assumption in manifest["model_assumptions"].items()
        if "policy_version" in assumption or "generator" in assumption
    }
    assert policy_versions == contract["model_policy_versions"]


def test_h00_small_replay_event_has_its_own_assumption_record() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assumption = {item["id"]: item for item in manifest["assumptions"]}[
        "ASM-H00-REGRESSION-OUTAGE-V1"
    ]
    scenario = ScenarioSpec.load(ROOT / "fixtures" / "scenarios" / "h00_small_replay.json")
    event = scenario.scheduled_events[0]

    assert assumption["target"] == "fixtures/scenarios/h00_small_replay.json:scheduled_events"
    assert assumption["values"] == {
        "due_minute": event.due_minute,
        "facility_id": event.payload["facility_id"],
        "units_out": event.payload["units_out"],
    }
    assert assumption["calibration_status"] == "synthetic deterministic regression fixture"
    assert event.provenance["source_id"] == assumption["id"]
    assert event.provenance["source_type"] == assumption["source_type"]
