"""The offline scenarios of the all-paths gate (benchmarks/gate_all_paths.py).

Each entry builds small synthetic Scenarios from code and fixtures/, runs them and returns
the hashes of the worlds it ended with. Nothing here calls a provider over the network: a
scripted local provider writes a response tape and the replay reads that tape.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sheltersim.contracts.bootstrap import ensure_builtin_contracts
from sheltersim.contracts.policies import selected_models
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TickEngine, TraceLevel
from sheltersim.core.world import StateInvariantError, WorldState
from sheltersim.domain.staff_dynamics import staff_dynamics_on_shift
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    EffectProgram,
    GuardExpression,
    SupportMode,
    TargetSelector,
    TimedAction,
)
from sheltersim.experiments.branch import (
    AddStaffCommand,
    AddToiletCommand,
    BranchRuntime,
    RelocateFacilityCommand,
    SetFacilityWorkingUnitsCommand,
    SetZoneEdgeCommand,
)
from sheltersim.experiments.initial_scenario import InitialScenarioBuilder, InitialScenarioRequest
from sheltersim.experiments.metrics import MetricsRecorder
from sheltersim.experiments.runner import OfflineSimulationController, run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore, _MetricSink
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import ProviderResponse
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.runtime import SocialRuntimeLimits
from sheltersim.storage.projects import ProjectStore, digest
from sheltersim.web.projects import ProjectAPI

REPOSITORY = Path(__file__).resolve().parents[1]
SCENARIOS = REPOSITORY / "fixtures" / "scenarios"
SOURCE_ID = "GATE-SYNTHETIC-INPUT"


def hashes_of(world: WorldState, metrics: dict[str, Any]) -> dict[str, str]:
    """The same three hashes as benchmarks/investigate_performance.py."""
    document = world.to_dict()
    return {
        "world_hash": digest(document),
        "events_hash": digest(document["event_log"]),
        "metrics_hash": digest(metrics),
    }


@dataclass
class Outcome:
    """What one gate scenario ended with."""

    # One entry per world the scenario ended with, by a label stable across runs.
    runs: dict[str, dict[str, str]] = field(default_factory=dict)
    # Labels whose three hashes must be equal, each with what the equality means.
    must_match: list[tuple[str, str, str]] = field(default_factory=list)
    # Interventions the simulation refused, with the reason; part of the expected result.
    refused: dict[str, str] = field(default_factory=dict)

    def add(self, label: str, world: WorldState, metrics: dict[str, Any]) -> None:
        self.runs[label] = hashes_of(world, metrics)


@dataclass(frozen=True)
class GateScenario:
    name: str
    what: str
    run: Callable[[Path], Outcome]
    # Whether Rust's parallel parsing of saved records runs: only saved Runs read them.
    reads_saves: bool = False


# Scenario builders -----------------------------------------------------------------------


def generated(*, evacuees: int = 30, minutes: int = 360, seed: int = 2026, **fields: Any) -> Any:
    """The Scenario the Project screen generates for a typed form (no regional research)."""
    return (
        InitialScenarioBuilder()
        .build(
            InitialScenarioRequest(
                municipality="合成市",
                disaster="地震",
                evacuees=evacuees,
                duration_days=minutes / 1440,
                seed=seed,
                **fields,
            )
        )
        .scenario
    )


def edited(scenario: Any, change: Callable[[dict[str, Any]], None]) -> ScenarioSpec:
    document = copy.deepcopy(scenario.model_dump(mode="json"))
    change(document)
    return ScenarioSpec.model_validate(document)


def fixture_short(
    name: str, *, minutes: int, agents: int, remap: dict[str, int] | None = None
) -> ScenarioSpec:
    """A file of fixtures/scenarios cut to size; its dated events are moved inside the Run."""
    document = json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))
    document["schema_version"] = 3
    document["duration_minutes"] = minutes
    document["profiles"] = document["profiles"][:agents]
    for event in document.get("scheduled_events", []):
        if remap and event["event_type"] in remap:
            event["due_minute"] = remap[event["event_type"]]
        if event["event_type"] == "delivery_delayed":
            event["payload"]["to_minute"] = minutes // 2
    document["scheduled_events"] = [
        event for event in document.get("scheduled_events", []) if event["due_minute"] < minutes
    ]
    return ScenarioSpec.model_validate(document)


def rumor_events(*, rumor_minute: int = 5, correction_minute: int = 60) -> list[dict[str, Any]]:
    """Three synthetic claims of each truth status, with staff corrections for two."""
    events: list[dict[str, Any]] = []
    for index, (truth, origin) in enumerate(
        (("false", "A001"), ("true", "A002"), ("unknown", "A003"))
    ):
        message_id = f"claim-{truth}"
        events.append(
            {
                "id": f"rumor-{truth}",
                "kind": "exogenous",
                "due_minute": rumor_minute + index,
                "priority": 0,
                "sequence": 10 + index,
                "event_type": "information_rumor",
                "payload": {
                    "message_id": message_id,
                    "truth_status": truth,
                    "origin_agent_id": origin,
                },
                "source_id": SOURCE_ID,
            }
        )
    for index, (truth, belief) in enumerate((("false", "rejected"), ("true", "verified"))):
        events.append(
            {
                "id": f"correction-{truth}",
                "kind": "exogenous",
                "due_minute": correction_minute + index,
                "priority": 0,
                "sequence": 20 + index,
                "event_type": "information_correction",
                "payload": {
                    "message_id": f"claim-{truth}",
                    "zone_id": "sleeping",
                    "belief": belief,
                },
                "source_id": SOURCE_ID,
                "causation_id": f"rumor-{truth}",
            }
        )
    return events


def with_rumors(scenario: Any, **config: Any) -> ScenarioSpec:
    def change(document: dict[str, Any]) -> None:
        document["scheduled_events"] = [*document.get("scheduled_events", []), *rumor_events()]
        document["model_configs"]["information_spread"].update(
            base_transmission_probability=0.9,
            minimum_contact_minutes=1,
            max_contacts_per_agent=6,
            rumor_belief_probability=0.9,
            forgetting_rate_per_minute=0.002,
            **config,
        )

    return edited(scenario, change)


# Runs ------------------------------------------------------------------------------------


def run_library(
    scenario: Any,
    directory: Path,
    *,
    trace_level: TraceLevel = TraceLevel.OFF,
    router: LLMRouter | None = None,
) -> tuple[WorldState, dict[str, Any]]:
    """run_scenario, with the metrics its artifact writer ends with."""
    seen: list[Any] = []
    finalize = RunArtifactWriter.finalize

    def observed(writer: RunArtifactWriter, world: WorldState) -> None:
        finalize(writer, world)
        seen.append(writer.metrics)

    RunArtifactWriter.finalize = observed
    try:
        world = run_scenario(scenario, directory, trace_level=trace_level, llm_router=router)
    finally:
        RunArtifactWriter.finalize = finalize
    return world, seen[0].to_dict()


def open_timeline(directory: Path, scenario: Any) -> tuple[ProjectStore, TimelineStore]:
    store = ProjectStore(directory)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    return store, timeline


def timeline_hashes(store: ProjectStore, branch: str) -> tuple[WorldState, dict[str, Any]]:
    _, checkpoint = store.load_branch("p", "r", branch)
    return checkpoint.restore_world(), checkpoint.metrics_state


def single(scenario_of: Callable[[], Any], **options: Any) -> Callable[[Path], Outcome]:
    """One Run through run_scenario."""

    def run(directory: Path) -> Outcome:
        outcome = Outcome()
        outcome.add("run", *run_library(scenario_of(), directory / "run", **options))
        return outcome

    return run


def _action(
    op: str,
    kind: str,
    target: str,
    units: str,
    parameters: dict[str, Any],
    *,
    start: int,
    end: int | None = None,
    effect_id: str | None = None,
) -> TimedAction:
    """A timed effect; its policy dependencies are the ones the primitive requires."""
    return TimedAction(
        effect_id=effect_id or f"{op}-1",
        op=op,
        target_selector=TargetSelector(kind=kind, target_id=target),
        start_minute=start,
        end_minute=end,
        parameters=parameters,
        units=units,
        source_id=SOURCE_ID,
        assumption_ids=[],
        policy_dependencies=sorted(DEFAULT_PRIMITIVE_REGISTRY.get(op).required_policies),
        reversible=end is not None,
    )


def _program(name: str, *effects: TimedAction) -> EffectProgram:
    return EffectProgram(
        program_id=f"program-{name}",
        narrative=name,
        source_text=name,
        simulation_start_minute=min(effect.start_minute for effect in effects),
        effects=list(effects),
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "gate-v1"},
    )


def _fork_with(
    outcome: Outcome,
    store: ProjectStore,
    timeline: TimelineStore,
    source: str,
    name: str,
    program: EffectProgram,
    end: int,
) -> None:
    """Fork and run one intervention Branch; a refusal is recorded, as it is a result too."""
    try:
        timeline.fork(
            source,
            program.simulation_start_minute,
            name,
            program=program,
            approved_program_hash=digest(program.model_dump(mode="json")),
        )
    except ValueError as exc:
        outcome.refused[name] = str(exc)
        return
    try:
        timeline.advance(name, end, checkpoint_interval=end)
    except StateInvariantError as exc:
        # A Branch the simulation cannot finish: pinned, so that a fix shows up in the gate.
        outcome.refused[name] = f"stopped: {exc}: {exc.__cause__}"
        return
    outcome.add(name, *timeline_hashes(store, name))


def _first_minute(
    timeline: TimelineStore,
    branch: str,
    first: int,
    last: int,
    condition: Callable[[WorldState], bool],
) -> int:
    for world in timeline.iter_worlds(branch, first, last):
        if condition(world):
            return world.minute_index
    raise AssertionError(f"no minute in {first}..{last} of {branch} meets the condition")


def _staff_idle(staff_id: str) -> Callable[[WorldState], bool]:
    def check(world: WorldState) -> bool:
        return world.staff[staff_id].active_task_id is None and staff_dynamics_on_shift(
            world, staff_id, world.minute_index
        )

    return check


def _facility_idle(facility_id: str) -> Callable[[WorldState], bool]:
    def check(world: WorldState) -> bool:
        facility = world.facilities[facility_id]
        return not any(
            (
                facility.queue_agent_ids,
                facility.active_agent_ids,
                facility.resource_queue_agent_ids,
                facility.resource_active_agent_ids,
            )
        )

    return check


# The scenarios ---------------------------------------------------------------------------


def _generated_project(directory: Path) -> Outcome:
    """The route the seven-day gate uses: the Project screen's ProjectAPI worker."""
    outcome = Outcome()
    scenario = generated(evacuees=100, minutes=720)
    store = ProjectStore(directory / "projects")
    api = ProjectAPI(store, recover=False)
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    api.post(
        ["p", "runs"],
        {
            "run_id": "r",
            "scenario_id": scenario.id,
            "scenario_version": 1,
            "execution_mode": "offline",
        },
    )
    api.submit("p", "r", "main", 720)
    # Only ends a stuck run; the reversed-order variants on a busy machine take long.
    api.wait_idle(timeout=7200)
    api.close()
    outcome.add("main", *timeline_hashes(store, "main"))
    library, metrics = run_library(scenario, directory / "library")
    outcome.add("library", library, metrics)
    outcome.must_match.append(("main", "library", "saving a Run does not change it"))
    return outcome


