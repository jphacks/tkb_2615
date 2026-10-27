from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

SMALL_REPLAY = Path(__file__).parents[1] / "fixtures" / "scenarios" / "h00_small_replay.json"
SCENARIO_100_AGENTS = Path(__file__).parents[1] / "fixtures" / "scenario_100_agents.json"
BASELINE_100_7D = Path(__file__).parents[1] / "fixtures" / "scenarios" / "baseline_100_7d.json"

# The structured form the demo-v2 Scenario screen posts (scenarioRequest in
# demo-v2.js): only the fields an operator types, each marked as user input.
# Small enough that running the whole Scenario takes seconds.
GENERATED_SCENARIO_REQUEST: dict[str, Any] = {
    "municipality": "合成市",
    "disaster": "地震",
    "evacuees": 20,
    "duration_days": 0.25,
    "field_origins": {
        "municipality": "user",
        "disaster": "user",
        "evacuees": "user",
        "duration_days": "user",
    },
}


def _isolate(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("SHELTERSIM_PROJECTS_DIR", str(root / "projects"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("SHELTERSIM_LLM_CACHE_PATH", str(root / "llm-tape.json"))
    # macOS production storage deliberately ignores XDG_STATE_HOME. Tests must
    # still isolate the shared ledger and honour per-test XDG overrides.
    from sheltersim.storage import usage
    from sheltersim.storage.projects import ProjectStore

    monkeypatch.setattr(
        usage,
        "task_campaign_store",
        lambda: ProjectStore(Path(os.environ["XDG_STATE_HOME"]) / "sheltersim"),
    )


@pytest.fixture(autouse=True)
def _isolated_local_state(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every test away from the developer's Projects, usage ledgers and LLM cache.

    Without this, a CLI test wrote a usage account under the repository's runs/.projects,
    and parallel workers (pytest -n) would share those default locations. Tests that set
    these variables themselves still override them.

    Paid live_api tests keep the real locations on purpose: their spend must count
    against this PC's campaign ledger.
    """
    if request.node.get_closest_marker("live_api") is not None:
        return
    _isolate(monkeypatch, tmp_path_factory.mktemp("local-state"))


def _generated_request(**fields: Any) -> dict[str, Any]:
    """GENERATED_SCENARIO_REQUEST with some fields changed and marked as user input."""
    request = copy.deepcopy(GENERATED_SCENARIO_REQUEST)
    request.update(copy.deepcopy(fields))
    request["field_origins"] = {**request["field_origins"], **{name: "user" for name in fields}}
    return request


@contextmanager
def _ui_project_api(monkeypatch: pytest.MonkeyPatch, root: Path) -> Iterator[Any]:
    """The Project screen's API over a fresh store, with an empty regional research.

    The real research calls public web services, so it is replaced by an empty
    result; everything else goes through the same code as the UI.
    """
    from sheltersim.region.models import RegionResearchResult
    from sheltersim.storage.projects import ProjectStore
    from sheltersim.web.projects import ProjectAPI

    _isolate(monkeypatch, root)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = ProjectStore(root / "store")
    store.create_project("project")
    api = ProjectAPI(store)
    api.workspace.research.research = lambda query, **_kwargs: RegionResearchResult(
        request=query, researched_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    try:
        yield api
    finally:
        api.close()


def _ui_preview(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run the Project screen's Scenario preview for one structured form."""
    with pytest.MonkeyPatch.context() as monkeypatch, _ui_project_api(monkeypatch, root) as api:
        return api.workspace.preview("project", {"text": "", "request": request})


@pytest.fixture(scope="session")
def generated_previews(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., dict[str, Any]]:
    """Previews of GENERATED_SCENARIO_REQUEST with some form fields changed.

    Each changed field is marked as user input, as the demo-v2 form does. Every
    distinct set of fields is built once per worker and shared, so do not change
    the returned preview.
    """
    cache: dict[str, dict[str, Any]] = {}

    def preview(**fields: Any) -> dict[str, Any]:
        key = json.dumps(fields, sort_keys=True, ensure_ascii=False)
        if key not in cache:
            cache[key] = _ui_preview(
                tmp_path_factory.mktemp("generated-scenario"), _generated_request(**fields)
            )
        return cache[key]

    return preview


@pytest.fixture(scope="session")
def generated_scenario_preview(generated_previews: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """The Scenario preview the Project screen gets for GENERATED_SCENARIO_REQUEST.

    It goes through the same ProjectAPI preview as the UI, so the Scenario has a
    floor plan (facility_geometry) and facility awareness (agent_dynamics) like
    every Scenario an operator creates. Only the regional research is replaced by
    an empty result, because the real one calls public web services.
    Built once per worker; use generated_scenario for a Scenario to change.
    """
    return generated_previews()


@pytest.fixture
def generated_scenario(generated_scenario_preview: dict[str, Any]):
    """A fresh ScenarioSpec of the shared generated preview, safe to change in one test."""
    from sheltersim.experiments.scenario import ScenarioSpec

    return ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))


@pytest.fixture
def generated_scenario_with(generated_previews: Callable[..., dict[str, Any]]):
    """Build a fresh ScenarioSpec from the shared form with some fields changed.

    For example generated_scenario_with(duration_days=4) is what an operator gets
    after typing 4 into the duration field.
    """
    from sheltersim.experiments.scenario import ScenarioSpec

    def build(**fields: Any):
        return ScenarioSpec.model_validate(copy.deepcopy(generated_previews(**fields)["scenario"]))

    return build


@pytest.fixture
def count_validations(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[int]]:
    """Record the id of every object a pydantic model validates from now on.

    For tests of parsed-config caches; monkeypatch.undo() stops the counting.
    """

    def count(model: Any) -> list[int]:
        calls: list[int] = []
        original = model.model_validate.__func__

        def validate(cls, value, *args, **kwargs):
            calls.append(id(value))
            return original(cls, value, *args, **kwargs)

        monkeypatch.setattr(model, "model_validate", classmethod(validate))
        return calls

    return count


@pytest.fixture
def ui_project_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The Project screen's API with an empty project named "project", open for one test.

    Use it when a test must call further routes on the preview it creates, which
    the cached generated_previews cannot do. Build the form with generated_request.
    """
    with _ui_project_api(monkeypatch, tmp_path) as api:
        yield api


@pytest.fixture
def project_store_at() -> Callable[[Path], Any]:
    """Open a ProjectStore at a folder, with an empty project named "project"."""

    def open_store(root: Path) -> Any:
        from sheltersim.storage.projects import ProjectStore

        store = ProjectStore(root)
        store.create_project("project")
        return store

    return open_store


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **_kwargs: object) -> Any:
        from sheltersim.llm.contracts import ProviderResponse

        self.calls += 1
        return ProviderResponse(
            text='{"ok":true}', model_id="fake", input_tokens=5, output_tokens=3, elapsed_ms=0
        )


@pytest.fixture
def counting_provider() -> _CountingProvider:
    """A fake LLM provider that answers {"ok":true} and counts its calls."""
    return _CountingProvider()


@pytest.fixture
def generated_request() -> Callable[..., dict[str, Any]]:
    """The structured form of GENERATED_SCENARIO_REQUEST with some fields changed."""
    return _generated_request


@pytest.fixture
def generated_scenario_with_split_queues(generated_scenario):
    """The generated Scenario with two service points, each its own queue, at two facilities.

    The generator gives every facility one service point. toilet_01 and
    water_station_main get a left and a right point here instead, one metre either
    side of the facility, so each serves two people at once, and each point needs a
    worker with the right skill; a toilet worker is added to the toilet area for that.
    """
    from sheltersim.experiments.scenario import ScenarioSpec

    document = generated_scenario.model_dump(mode="json")
    geometry = document["model_configs"]["facility_geometry"]
    zones = {facility["id"]: facility["zone_id"] for facility in document["facilities"]}
    facilities = {facility["id"]: facility for facility in document["facilities"]}
    for facility_id, skill in (
        ("toilet_01", "toilet_service"),
        ("water_station_main", "water_service"),
    ):
        facilities[facility_id]["capacity"] = facilities[facility_id]["working_units"] = 2
        point = geometry["facility_points"][facility_id]
        geometry["service_points"] = [
            row for row in geometry["service_points"] if row["facility_id"] != facility_id
        ]
        for side, offset in (("left", -1.0), ("right", 1.0)):
            geometry["service_points"].append(
                {
                    "schema_version": 1,
                    "id": f"{facility_id}-{side}",
                    "facility_id": facility_id,
                    "point_m": {"x": point["x"], "y": point["y"] + offset},
                    "service_capacity": 1,
                    "queue_id": f"{facility_id}-{side}-fifo",
                    "access": {"accessibility": "accessible", "required_staff_skills": [skill]},
                }
            )
    document["staff"].append(
        {"id": "toilet_staff_01", "zone_id": zones["toilet_01"], "skills": ["toilet_service"]}
    )
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def place_three_at_facility() -> Callable[[Any, str], None]:
    """Stand A001-A003 in a facility's area and keep everyone from deciding anew.

    A request to a facility's queue needs its person to have arrived there.
    """

    def place(world: Any, facility_id: str) -> None:
        from sheltersim.domain.geometry_overlay import effective_geometry
        from sheltersim.domain.movement import MOVEMENT_STATE_KEY

        zone_id = world.facilities[facility_id].zone_id
        zone = next(row for row in effective_geometry(world).zones if row.zone_id == zone_id)
        centre = {
            "x": sum(point.x for point in zone.polygon) / len(zone.polygon),
            "y": sum(point.y for point in zone.polygon) / len(zone.polygon),
        }
        for agent in world.agents.values():
            agent.next_decision_at = 100
        for agent_id in ("A001", "A002", "A003"):
            world.agents[agent_id].zone_id = zone_id
            world.model_state[MOVEMENT_STATE_KEY]["positions"][agent_id].update(
                zone_id=zone_id,
                point=dict(centre),
                edge_id=None,
                progress=0.0,
                position_kind="physical",
            )

    return place


def _scenario_with_stock(
    *,
    water_ml: int = 0,
    food_meal_units: int = 0,
    source_ids: list[str] | None = None,
    scheduled_events: list[dict[str, object]] | None = None,
    duration_minutes: int = 5,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = scheduled_events or []
    for scheduled in document["scheduled_events"]:
        if scheduled.get("event_type", "").startswith("resource_"):
            scheduled["payload"] = {
                "warehouse_id": "warehouse-main",
                **scheduled.get("payload", {}),
            }
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-main",
        "zone_id": "sleeping",
        "water_ml": water_ml,
        "food_meal_units": food_meal_units,
        "source_ids": (
            source_ids
            if source_ids is not None
            else (["ASM-H01-TEST-INITIAL-STOCK-V1"] if water_ml or food_meal_units else [])
        ),
    }
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def scenario_with_stock() -> Callable[..., Any]:
    """Build h00_small_replay with an initial stock in warehouse-main and resource events.

    Resource events get warehouse-main in their payload, and a stock other than zero
    gets a test source ID unless source_ids is given.
    """
    return _scenario_with_stock


def _resource_service_scenario(
    *,
    duration: int = 12,
    water_ml: int = 1000,
    food_units: int = 4,
    water_capacity: int = 1,
    meal_capacity: int = 1,
    water_working: int | None = None,
    water_staff_count: int = 1,
    meal_staff: bool = True,
    service_minutes: int = 2,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    document["facilities"].extend(
        [
            {
                "id": "water_station_1",
                "kind": "water_station",
                "zone_id": "toilet",
                "capacity": water_capacity,
                "working_units": water_capacity if water_working is None else water_working,
            },
            {
                "id": "meal_distribution_1",
                "kind": "meal_distribution",
                "zone_id": "toilet",
                "capacity": meal_capacity,
                "working_units": meal_capacity,
            },
        ]
    )
    for index in range(water_staff_count):
        document["staff"].append(
            {"id": f"S-WATER-{index + 1}", "zone_id": "toilet", "skills": ["water_service"]}
        )
    if meal_staff:
        document["staff"].append({"id": "S-MEAL", "zone_id": "toilet", "skills": ["meal_service"]})
    document["initial_stock"] = {
        "schema_version": 1,
        "warehouse_id": "warehouse-h02",
        "zone_id": "toilet",
        "water_ml": water_ml,
        "food_meal_units": food_units,
        "source_ids": ["ASM-H02-TEST-INITIAL-STOCK-V1"] if water_ml or food_units else [],
    }
    document["model_configs"]["resource_service"] = {
        "schema_version": 1,
        "policy_version": "resource-service-v1",
        "source_id": "ASM-H02-RESOURCE-SERVICE-V1",
        "source_type": "assumption",
        "service_minutes": service_minutes,
        "water_ml_per_service": 250,
        "food_meal_units_per_service": 1,
        "hydration_growth_per_minute": 0.02,
        "hydration_action_threshold": 0.25,
        "hunger_action_threshold": 0.70,
        "hydration_relief": 0.35,
        "hunger_relief": 0.50,
    }
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def resource_service_scenario() -> Callable[..., Any]:
    """Build h00_small_replay with a staffed water station and meal point in the toilet zone.

    The Scenario opts into resource_service and starts with water and meals in
    warehouse-h02; the arguments change the stock, capacities, staff and duration.
    """
    return _resource_service_scenario


def _single_agent_awareness_scenario(initial_known_facility_ids: list[str]) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "facility-awareness-toilet-integration"
    document["duration_minutes"] = 180
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:1]
    profile = document["profiles"][0]
    profile["initial_zone_id"] = "sleeping"
    profile["initial_needs"] = {
        "toilet_urgency": 0.65,
        "hunger": 0.45,
        "fatigue": 0.25,
    }
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.01,
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-life-state-v1",
    }
    document["model_configs"]["facility_awareness"] = {
        "schema_version": 1,
        "initial_known_facility_ids": initial_known_facility_ids,
        "discover_co_located_facilities_on_arrival": True,
        "source_type": "assumption",
        "source_id": "ASM-FACILITY-ENTRY-ORIENTATION-V1",
    }
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def single_agent_awareness_scenario() -> Callable[[list[str]], Any]:
    """Build a 180-minute scenario_100_agents with only A001, in the sleeping area.

    A001 needs the toilet, the Scenario opts into facility_awareness, and A001
    starts knowing only the facilities given; it discovers the others it reaches.
    """
    return _single_agent_awareness_scenario


def _add_toilets_program(count: int = 2, *, start_minute: int) -> Any:
    from sheltersim.events.effects import EffectProgram, SupportMode, TargetSelector, TimedAction

    return EffectProgram(
        program_id="program-installed-toilets",
        narrative="Add temporary toilets",
        source_text="仮設トイレを追加",
        simulation_start_minute=start_minute,
        effects=[
            TimedAction(
                effect_id="add-toilets",
                op="add_toilet",
                target_selector=TargetSelector(kind="zone", target_id="toilet"),
                start_minute=start_minute,
                parameters={"count": count},
                units="toilet_stall",
                source_id="TEST-INSTALLED-FACILITY",
                assumption_ids=[],
                policy_dependencies=["facility_capacity"],
                reversible=False,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


@pytest.fixture
def add_toilets_program() -> Callable[..., Any]:
    """Build an EffectProgram that adds count toilets (2 by default) to the toilet zone."""
    return _add_toilets_program


def _research_psychology_scenario(*, duration: int = 20, social: bool = False) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "research-model-test"
    document["duration_minutes"] = duration
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    for profile in document["profiles"]:
        profile["initial_needs"] = {
            "toilet_urgency": 0.0,
            "hunger": 0.0,
            "fatigue": 0.0,
        }
    document["model_configs"]["psychology"] = {
        "natural_recovery_rate_per_minute": 0.02,
        "chronic_retention": 0.9,
        "chronic_acute_weight": 0.08,
        "chronic_high_distress_weight": 0.02,
        "high_distress_threshold": 0.7,
    }
    document["model_configs"]["policy_registry"] = {
        "schema_version": 1,
        "psychology": "mean-reverting-distress-v1",
    }
    if social:
        document["model_configs"]["social_propagation"] = {
            "enabled": True,
            "max_contacts_per_agent": 1,
            "minimum_contact_minutes": 1,
            "stress_spread_per_minute": 0.2,
            "natural_recovery_per_minute": 0.0,
            "cooldown_minutes": 5,
            "max_pair_stress_increase": 0.25,
            "max_agent_stress_increase_per_minute": 0.05,
            "source_id": "ASM-BOSSE-AFFECT-CONTAGION-V1",
        }
        document["model_configs"]["policy_registry"]["social"] = (
            "expressiveness-channel-openness-v1"
        )
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def research_psychology_scenario() -> Callable[..., Any]:
    """Build a two-person scenario_100_agents with mean-reverting distress and no needs.

    With social=True it also enables the research social operator (Bosse et al.
    affect contagion, expressiveness-channel-openness-v1).
    """
    return _research_psychology_scenario


def _stress_delta(agent_id: str, minute: int, stress: float) -> Any:
    from sheltersim.policies.psychology import AgentStateDelta

    return AgentStateDelta(
        agent_id=agent_id,
        minute_index=minute,
        toilet_urgency=0.0,
        hunger=0.0,
        fatigue=0.0,
        stress=stress,
        trust=0.5,
    )


@pytest.fixture
def stress_delta() -> Callable[[str, int, float], Any]:
    """Build a psychology update that sets only stress (needs 0, trust 0.5)."""
    return _stress_delta


def _causal_stress_scenario(
    *,
    agent_count: int = 2,
    duration: int = 20,
    stress_pressure: float = 0.1,
    queued_stress: float = 0.01,
    support_wait_stress: float = 0.02,
    social_enabled: bool = False,
    with_service: bool = True,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    profiles = [
        {
            "schema_version": 2,
            "id": f"A{index:03}",
            "group_id": "G01",
            "initial_zone_id": "main",
            "support_need": 0.8,
            "stress_sensitivity": 1.0,
            "initial_needs": {"toilet_urgency": 0.8, "hunger": 0.9, "fatigue": 0.2},
            "rng_key": f"profile:A{index:03}",
        }
        for index in range(1, agent_count + 1)
    ]
    config = {
        "psychology": {
            "stress_pressure": stress_pressure,
            "queued_stress": queued_stress,
            "support_wait_stress": support_wait_stress,
        },
        "behavior": {"decision_interval_minutes": 5},
        "staff": {"support_task_minutes": 5, "unresolved_after_minutes": 10},
        "social_propagation": {
            "enabled": social_enabled,
            "max_contacts_per_agent": 1,
            "minimum_contact_minutes": 1,
            "stress_spread_per_minute": 0.01,
            "natural_recovery_per_minute": 0.0,
            "cooldown_minutes": 1,
            "max_pair_stress_increase": 0.05,
            "max_agent_stress_increase_per_minute": 0.05,
        },
    }
    document = {
        "schema_version": 3,
        "id": "causal-stress-test",
        "duration_minutes": duration,
        "seed": 23,
        "zones": [{"id": "main", "name": "Main", "capacity": 10}],
        "profiles": profiles,
        "model_configs": config,
    }
    if with_service:
        document["facilities"] = [
            {"id": "toilet-1", "kind": "toilet", "zone_id": "main", "capacity": 1}
        ]
        document["staff"] = [{"id": "staff-1", "zone_id": "main", "skills": ["support"]}]
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def causal_stress_scenario() -> Callable[..., Any]:
    """Build a one-zone Scenario where needy residents queue for one toilet and one worker.

    The psychology coefficients for need pressure, queueing and support waits can be
    changed; with_service=False drops the toilet and the worker.
    """
    return _causal_stress_scenario


def _information_spread_config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "enabled": True,
        "max_contacts_per_agent": 16,
        "minimum_contact_minutes": 1,
        "contact_duration_scale_minutes": 1,
        "base_transmission_probability": 1.0,
        "correction_transmission_multiplier": 1.0,
        "trust_sensitivity": 0.0,
        "family_contact_multiplier": 1.0,
        "adjacent_zone_contact_multiplier": 1.0,
        "query_pressure_per_claim": 0.5,
        "query_pressure_decay_per_minute": 0.0,
        "query_pressure_relief_per_correction": 0.5,
        "query_threshold": 0.5,
        "query_utility_weight": 4.0,
    }
    values.update(overrides)
    return values


@pytest.fixture
def information_spread_config() -> Callable[..., dict[str, object]]:
    """An enabled information_spread config where every contact passes a claim on.

    Keyword arguments replace single coefficients.
    """
    return _information_spread_config


def _information_spread_scenario(
    *,
    duration: int = 6,
    profile_count: int = 2,
    truth_status: str = "unknown",
    correction: bool = True,
    config: dict[str, object] | None = None,
) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["id"] = "information-spread-test"
    document["duration_minutes"] = duration
    document["scheduled_events"] = [
        {
            "id": "rumor-event",
            "kind": "exogenous",
            "due_minute": 0,
            "priority": 0,
            "sequence": 10,
            "event_type": "information_rumor",
            "payload": {
                "message_id": "claim-water-1",
                "truth_status": truth_status,
                "origin_agent_id": "A001",
            },
            "source_id": "scenario-rumor-source",
        }
    ]
    if correction:
        document["scheduled_events"].append(
            {
                "id": "correction-event",
                "kind": "exogenous",
                "due_minute": 3,
                "priority": 0,
                "sequence": 11,
                "event_type": "information_correction",
                "payload": {
                    "message_id": "claim-water-1",
                    "zone_id": "sleeping",
                    "belief": "rejected" if truth_status == "false" else "verified",
                },
                "source_id": "staff-notice-source",
                "causation_id": "rumor-event",
            }
        )
    document["profiles"] = document["profiles"][:profile_count]
    for profile in document["profiles"]:
        profile["support_need"] = 0.0
        profile["initial_needs"] = {
            "toilet_urgency": 0.0,
            "hunger": 0.0,
            "fatigue": 0.0,
        }
    document["model_configs"]["information_spread"] = config or _information_spread_config()
    document["model_configs"].setdefault("behavior", {})["temperature"] = 0.01
    return ScenarioSpec.model_validate(document)


@pytest.fixture
def information_spread_scenario() -> Callable[..., Any]:
    """Build scenario_100_agents with a rumor from A001 at minute 0 and, by default, a
    staff correction in the sleeping area at minute 3.

    Residents have no needs, so information is the only thing that drives inquiries.
    """
    return _information_spread_scenario


def _small_replay_scenario(duration_minutes: int = 30) -> Any:
    from sheltersim.experiments.scenario import ScenarioSpec

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    return ScenarioSpec.from_json(json.dumps(document))


@pytest.fixture
def small_replay_scenario() -> Callable[..., Any]:
    """Build h00_small_replay without its scheduled events, 30 minutes long by default."""
    return _small_replay_scenario


def _route_edit_command(
    *,
    edge_id: str = "corridor_toilet",
    travel_minutes: int = 8,
    max_agents_in_transit: int | None = 1,
) -> dict[str, object]:
    return {
        "kind": "set_zone_edge",
        "edge_id": edge_id,
        "travel_minutes": travel_minutes,
        "closed": False,
        "max_agents_in_transit": max_agents_in_transit,
    }


@pytest.fixture
def route_edit_command() -> Callable[..., dict[str, object]]:
    """A layout Branch command that sets corridor_toilet to 8 minutes for one person at a time."""
    return _route_edit_command


def _small_replay_demo(duration_minutes: int = 5200) -> Any:
    from typing import get_args

    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.web.demo import DemoService

    document = json.loads(SMALL_REPLAY.read_text(encoding="utf-8"))
    # The ScenarioSpec schema evolves independently from this compact regression fixture.
    document["schema_version"] = get_args(ScenarioSpec.model_fields["schema_version"].annotation)[0]
    document["duration_minutes"] = duration_minutes
    document["scheduled_events"] = []
    return DemoService(ScenarioSpec.model_validate(document))


@pytest.fixture
def small_replay_demo() -> Callable[..., Any]:
    """Build a DemoService over h00_small_replay without events, 5200 minutes long by default."""
    return _small_replay_demo


# Day 4, 14:00: where the timed-plan tests fork.
DAY4_1400 = 3 * 1440 + 14 * 60


def _day4_timed_action(
    *,
    effect_id: str,
    op: str,
    target_kind: str,
    target_id: str,
    parameters: dict[str, Any],
    units: str,
    policy_dependencies: list[str],
    start_minute: int = DAY4_1400,
    end_minute: int | None = None,
    guard: Any = None,
    reversible: bool = True,
) -> Any:
    from sheltersim.events.effects import TargetSelector, TimedAction

    return TimedAction(
        effect_id=effect_id,
        op=op,
        target_selector=TargetSelector(kind=target_kind, target_id=target_id),
        start_minute=start_minute,
        end_minute=end_minute,
        guard=guard,
        parameters=parameters,
        units=units,
        source_id="H08-TEST-PLAN",
        assumption_ids=[],
        policy_dependencies=policy_dependencies,
        reversible=reversible,
    )


def _day4_plan(*, narrative: str = "Approved Day 4 plan", fork_minute: int = DAY4_1400) -> Any:
    from sheltersim.events.effects import GuardExpression, SupportMode
    from sheltersim.llm.schemas import DraftPlan

    actions = [
        _day4_timed_action(
            effect_id="temporary-toilet-outage",
            op="set_facility_availability",
            target_kind="facility",
            target_id="toilet_1",
            parameters={"working_units": 0},
            units="facility_unit",
            policy_dependencies=["facility_capacity"],
            start_minute=fork_minute,
            end_minute=fork_minute + 2,
            guard=GuardExpression(operator="gte", field="target.working_units", value=1),
        ),
        _day4_timed_action(
            effect_id="add-temporary-toilet",
            op="add_toilet",
            target_kind="zone",
            target_id="toilet",
            parameters={"count": 1},
            units="toilet_stall",
            policy_dependencies=["facility_capacity"],
            start_minute=fork_minute,
            reversible=False,
        ),
        _day4_timed_action(
            effect_id="add-support-staff",
            op="add_staff",
            target_kind="zone",
            target_id="information_desk",
            parameters={"count": 1},
            units="staff_member",
            policy_dependencies=["staff_operations"],
            start_minute=fork_minute,
            reversible=False,
        ),
    ]
    return DraftPlan(
        program_id="h08-approved-plan",
        narrative=narrative,
        source_text="Synthetic test preview; add one toilet and staff member.",
        simulation_start_minute=fork_minute,
        effects=actions,
        assumptions=[],
        support_mode=SupportMode.COMPOSED,
        compiler_provenance={
            "compiler_kind": "offline_rule",
            "compiler_version": "h08-test-v1",
        },
        branch_id="baseline",
        fork_at_minute=fork_minute,
        ambiguities=[],
        requires_confirmation=True,
    )


@pytest.fixture
def day4_plan() -> Callable[..., Any]:
    """Build a DraftPlan for the baseline Branch: a two-minute toilet outage, one added
    toilet and one added support worker, forking at Day 4 14:00 by default.
    """
    return _day4_plan


def _small_benchmark_scenario(duration_minutes: int = 200) -> Any:
    from sheltersim.experiments.benchmark import build_benchmark_scenario
    from sheltersim.experiments.scenario import ScenarioSpec

    # By minute 200 stressed synthetic Agents trigger support-priority and social tasks.
    return build_benchmark_scenario(
        ScenarioSpec.load(BASELINE_100_7D),
        agent_count=30,
        seed=7,
        duration_minutes=duration_minutes,
    )


@pytest.fixture
def small_benchmark_scenario() -> Callable[..., Any]:
    """Build a 30-person, seed 7 benchmark Scenario from baseline_100_7d, 200 minutes by
    default (long enough for stressed people to trigger support-priority and social tasks).
    """
    return _small_benchmark_scenario


def _saved_timeline(tmp_path: Path) -> tuple[Any, Any]:
    from sheltersim.core.world import WorldState
    from sheltersim.experiments.branch import BranchRuntime
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.experiments.timeline import TimelineStore
    from sheltersim.storage.projects import ProjectStore, digest

    store = ProjectStore(tmp_path)
    store.create_project("p")
    scenario = ScenarioSpec.load("fixtures/scenario_100_agents.json")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return store, timeline


@pytest.fixture
def saved_timeline() -> Callable[[Path], tuple[Any, Any]]:
    """Open a store at a folder with project p, Run r of scenario_100_agents and its
    first Checkpoint recorded; return the store and the Run's TimelineStore.
    """
    return _saved_timeline


def _fake_msvcrt() -> Any:
    import fcntl
    import struct
    import types

    def locking(fd: int, mode: int, size: int) -> None:
        kind = fcntl.F_UNLCK if mode == 0 else fcntl.F_WRLCK
        try:
            lock = struct.pack("hh4xqqi4x", kind, os.SEEK_CUR, 0, size, 0)
            fcntl.fcntl(fd, fcntl.F_OFD_SETLK, lock)
        except OSError as exc:
            raise PermissionError(13, "locked") from exc

    return types.SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0, locking=locking)


@pytest.fixture
def windows_like(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make project storage see Windows: no fcntl, msvcrt locks, and no os.O_NOFOLLOW.

    The msvcrt locks are Linux open-file-description locks, so a test using this needs
    them (fcntl.F_OFD_SETLK).
    """
    import sheltersim.storage.projects as projects_module

    monkeypatch.setattr(projects_module, "fcntl", None)
    monkeypatch.setattr(projects_module, "msvcrt", _fake_msvcrt(), raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW")


def _make_event(
    event_id: str,
    *,
    due_minute: int = 0,
    priority: int = 0,
    sequence: int = 0,
    event_type: str = "record",
    payload: dict[str, object] | None = None,
    kind: Any = None,
) -> Any:
    from sheltersim.events.models import EventKind, ScheduledEvent

    return ScheduledEvent(
        event_id=event_id,
        kind=EventKind.EXOGENOUS if kind is None else kind,
        due_minute=due_minute,
        priority=priority,
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
    )


@pytest.fixture
def make_event() -> Callable[..., Any]:
    """Build a ScheduledEvent: exogenous "record", due at minute 0, priority and sequence 0."""
    return _make_event


class _RecordHandler:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, world: Any, event: Any) -> None:
        self.applied.append(event.event_id)


@pytest.fixture
def record_handler() -> type[_RecordHandler]:
    """Make an event handler that changes nothing and lists the ids it applies, in order."""
    return _RecordHandler


@pytest.fixture
def live_checkpoint_world() -> Any:
    """failure_day3 at minute 0 with every kind of runtime state a Checkpoint must keep.

    Two people request toilet_1 (one served, one queued), A003 asks staff for
    support, the model and metric state hold values, and one event is realized
    while the Scenario's events are still pending.
    """
    from sheltersim.core.rng import CounterRNG
    from sheltersim.core.world import WorldState
    from sheltersim.domain.facilities import ToiletServiceManager
    from sheltersim.domain.staff import StaffService
    from sheltersim.events.handlers import EventDispatcher
    from sheltersim.events.models import EventKind, ScheduledEvent
    from sheltersim.experiments.scenario import ScenarioSpec

    fixture = Path(__file__).parents[1] / "fixtures" / "scenarios" / "failure_day3.json"
    world = WorldState.from_scenario(ScenarioSpec.load(fixture))
    toilet = ToiletServiceManager(world)
    toilet.request("A001", "toilet_1")
    toilet.request("A002", "toilet_1")
    toilet.arbitrate(0, CounterRNG(world.seed))
    staff = StaffService(world)
    staff.request("A003")
    staff.assign_pending(0)
    world.model_state = {"behavior-model": {"adaptive_state": [0.25, 0.75]}}
    world.metric_state["model_accumulator"] = 12.0

    audit_event = ScheduledEvent(
        event_id="checkpoint-audit",
        kind=EventKind.EXOGENOUS,
        due_minute=0,
        priority=0,
        sequence=world.allocate_event_sequence(),
        event_type="unknown_checkpoint_test_event",
    )
    world.schedule_event(audit_event)
    EventDispatcher().dispatch_due(world, 0)
    world.validate()
    assert any(task.kind == "toilet_service" for task in world.active_tasks.values())
    assert any(task.kind == "toilet_queue" for task in world.active_tasks.values())
    assert any(task.kind == "staff_support" for task in world.active_tasks.values())
    assert world.event_queue.pending()
    assert world.event_log.records
    return world


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Give each parallel worker one long simulation to start with.

    pytest-xdist's worksteal scheduler hands each worker a contiguous block of the
    collected items and lets idle workers steal from the end of other blocks. When
    the long simulations sit together, one worker runs them back to back while the
    others finish early; putting one at the head of each block starts them all at once.
    Every worker collects in this same order, so the blocks still agree.
    """
    workers = getattr(config, "workerinput", {}).get("workercount", 1)
    long_runs = [item for item in items if item.get_closest_marker("long_simulation")]
    if workers < 2 or not long_runs:
        return
    rest = [item for item in items if item.get_closest_marker("long_simulation") is None]
    size = len(items) // workers
    ordered: list[pytest.Item] = []
    for index in range(workers):
        block = rest[: size - (1 if index < len(long_runs) else 0)] if index < workers - 1 else rest
        rest = rest[len(block) :]
        ordered.extend(long_runs[index : index + 1] + block)
    ordered.extend(long_runs[workers:])
    items[:] = ordered
