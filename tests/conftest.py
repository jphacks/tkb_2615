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