def _trace_levels(directory: Path) -> Outcome:
    outcome = Outcome()
    scenario = with_rumors(generated(evacuees=20, minutes=240))
    for level in TraceLevel:
        world, metrics = run_library(scenario, directory / level.value, trace_level=level)
        outcome.add(level.value, world, metrics)
    # The older Scenario form traces other models (its psychology has its own trace path).
    legacy = fixture_short("baseline_100_7d", minutes=180, agents=12)
    for level in TraceLevel:
        world, metrics = run_library(legacy, directory / f"old-{level.value}", trace_level=level)
        outcome.add(f"old-{level.value}", world, metrics)
    for level in (TraceLevel.SUMMARY, TraceLevel.FULL):
        outcome.must_match.append(("off", level.value, "tracing does not change the simulation"))
        outcome.must_match.append(
            ("old-off", f"old-{level.value}", "tracing does not change the simulation")
        )
    return outcome


class ScriptedProvider:
    """A local stand-in for the paid provider: every answer is a pure function of the task.

    It picks among the actions a task allows by the hash of its input, so a Run reaches
    each kind of accepted decision.
    """

    def complete(
        self, *, model_id: str, task: Any, max_output_tokens: int, timeout_seconds: float
    ) -> ProviderResponse:
        del max_output_tokens, timeout_seconds
        return ProviderResponse(
            text=self._answer(task),
            model_id=model_id,
            input_tokens=10,
            output_tokens=5,
            elapsed_ms=0.0,
        )

    @staticmethod
    def _answer(task: Any) -> str:
        properties = task.output_schema.get("properties", {})
        pick = int(hashlib.sha256(task.input_hash.encode()).hexdigest()[:8], 16)
        if "event_id" in properties:
            actions = [item for item in properties["action"]["enum"] if item != "no_interaction"]
            reasons = [
                item for item in properties["reason_short"]["enum"] if item != "rule_fallback"
            ]
            if pick % 5 == 4:
                return task.fallback_text
            return json.dumps(
                {
                    "event_id": json.loads(task.fallback_text)["event_id"],
                    "action": actions[pick % len(actions)],
                    "reason_short": reasons[pick % len(reasons)],
                }
            )
        if "priority" in properties:
            return json.dumps(
                {"priority": pick % 3 + 1, "rationale": "synthetic scripted answer"},
                ensure_ascii=False,
            )
        if set(properties) == {"action"}:
            choices = properties["action"].get("enum") or []
            real = [choice for choice in choices if choice != "no_interaction"] or choices
            if pick % 5 == 4 or not real:
                return task.fallback_text
            return json.dumps({"action": real[pick % len(real)]})
        return task.fallback_text


def _llm_settings(mode: LLMMode) -> LLMSettings:
    return LLMSettings(
        mode=mode,
        api_key="synthetic-not-a-key" if mode is LLMMode.RECORD else None,
        max_api_calls=400,
        max_input_tokens=16_384,
        max_output_tokens=256,
        budget_usd=10.0,
    )


def _llm_tape(directory: Path) -> Outcome:
    """Decisions a scripted provider records, then the same Run from its tape alone."""
    outcome = Outcome()
    scenario = with_rumors(
        generated(evacuees=30, minutes=360, staff_count=1),
    )
    router = LLMRouter(
        _llm_settings(LLMMode.RECORD), provider=ScriptedProvider(), tape=ResponseTape()
    )
    world, metrics = run_library(scenario, directory / "record", router=router)
    outcome.add("record", world, metrics)
    replay = LLMRouter(
        _llm_settings(LLMMode.REPLAY), tape=ResponseTape.from_dict(router.tape.to_dict())
    )
    world, metrics = run_library(scenario, directory / "replay", router=replay)
    outcome.add("replay", world, metrics)
    outcome.must_match.append(("record", "replay", "a Run replays from its response tape"))
    # Everyone under stress and the daily caps opened, so each kind of decision applies.
    stressed = with_rumors(generated(evacuees=24, minutes=240, staff_count=1))
    tape: dict[str, Any] = {}
    for label, router in (
        (
            "stressed-record",
            LLMRouter(
                _llm_settings(LLMMode.RECORD), provider=ScriptedProvider(), tape=ResponseTape()
            ),
        ),
        ("stressed-replay", None),
    ):
        if router is None:
            router = LLMRouter(_llm_settings(LLMMode.REPLAY), tape=ResponseTape.from_dict(tape))
        outcome.add(label, *_stressed_run(stressed, router))
        tape = router.tape.to_dict()
    outcome.must_match.append(("stressed-record", "stressed-replay", "a Run replays from its tape"))
    # The Project route keeps the responses in the Checkpoint; a fork replays them.
    store, timeline = open_timeline(directory / "projects", scenario)
    project_router = LLMRouter(
        _llm_settings(LLMMode.RECORD), provider=ScriptedProvider(), tape=ResponseTape()
    )
    timeline.advance("main", 360, router=project_router, checkpoint_interval=60)
    outcome.add("project", *timeline_hashes(store, "main"))
    timeline.fork("main", 120, "fork")
    timeline.advance("fork", 360, checkpoint_interval=60)
    outcome.add("project-fork", *timeline_hashes(store, "fork"))
    outcome.must_match.append(
        ("project", "project-fork", "a fork replays the recorded decisions without a provider")
    )
    return outcome


def _stressed_run(scenario: Any, router: LLMRouter) -> tuple[WorldState, dict[str, Any]]:
    """Controller with wider limits than a Run's, over a synthetic stressed population."""
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    world = runtime.world
    for agent in world.agents.values():
        agent.stress = 0.9
    controller = OfflineSimulationController(world, _MetricSink(runtime.metrics), llm_router=router)
    controller.social_policy.limits = SocialRuntimeLimits(
        cooldown_minutes=5, per_agent_per_day=40, global_per_day=120, complaint_wait_minutes=5
    )
    TickEngine(world, phase_handler=controller).run(steps=scenario.duration_minutes)
    return world, runtime.metrics.to_dict()


def _fork_resume(directory: Path) -> Outcome:
    """A fork, a resume from disk and a Checkpoint round trip end as the straight Run does.

    The defined behaviour (AGENTS.md): a Checkpoint holds everything a Run needs, and a
    no-op fork has no effect of its own.
    """
    outcome = Outcome()
    end, middle = 300, 130
    scenario = with_rumors(generated(evacuees=24, minutes=end))
    store, timeline = open_timeline(directory / "straight", scenario)
    timeline.advance("main", end, checkpoint_interval=30)
    outcome.add("straight", *timeline_hashes(store, "main"))
    # Fork between two saves and at a saved minute: the first replays the gap from a save.
    for minute in (middle, 120, 1):
        name = f"fork-{minute}"
        timeline.fork("main", minute, name)
        timeline.advance(name, end, checkpoint_interval=45)
        outcome.add(name, *timeline_hashes(store, name))
        outcome.must_match.append(("straight", name, "a no-op fork ends as the straight Run"))
    # A second-generation fork reads across its parent's segment.
    timeline.fork("fork-130", 200, "fork-of-fork")
    timeline.advance("fork-of-fork", end, checkpoint_interval=30)
    outcome.add("fork-of-fork", *timeline_hashes(store, "fork-of-fork"))
    outcome.must_match.append(("straight", "fork-of-fork", "a fork of a fork too"))
    # Reading the past is read-only: the world of a seek matches the world at that minute.
    seen = timeline.seek("main", middle).world
    copied = timeline.seek("fork-130", middle).world
    if seen.canonical_json() != copied.canonical_json():
        raise AssertionError("a past minute reads differently through a fork")
    # A resume: a new store over the same directory continues a Run saved at the mid-point.
    store_two, timeline_two = open_timeline(directory / "resumed", scenario)
    timeline_two.advance("main", middle, checkpoint_interval=30)
    del store_two, timeline_two
    reopened = ProjectStore(directory / "resumed")
    TimelineStore(reopened, "p", "r").advance("main", end, checkpoint_interval=30)
    outcome.add("resumed", *timeline_hashes(reopened, "main"))
    outcome.must_match.append(("straight", "resumed", "a resume ends as the straight Run"))
    # A Checkpoint through its JSON form, without any store.
    outcome.add("checkpoint-json", *_through_checkpoint_json(scenario, middle))
    outcome.must_match.append(("straight", "checkpoint-json", "a Checkpoint holds the whole state"))
    return outcome


def _engine(world: WorldState, metrics: MetricsRecorder) -> TickEngine:
    return TickEngine(world, phase_handler=OfflineSimulationController(world, _MetricSink(metrics)))


def _through_checkpoint_json(scenario: Any, middle: int) -> tuple[WorldState, dict[str, Any]]:
    runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
    _engine(runtime.world, runtime.metrics).run(steps=middle)
    checkpoint = Checkpoint.from_json(
        Checkpoint.capture(runtime.world, metrics=runtime.metrics).to_json()
    )
    world, metrics = checkpoint.restore_state()
    _engine(world, metrics).run(steps=scenario.duration_minutes - middle)
    return world, metrics.to_dict()


def _operations() -> dict[str, Any]:
    """Outages, an absence and a shipment the operator entered on the form."""
    return {
        "equipment": [{"facility_id": "intake_desk", "capacity": 2, "working_units": 1}],
        "initial_resources": {
            "status": "entered",
            "food_meal_units": 120,
            "water_ml": 60_000,
            "source_kind": "operator",
        },
        "scheduled": [
            {
                "kind": "facility_outage",
                "facility_id": "toilet_01",
                "start_minute": 0,
                "end_minute": 120,
                "units_out": 1,
            },
            {
                "kind": "water_outage",
                "facility_id": "water_station_main",
                "start_minute": 30,
                "end_minute": 90,
            },
            {
                "kind": "staff_unavailable",
                "staff_id": "staff_002",
                "start_minute": 5,
                "end_minute": 30,
                "repeat_every_minutes": 60,
            },
            {
                "kind": "resource_delivery",
                "resource_id": "water_ml",
                "quantity": 100_000,
                "due_minute": 150,
                "warehouse_id": "main_warehouse",
            },
        ],
    }


def _interventions(directory: Path) -> Outcome:
    """Every effect primitive except those on the floor plan, each on its own Branch."""
    outcome = Outcome()
    start, end = 60, 240
    scenario = generated(evacuees=30, minutes=end, operational_conditions=_operations())
    store, timeline = open_timeline(directory / "projects", scenario)
    timeline.advance("main", end, checkpoint_interval=30)
    outcome.add("main", *timeline_hashes(store, "main"))
    world = WorldState.from_scenario(scenario)
    shipment_id = next(
        key
        for key, row in world.model_state["operational_events_v1"]["shipments"].items()
        if row["resource_id"] == "water_ml"
    )

    def zone_condition(op: str, units: str, parameters: dict[str, Any]) -> TimedAction:
        return _action(op, "zone", "sleeping", units, parameters, start=start, end=180)

    actions = {
        "add-staff": _action(
            "add_staff", "zone", "staff", "staff_member", {"count": 2}, start=start
        ),
        "add-toilets": _action(
            "add_toilet", "zone", "toilet", "toilet_stall", {"count": 2}, start=start
        ),
        "outage": _action(
            "facility_outage",
            "facility",
            "water_station_02",
            "facility_unit",
            {"units": 1},
            start=start,
            end=120,
        ),
        "availability": _action(
            "set_facility_availability",
            "facility",
            "medical_desk",
            "facility_unit",
            {"working_units": 0},
            start=start,
            end=120,
        ),
        "repair": _action(
            "facility_repair",
            "facility",
            "toilet_01",
            "facility_unit",
            {
                "facility_id": "toilet_01",
                "component_id": "toilet_01",
                "expected_working_units": 0,
                "restored_working_units": 1,
            },
            start=start,
        ),
        "water-outage": _action(
            "water_outage",
            "facility",
            "water_station_02",
            "water_supply",
            {"outage": True},
            start=start,
            end=120,
        ),
        "delivery": _action(
            "deliver_resource",
            "resource",
            "water_ml",
            "water_ml",
            {"warehouse_id": "main_warehouse", "resource_id": "water_ml", "quantity": 100_000},
            start=start,
        ),
        "delay-shipment": _action(
            "modify_supply_schedule",
            "shipment",
            shipment_id,
            "minute",
            {"shipment_id": shipment_id, "to_minute": 200},
            start=start,
        ),
        "staff-away": _action(
            "set_staff_availability",
            "staff",
            "staff_003",
            "staff_member",
            {"available": False},
            start=start,
            end=120,
        ),
        "staff-role": _action(
            "set_staff_role",
            "staff",
            "staff_002",
            "staff_role",
            {"skills": ["support", "water_service", "meal_service"]},
            start=_first_minute(timeline, "main", start, end - 30, _staff_idle("staff_002")),
        ),
        "noise": zone_condition("set_ambient_noise", "normalized_noise_0_1", {"level": 1.0}),
        "lighting": zone_condition("set_lighting", "normalized_lighting_0_1", {"level": 1.0}),
        "ventilation": zone_condition(
            "set_ventilation", "relative_ventilation_effectiveness_0_1", {"effectiveness": 0.0}
        ),
        "privacy": zone_condition("set_privacy", "normalized_privacy_0_1", {"level": 0.0}),
        "temperature": zone_condition("set_temperature", "celsius", {"temperature_c": 35.0}),
        "demand": zone_condition(
            "modify_demand_rate",
            "relative_action_rate",
            {"action": "rest", "probability_multiplier": 2.0, "duration_multiplier": 2.0},
        ),
    }
    for name, action in actions.items():
        _fork_with(outcome, store, timeline, "main", name, _program(name, action), end)
    # An order needs an idle worker with the skill, so a role change comes first.
    idle_at = _first_minute(timeline, "main", start, end - 60, _staff_idle("staff_001"))
    trained = _program(
        "trained",
        _action(
            "set_staff_role",
            "staff",
            "staff_001",
            "staff_role",
            {"skills": ["support", "cleaning", "maintenance", "patrol", "intake", "information"]},
            start=idle_at,
        ),
        _action("add_staff", "zone", "sleeping", "staff_member", {"count": 1}, start=idle_at),
    )
    _fork_with(outcome, store, timeline, "main", "trained", trained, end)
    order_at = _first_minute(timeline, "trained", idle_at + 1, end - 30, _staff_idle("staff_001"))
    for task_kind, extra in (
        ("patrol", {"target_zone_id": "sleeping"}),
        ("cleaning", {"target_zone_id": "toilet"}),
        ("information_outreach", {"target_zone_id": "sleeping"}),
        ("intake", {"target_zone_id": "intake"}),
    ):
        order = _action(
            "staff_task_order",
            "staff",
            "staff_001",
            "staff_task_order",
            {"task_kind": task_kind, "reason": "synthetic order", **extra},
            start=order_at,
        )
        _fork_with(
            outcome,
            store,
            timeline,
            "trained",
            f"order-{task_kind}",
            _program(task_kind, order),
            end,
        )

    # Maintenance needs a facility with a unit out and nothing blocking the repair.
    repair = _action(
        "staff_task_order",
        "staff",
        "staff_001",
        "staff_task_order",
        {
            "task_kind": "maintenance",
            "reason": "synthetic order",
            "target_zone_id": world.facilities["intake_desk"].zone_id,
            "target_id": "intake_desk",
        },
        start=order_at,
    )
    _fork_with(outcome, store, timeline, "trained", "order-maintenance", _program("m", repair), end)

    # Guards: a true one applies the effect, a false one suppresses its start and expiry.
    def guarded(name: str, guard: GuardExpression) -> None:
        action = _action(
            "set_facility_availability",
            "facility",
            "medical_desk",
            "facility_unit",
            {"working_units": 0},
            start=start,
            end=120,
        ).model_copy(update={"guard": guard})
        _fork_with(outcome, store, timeline, "main", name, _program(name, action), end)

    leaves = {
        "minute": GuardExpression(operator="gte", field="simulation.minute", value=start),
        "capacity": GuardExpression(operator="gt", field="target.capacity", value=0),
        "working": GuardExpression(operator="ne", field="target.working_units", value=99),
        "queue": GuardExpression(operator="lt", field="target.queue_length", value=99),
    }
    guarded("guard-and", GuardExpression(operator="and", operands=list(leaves.values())))
    guarded(
        "guard-or-false",
        GuardExpression(
            operator="or",
            operands=[
                GuardExpression(operator="eq", field="target.capacity", value=99),
                GuardExpression(operator="lte", field="simulation.minute", value=0),
            ],
        ),
    )
    for name, op, kind, target, units, parameters, field_name, value in (
        (
            "guard-zone",
            "set_ambient_noise",
            "zone",
            "sleeping",
            "normalized_noise_0_1",
            {"level": 0.5},
            "zone.occupancy",
            0,
        ),
        (
            "guard-resource",
            "deliver_resource",
            "resource",
            "water_ml",
            "water_ml",
            {"warehouse_id": "main_warehouse", "resource_id": "water_ml", "quantity": 1000},
            "resource.available_quantity",
            0,
        ),
    ):
        timed = op != "deliver_resource"
        action = _action(
            op, kind, target, units, parameters, start=start, end=120 if timed else None
        ).model_copy(
            update={"guard": GuardExpression(operator="gte", field=field_name, value=value)}
        )
        _fork_with(outcome, store, timeline, "main", name, _program(name, action), end)

    # A handoff moves a running task to another worker.
    def can_hand_off(world: WorldState) -> bool:
        record = world.model_state["staff_operations_v1"]["staff"]["staff_001"]
        added = sorted(set(world.staff) - {staff.id for staff in scenario.staff})
        return (
            record.get("phase") == "working"
            and world.staff[added[0]].active_task_id is None
            and world.staff[added[0]].zone_id == record.get("target_zone_id")
        )

    idle_again = _first_minute(timeline, "main", start, end - 60, _staff_idle("staff_001"))
    setup = _program(
        "handoff-setup",
        _action("add_staff", "zone", "sleeping", "staff_member", {"count": 1}, start=idle_again),
        _action(
            "staff_task_order",
            "staff",
            "staff_001",
            "staff_task_order",
            {"task_kind": "patrol", "target_zone_id": "sleeping", "reason": "synthetic patrol"},
            start=idle_again,
        ),
    )
    _fork_with(outcome, store, timeline, "main", "handoff-setup", setup, end)
    minute = _first_minute(timeline, "handoff-setup", idle_again, end - 1, can_hand_off)
    state = timeline.seek("handoff-setup", minute).world
    handoff = _action(
        "staff_handoff",
        "staff",
        "staff_001",
        "staff_member",
        {
            "staff_id": "staff_001",
            "replacement_staff_id": sorted(set(state.staff) - {row.id for row in scenario.staff})[
                0
            ],
            "task_id": state.model_state["staff_operations_v1"]["staff"]["staff_001"]["task_id"],
            "reason_code": "reassigned",
        },
        start=minute,
    )
    _fork_with(outcome, store, timeline, "handoff-setup", "handoff", _program("h", handoff), end)
    return outcome


def _geometry(directory: Path) -> Outcome:
    """The floor-plan primitives: the overlay, and walks that replan around it."""
    outcome = Outcome()
    start, end = 30, 300
    # The polygons below fit the plan generated for this many people.
    scenario = generated(evacuees=20, minutes=end)
    store, timeline = open_timeline(directory / "projects", scenario)
    timeline.advance("main", end, checkpoint_interval=30)
    outcome.add("main", *timeline_hashes(store, "main"))
    toilet = scenario.model_configs["facility_geometry"]["facility_points"]["toilet_01"]
    free = _first_minute(timeline, "main", start, end - 30, _facility_idle("toilet_01"))
    room_points = [
        {"x": 0.5, "y": 8.0},
        {"x": 2.5, "y": 8.0},
        {"x": 2.5, "y": 12.0},
        {"x": 0.5, "y": 12.0},
    ]
    beds = [
        {"x": 1.0, "y": 9.0},
        {"x": 2.0, "y": 9.0},
        {"x": 1.0, "y": 11.0},
        {"x": 2.0, "y": 11.0},
    ]
    actions: dict[str, TimedAction] = {
        "move-facility": _action(
            "move_facility",
            "facility",
            "toilet_01",
            "metre_2d",
            {"point_m": {"x": toilet["x"], "y": 8.0}},
            start=free,
        ),
        "transfer": _action(
            "transfer_equipment",
            "equipment",
            "toilet_01",
            "metre_2d",
            {
                "schema_version": 1,
                "equipment_id": "toilet_01",
                "from_zone_id": "toilet",
                "to_zone_id": "sleeping",
                "point_m": {"x": 5.0, "y": 9.0},
            },
            start=free,
        ),
        "transfer-refused": _action(
            "transfer_equipment",
            "equipment",
            "water_station_main",
            "metre_2d",
            {
                "schema_version": 1,
                "equipment_id": "water_station_main",
                "from_zone_id": "food",
                "to_zone_id": "sleeping",
                "point_m": {"x": 5.0, "y": 9.0},
            },
            start=start,
        ),
        "partition": _action(
            "set_partition",
            "zone",
            "toilet",
            "metre_2d",
            {
                "polygon_m": [
                    {"x": 13.9, "y": 8.4},
                    {"x": 14.9, "y": 8.4},
                    {"x": 14.9, "y": 9.0},
                    {"x": 13.9, "y": 9.0},
                ]
            },
            start=start,
            end=120,
        ),
        "temporary-room": _action(
            "temporary_room_geometry",
            "zone",
            "sleeping",
            "metre_2d",
            {
                "schema_version": 1,
                "room_id": "room_a",
                "zone_id": "sleeping",
                "polygon_m": room_points,
                "people_capacity": 4,
                "sleeping_capacity": 4,
                "bed_points_m": beds,
                "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
            },
            start=start,
            end=150,
        ),
        "close-route": _action(
            "set_route_access",
            "route",
            "route_corridor_toilet",
            "boolean",
            {"accessible": False},
            start=start,
            end=95,
        ),
    }
    for name, action in actions.items():
        _fork_with(outcome, store, timeline, "main", name, _program(name, action), end)
    # A partition is refused while someone stands where it goes, so wait for a clear minute.
    for minute in range(start, end - 90, 10):
        partition = actions["partition"].model_copy(
            update={"start_minute": minute, "end_minute": minute + 90}
        )
        outcome.refused.pop("partition", None)
        _fork_with(outcome, store, timeline, "main", "partition", _program("p", partition), end)
        if "partition" in outcome.runs:
            break

    # A route closed under a resident walking to a support worker.
    trip = None
    for state in timeline.iter_worlds("main", start, end - 30):
        for journey in sorted(
            state.model_state["movement_v1"]["journeys"].values(), key=lambda row: row["event_id"]
        ):
            if journey.get("purpose") == "staff_support_travel" and journey.get("route_edge_ids"):
                trip = (state.minute_index, journey["route_edge_ids"][-1])
                break
        if trip is not None:
            break
    if trip is not None:
        closure = _action(
            "set_route_access",
            "route",
            trip[1],
            "boolean",
            {"accessible": False},
            start=trip[0],
            end=trip[0] + 20,
        )
        _fork_with(
            outcome, store, timeline, "main", "support-trip-closed", _program("t", closure), end
        )

    # A route closed under a worker who is walking it: the trip is replanned or given up.
    idle_at = _first_minute(timeline, "main", start, end - 60, _staff_idle("staff_001"))
    patrol = _action(
        "staff_task_order",
        "staff",
        "staff_001",
        "staff_task_order",
        {"task_kind": "patrol", "target_zone_id": "sleeping", "reason": "synthetic patrol"},
        start=idle_at,
    )
    _fork_with(outcome, store, timeline, "main", "walk", _program("walk", patrol), end)

    def walking(world: WorldState) -> bool:
        record = world.model_state["staff_operations_v1"]["staff"]["staff_001"]
        return record.get("phase") == "travel"

    if "walk" in outcome.runs:
        minute = _first_minute(timeline, "walk", idle_at, end - 30, walking)
        record = timeline.seek("walk", minute).world.model_state["staff_operations_v1"]["staff"][
            "staff_001"
        ]
        closure = _action(
            "set_route_access",
            "route",
            record["route_edge_ids"][-1],
            "boolean",
            {"accessible": False},
            start=minute,
            end=minute + 15,
        )
        _fork_with(
            outcome, store, timeline, "walk", "walk-closed", _program("closed", closure), end
        )
    return outcome


def _known_stops(directory: Path) -> Outcome:
    """Valid interventions that stop the Run today; each is listed in docs/rust-port.md.

    The stop is the expected result, so that a fix or another stop shows up in the gate.
    """
    outcome = Outcome()
    # The closure's expiry is due in the minute a support request times out. That request's
    # shared operations record is written after the batch of due events, and the expiry
    # validates the world inside the batch.
    scenario = generated(evacuees=30, minutes=180)
    store, timeline = open_timeline(directory / "expiry", scenario)
    timeline.advance("main", 30, checkpoint_interval=30)
    closure = _action(
        "set_route_access",
        "route",
        "route_corridor_toilet",
        "boolean",
        {"accessible": False},
        start=30,
        end=90,
    )
    _fork_with(outcome, store, timeline, "main", "expiry-at-a-timeout", _program("c", closure), 180)
    # A guard on an absence with an end: the restoration event inherits the guard.
    scenario = generated(evacuees=30, minutes=180)
    store, timeline = open_timeline(directory / "guard", scenario)
    timeline.advance("main", 60, checkpoint_interval=60)
    absence = _action(
        "set_staff_availability",
        "staff",
        "staff_003",
        "staff_member",
        {"available": False},
        start=60,
        end=120,
    ).model_copy(
        update={"guard": GuardExpression(operator="eq", field="staff.available", value=True)}
    )
    _fork_with(outcome, store, timeline, "main", "guarded-absence", _program("g", absence), 180)
    return outcome


def _population(directory: Path) -> Outcome:
    """Arrivals, departures and each presence change of one person."""
    outcome = Outcome()
    start, end = 30, 240
    scenario = generated(evacuees=30, minutes=end)
    store, timeline = open_timeline(directory / "projects", scenario)
    timeline.advance("main", end, checkpoint_interval=30)
    outcome.add("main", *timeline_hashes(store, "main"))
    world = timeline.seek("main", start).world
    people = sorted(world.agents)
    free = [
        agent_id
        for agent_id in people
        if world.agents[agent_id].activity.value in ("idle", "resting", "sleeping")
    ]
    waiting = [agent_id for agent_id in people if world.agents[agent_id].support_request_id]
    by_zone: dict[str, list[str]] = {}
    for agent_id in free:
        by_zone.setdefault(world.agents[agent_id].zone_id, []).append(agent_id)
    pair = next(members[:2] for _, members in sorted(by_zone.items()) if len(members) > 1)

    def presence(effect_id: str, person: str, *, at: int = start, **parameters: Any) -> TimedAction:
        return _action(
            "population_presence",
            "person",
            person,
            "population_transition",
            parameters,
            start=at,
            effect_id=effect_id,
        )

    reunion = presence(
        "reunite",
        pair[0],
        action="reunite",
        household_id=world.agents[pair[0]].profile.group_id,
    ).model_copy(update={"target_selector": TargetSelector(kind="person", candidate_ids=pair)})
    programs = {
        "arrive": [
            _action(
                "add_evacuees",
                "zone",
                "sleeping",
                "evacuee",
                {"count": 6, "household_size": 2, "mobility": "assisted"},
                start=start,
            ),
        ],
        "arrive-space": [
            _action(
                "add_evacuees",
                "zone",
                "sleeping",
                "evacuee",
                {"count": 20, "mobility": "wheelchair"},
                start=start,
            ),
            _action(
                "add_temporary_space",
                "zone",
                "sleeping",
                "person_capacity",
                {"people_capacity": 20, "sleeping_capacity": 20},
                start=start,
                end=180,
            ),
        ],
        "depart": [
            _action(
                "depart_evacuees",
                "zone",
                "sleeping",
                "evacuee",
                {"count": 5, "include_initial": True},
                start=start,
            ),
        ],
        "exit-expected-return": [
            presence("exit", free[0], action="temporary_exit", expected_return_minute=start + 60),
        ],
        "exit-then-return": [
            presence("exit", free[0], action="temporary_exit"),
            presence("return", free[0], at=start + 40, action="return"),
        ],
        "transfer": [presence("transfer", free[0], action="transfer")],
        "relocate": [presence("relocate", free[0], action="relocate", zone_id="quiet")],
        "reunite": [reunion],
    }
    if waiting:
        programs["exit-waiting"] = [
            presence("exit", waiting[0], action="temporary_exit", expected_return_minute=start + 90)
        ]
    for name, actions in programs.items():
        _fork_with(outcome, store, timeline, "main", name, _program(name, *actions), end)
    return outcome


def _conditions(directory: Path) -> Outcome:
    """Every kind of condition the operator can enter on the form, including infrastructure."""
    outcome = Outcome()
    infrastructure = {
        "dependency_edges": [
            {"dependent_id": "toilet_01", "dependency_id": "panel-main"},
            {"dependent_id": "accessible_toilet_01", "dependency_id": "panel-main"},
            {"dependent_id": "panel-main", "dependency_id": "grid-main"},
        ],
        "heat_stress_per_minute": 0.001,
        "cold_stress_per_minute": 0.001,
        "source_id": SOURCE_ID,
    }
    scheduled = [
        {
            "kind": "power_outage",
            "component_id": "grid-main",
            "start_minute": 20,
            "end_minute": 70,
            "repeat_every_minutes": 150,
        },
        {
            "kind": "equipment_failure",
            "facility_id": "water_station_main",
            "units_out": 1,
            "start_minute": 40,
            "end_minute": 90,
        },
        {"kind": "extreme_heat", "zone_id": "sleeping", "start_minute": 10, "end_minute": 100},
        {"kind": "extreme_cold", "zone_id": "food", "start_minute": 110, "end_minute": 200},
        {
            "kind": "staff_unavailable",
            "staff_id": "staff_001",
            "start_minute": 15,
            "end_minute": 45,
            "repeat_every_minutes": 70,
        },
        {
            "kind": "resource_loss",
            "resource_id": "water_ml",
            "quantity": 2_000,
            "due_minute": 50,
        },
        {
            "kind": "resource_delivery",
            "resource_id": "food_meal_units",
            "quantity": 40,
            "due_minute": 120,
            "repeat_every_minutes": 120,
        },
    ]
    conditions = {
        "initial_resources": {
            "status": "entered",
            "food_meal_units": 20,
            "water_ml": 8_000,
            "source_kind": "operator",
        },
        "scheduled": scheduled,
        "infrastructure": infrastructure,
    }
    scenario = generated(evacuees=40, minutes=300, operational_conditions=conditions)
    outcome.add("run", *run_library(scenario, directory / "run"))

    # With the staff all away the queues stall: requests time out and walks are interrupted.
    def understaffed(document: dict[str, Any]) -> None:
        document["staff"] = [row for row in document["staff"] if row["id"] in ("staff_001",)]

    thin = edited(
        generated(
            evacuees=30,
            minutes=240,
            operational_conditions={
                "scheduled": [
                    {
                        "kind": "staff_unavailable",
                        "staff_id": "staff_001",
                        "start_minute": 10,
                        "end_minute": 40,
                        "repeat_every_minutes": 50,
                    }
                ]
            },
        ),
        understaffed,
    )
    outcome.add("one-worker", *run_library(thin, directory / "one-worker"))
    return outcome


def _split_queues(directory: Path) -> Outcome:
    """Two service points with a queue each, which the generator never makes."""
    outcome = Outcome()

    def split(document: dict[str, Any]) -> None:
        geometry = document["model_configs"]["facility_geometry"]
        facilities = {row["id"]: row for row in document["facilities"]}
        zones = {row["id"]: row["zone_id"] for row in document["facilities"]}
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

    scenario = edited(generated(evacuees=30, minutes=300), split)
    outcome.add("run", *run_library(scenario, directory / "run"))
    fifo = edited(
        generated(evacuees=30, minutes=300, queue_policy_id="strict-fifo-queue-v1"), split
    )
    outcome.add("strict-fifo", *run_library(fifo, directory / "strict-fifo"))
    return outcome


def _branch_commands(directory: Path) -> Outcome:
    """The layout commands of the older demo, on the older Scenario form that has zone edges."""
    del directory
    outcome = Outcome()
    commands = {
        "add-staff": AddStaffCommand("S-EXTRA", "toilet", ("support",)),
        "add-toilet": AddToiletCommand("toilet-extra", "toilet", 1, 1),
        "working-units": SetFacilityWorkingUnitsCommand("toilet_1", 0),
        "route-capacity": SetZoneEdgeCommand("corridor_toilet", 8, False, 1),
        "route-closed": SetZoneEdgeCommand("corridor_toilet", 4, True, None),
        "relocate": RelocateFacilityCommand("toilet_2", "corridor"),
    }
    for name, command in commands.items():
        scenario = fixture_short("h00_small_replay", minutes=120, agents=4)
        runtime = BranchRuntime.from_world(WorldState.from_scenario(scenario))
        _engine(runtime.world, runtime.metrics).run(steps=20)
        child = runtime.fork(name)
        try:
            child.apply(command)
        except ValueError as exc:
            outcome.refused[name] = str(exc)
            continue
        _engine(child.world, child.metrics).run(steps=100)
        outcome.add(name, child.world, child.metrics.to_dict())
    return outcome


def _arrivals(directory: Path) -> Outcome:
    """Arrivals that walk in from outside, wait for intake and fill the alternative zones."""
    outcome = Outcome()
    start, end = 20, 300

    def approach(document: dict[str, Any]) -> None:
        document["model_configs"]["facility_geometry"]["arrival_approach_routes"] = [
            {
                "route_id": "arrival-to-corridor",
                "destination_zone_id": "corridor",
                "points_m": _corridor_route(document),
                # Slow, so that the arrivals are still at the door when an intake is ordered.
                "walking_speed_m_per_min": 0.5,
                "source_kind": "operator",
                "source_ids": ["SRC-GATE-APPROACH-ROUTE"],
            }
        ]

    scenario = edited(generated(evacuees=30, minutes=end), approach)
    store, timeline = open_timeline(directory / "projects", scenario)
    timeline.advance("main", end, checkpoint_interval=30)
    outcome.add("main", *timeline_hashes(store, "main"))

    def arrival(effect_id: str, zone: str, at: int, **parameters: Any) -> TimedAction:
        return _action(
            "add_evacuees", "zone", zone, "evacuee", parameters, start=at, effect_id=effect_id
        )

    programs = {
        "walk-in": [
            _action("add_staff", "zone", "intake", "staff_member", {"count": 5}, start=start),
            arrival(
                "walk-in",
                "corridor",
                start,
                count=4,
                household_size=2,
                approach_route_id="arrival-to-corridor",
            ),
        ],
        "overflow": [
            arrival(
                "overflow",
                "toilet",
                start,
                count=200,
                household_size=3,
                admission_zone_ids=["quiet", "sleeping"],
                person_context_overrides=[
                    {"member_index": 0, "care_support_needs": ["hygiene_support"]},
                ],
            )
        ],
        "room-arrivals": [
            _action(
                "temporary_room_geometry",
                "zone",
                "sleeping",
                "metre_2d",
                {
                    "schema_version": 1,
                    "room_id": "room_a",
                    "zone_id": "sleeping",
                    "polygon_m": [
                        {"x": 0.5, "y": 8.0},
                        {"x": 2.5, "y": 8.0},
                        {"x": 2.5, "y": 12.0},
                        {"x": 0.5, "y": 12.0},
                    ],
                    "people_capacity": 4,
                    "sleeping_capacity": 4,
                    "bed_points_m": [
                        {"x": 1.0, "y": 9.0},
                        {"x": 2.0, "y": 9.0},
                        {"x": 1.0, "y": 11.0},
                        {"x": 2.0, "y": 11.0},
                    ],
                    "route_edge_ids": ["route_corridor_sleeping", "route_sleeping_corridor"],
                },
                start=start,
                end=150,
            ),
            arrival("to-room", "sleeping", start + 1, count=8),
        ],
        "later-departure": [
            arrival("in", "sleeping", start, count=6),
            _action(
                "depart_evacuees",
                "zone",
                "sleeping",
                "evacuee",
                {"count": 3},
                start=start + 40,
                effect_id="out",
            ),
        ],
    }
    for name, actions in programs.items():
        _fork_with(outcome, store, timeline, "main", name, _program(name, *actions), end)

    # An intake order needs arrivals still waiting at the door and a free worker.
    added = sorted(
        set(timeline.seek("walk-in", end).world.staff) - {row.id for row in scenario.staff}
    )

    def free_worker(world: WorldState) -> str | None:
        return next((key for key in added if world.staff[key].active_task_id is None), None)

    def intake_possible(world: WorldState) -> bool:
        pending = world.model_state.get("population_dynamics_v1", {}).get("pending_arrivals")
        return bool(pending) and free_worker(world) is not None

    minute = _first_minute(timeline, "walk-in", start + 1, end - 30, intake_possible)
    worker = free_worker(timeline.seek("walk-in", minute).world)
    order = _action(
        "staff_task_order",
        "staff",
        worker,
        "staff_task_order",
        {"task_kind": "intake", "reason": "synthetic intake", "target_zone_id": "intake"},
        start=minute,
    )
    _fork_with(outcome, store, timeline, "walk-in", "intake", _program("intake", order), end)
    return outcome


def _corridor_route(document: dict[str, Any]) -> list[dict[str, float]]:
    """From outside the plan, which grows to hold it, to the middle of the corridor."""
    geometry = document["model_configs"]["facility_geometry"]
    corridor = next(row for row in geometry["zones"] if row["zone_id"] == "corridor")
    points = corridor["polygon"]
    middle = {
        "x": sum(point["x"] for point in points) / len(points),
        "y": sum(point["y"] for point in points) / len(points),
    }
    outside = float(geometry["height_m"]) + 5.0
    geometry["height_m"] = outside + 5.0
    return [{"x": middle["x"], "y": outside}, middle]


def _scarcity(directory: Path) -> Outcome:
    """Stock that runs out, so services stall, and care needs that the default form lacks."""
    outcome = Outcome()
    empty = generated(
        evacuees=24,
        minutes=480,
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "food_meal_units": 4,
                "water_ml": 1_500,
                "source_kind": "operator",
            },
        },
    )
    outcome.add("empty-stock", *run_library(empty, directory / "empty-stock"))
    care = generated(
        evacuees=30,
        minutes=480,
        staff_count=2,
        population_conditions={
            "medical_support_rate": 0.4,
            "medication_support_rate": 0.4,
            "care_support_need_rates": {
                "caregiver_assistance": 0.4,
                "privacy_accommodation": 0.4,
                "mobility_assistance": 0.4,
                "hygiene_support": 0.4,
                "emotional_support": 0.4,
                "language_access": 0.4,
                "dependent_assistance": 0.4,
            },
            "pet_household_rate": 0.3,
            "daily_rhythms": [
                {
                    "rhythm_id": "night-shift",
                    "sleep_start_minute": 480,
                    "wake_minute": 900,
                    "weight": 1.0,
                },
                {
                    "rhythm_id": "day",
                    "sleep_start_minute": 1320,
                    "wake_minute": 360,
                    "weight": 2.0,
                },
            ],
            "source_ids": [SOURCE_ID],
        },
    )
    outcome.add("care-needs", *run_library(care, directory / "care-needs"))
    return outcome


def _operational(directory: Path) -> Outcome:
    """Stock, deliveries and outages from the fixture files, with their dated events moved."""
    outcome = Outcome()
    moved = {
        "resource_delivery": 20,
        "delivery_delayed": 10,
        "water_outage": 60,
        "water_restored": 80,
        "facility_outage": 40,
        "facility_restored": 100,
    }
    for name in (
        "multi_operational",
        "water_intervention",
        "failure_day3",
        "delivery_intervention",
        "research_shortage_100_7d",
    ):
        scenario = fixture_short(name, minutes=200, agents=20, remap=moved)
        outcome.add(name, *run_library(scenario, directory / name))
    return outcome


def _long_generated(directory: Path) -> Outcome:
    """Two days of a few people: rhythms, shifts and supplies that only a day boundary reaches."""
    outcome = Outcome()
    scenario = generated(
        evacuees=16,
        minutes=2880,
        staff_count=3,
        default_supply_start_day=2,
        operational_conditions={
            "initial_resources": {
                "status": "entered",
                "food_meal_units": 30,
                "water_ml": 30_000,
                "source_kind": "operator",
            },
        },
    )
    outcome.add("run", *run_library(scenario, directory / "run"))
    return outcome


_LEGACY_PSYCHOLOGY = {
    "natural_recovery_rate_per_minute": 0.01,
    "chronic_retention": 0.9,
    "chronic_acute_weight": 0.08,
    "chronic_high_distress_weight": 0.02,
    "high_distress_threshold": 0.7,
}


def _small_legacy(registry: dict[str, str], *, psychology: dict[str, Any] | None = None) -> Any:
    def change(document: dict[str, Any]) -> None:
        document["scheduled_events"] = rumor_events(rumor_minute=2, correction_minute=30)
        for event in document["scheduled_events"]:
            event["payload"].pop("zone_id", None)
            if event["event_type"] == "information_correction":
                event["payload"]["zone_id"] = document["profiles"][0]["initial_zone_id"]
        configs = document["model_configs"]
        configs["policy_registry"] = {"schema_version": 1, **registry}
        if psychology is not None:
            configs["psychology"] = psychology
        configs["information_spread"] = {
            "enabled": True,
            "max_contacts_per_agent": 6,
            "minimum_contact_minutes": 1,
            "contact_duration_scale_minutes": 1,
            "base_transmission_probability": 0.9,
            "correction_transmission_multiplier": 1.0,
            "trust_sensitivity": 0.2,
            "family_contact_multiplier": 1.0,
            "adjacent_zone_contact_multiplier": 1.0,
            "query_pressure_per_claim": 0.5,
            "query_pressure_decay_per_minute": 0.0,
            "query_pressure_relief_per_correction": 0.5,
            "query_threshold": 0.5,
            "query_utility_weight": 4.0,
        }

    return edited(fixture_short("baseline_100_7d", minutes=240, agents=20), change)


# The older Scenario form is the only one that can select these models; the generated form
# requires a life-state psychology.
_MODELS_ON_THE_OLD_FORM: dict[tuple[str, str], Callable[[], Any]] = {
    ("psychology", "causal-stress-v1"): lambda: _small_legacy({"psychology": "causal-stress-v1"}),
    ("psychology", "bounded-stress-v1"): lambda: _small_legacy({"psychology": "bounded-stress-v1"}),
    ("psychology", "mean-reverting-distress-v1"): lambda: _small_legacy(
        {"psychology": "mean-reverting-distress-v1"}, psychology=_LEGACY_PSYCHOLOGY
    ),
    ("psychology", "life-state-feedback-v1"): lambda: _small_legacy(
        {"psychology": "life-state-feedback-v1"}
    ),
}

# The form field that selects a model of the domain on the generated Scenario.
_REQUEST_FIELDS = {
    "behavior": "behavior_policy_id",
    "social": "social_policy_id",
    "information": "information_policy_id",
    "queue": "queue_policy_id",
    "relationship": "relationship_policy_id",
    "social_proximity": "social_proximity_policy_id",
    "body_dynamics": "body_dynamics_policy_id",
    "staff_operations": "staff_operations_policy_id",
    "staff_fatigue": "staff_fatigue_policy_id",
    "zone_environment": "zone_temperature_policy_id",
    "zone_rest": "zone_rest_policy_id",
    "zone_hygiene": "zone_hygiene_policy_id",
    "zone_privacy": "zone_privacy_policy_id",
    "care_outcome": "care_outcome_policy_id",
}


def _model_scenario(domain: str, model_id: str) -> Any:
    """A Scenario that selects the model; a silent fallback would verify nothing."""
    scenario = _scenario_selecting(domain, model_id)
    selected = selected_models(scenario.model_configs).get(domain)
    if selected != model_id:
        raise AssertionError(f"{domain} selected {selected!r}, not {model_id!r}")
    return scenario


def _scenario_selecting(domain: str, model_id: str) -> Any:
    legacy = _MODELS_ON_THE_OLD_FORM.get((domain, model_id))
    if legacy is not None:
        return legacy()
    base: dict[str, Any] = {"evacuees": 20, "minutes": 180}
    if domain in _REQUEST_FIELDS:
        return with_rumors(generated(**base, **{_REQUEST_FIELDS[domain]: model_id}))

    def change(document: dict[str, Any]) -> None:
        document["model_configs"]["policy_registry"][domain] = model_id
        # A suite fixes its members, and these selections replace a member.
        document["model_configs"]["policy_registry"].pop("suite_id", None)

    return edited(with_rumors(generated(**base)), change)


def selectable_models() -> list[tuple[str, str]]:
    """Every (domain, model) of the policy registry, in a stable order."""
    ensure_builtin_contracts()
    from sheltersim.contracts.policies import MODEL_DEFINITIONS

    return sorted((row["domain"], model_id) for model_id, row in MODEL_DEFINITIONS.items())


def all_scenarios() -> dict[str, GateScenario]:
    scenarios = [
        GateScenario(
            "generated_project",
            "100人の生成Scenarioを画面の経路で12時間（今の7日分の確認の短縮形）",
            _generated_project,
            reads_saves=True,
        ),
        GateScenario(
            "long_generated", "少人数の2日間（日付をまたぐ処理、配給、シフト）", _long_generated
        ),
        GateScenario("trace_levels", "すべてのtrace level（新旧のScenarioの形）", _trace_levels),
        GateScenario(
            "fork_resume", "途中からの分岐・再開・Checkpointの往復", _fork_resume, reads_saves=True
        ),
        GateScenario(
            "llm_tape", "記録した応答（台本のProvider）とその再生", _llm_tape, reads_saves=True
        ),
        GateScenario(
            "interventions",
            "床図以外のEffectProgramのすべての操作",
            _interventions,
            reads_saves=True,
        ),
        GateScenario("geometry", "床図を変える操作と迂回", _geometry, reads_saves=True),
        GateScenario(
            "population", "人の出入りとpopulation_presence", _population, reads_saves=True
        ),
        GateScenario(
            "operational", "fixtureの運営Scenario（在庫・配送・断水・故障）", _operational
        ),
        GateScenario("conditions", "入力できるすべての運営条件とインフラの依存", _conditions),
        GateScenario("split_queues", "1つの設備に複数の列", _split_queues),
        GateScenario("branch_commands", "旧demoの配置コマンド", _branch_commands),
        GateScenario("arrivals", "外から歩いて来る到着と受け入れ", _arrivals, reads_saves=True),
        GateScenario("scarcity", "在庫切れとケアの必要", _scarcity),
        GateScenario(
            "known_stops", "今はRunが止まる有効な介入（不具合）", _known_stops, reads_saves=True
        ),
    ]
    for domain, model_id in selectable_models():
        scenarios.append(
            GateScenario(
                f"model__{domain}__{model_id}",
                f"{domain}のモデル{model_id}",
                # Bound by default arguments: the loop variables change.
                single(lambda domain=domain, model_id=model_id: _model_scenario(domain, model_id)),
            )
        )
    return {scenario.name: scenario for scenario in scenarios}
