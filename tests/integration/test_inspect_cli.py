"""Pre-run scenario preview, freezing, and the offline run inspector through the CLI.

Running a frozen Scenario from the CLI is checked in tests/e2e/test_cli_run.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.paths import SCENARIO_100_AGENTS

from sheltersim.__main__ import main
from sheltersim.core.engine import TraceLevel
from sheltersim.core.rng import CounterRNG
from sheltersim.core.world import WorldState
from sheltersim.events.handlers import EventDispatcher
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.inspection.traces import RunArtifactWriter
from sheltersim.policies.behavior import UtilityBehaviorPolicy, WorldView


def make_scenario_file(path: Path) -> Path:
    scenario_data = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    scenario_data["scheduled_events"] = [
        {
            "id": "planned-facility-failure",
            "kind": "exogenous",
            "due_minute": 5,
            "priority": 0,
            "sequence": 0,
            "event_type": "facility_outage",
            "payload": {"facility_id": "toilet_1", "units_out": 1},
            "source_id": "fixture-plan",
            "causation_id": None,
            "provenance": {"source_type": "assumption"},
        }
    ]
    scenario = ScenarioSpec.model_validate(scenario_data)
    scenario.save(path)
    return path


def make_run(directory: Path) -> tuple[Path, str]:
    scenario = ScenarioSpec.load(make_scenario_file(directory.parent / "scenario.json"))
    world = WorldState.from_scenario(scenario)
    writer = RunArtifactWriter(scenario, directory, trace_level=TraceLevel.FULL)
    writer.write_initial(world)
    proposal = UtilityBehaviorPolicy().propose(
        WorldView.from_world(world), "A001", CounterRNG(world.seed)
    )
    writer.record_decision(proposal)
    first_event = scenario.scheduled_events[0]
    world.minute_index = first_event.due_minute
    EventDispatcher().dispatch_due(world, first_event.due_minute)
    writer.capture_metric(world, minute=first_event.due_minute)
    writer.finalize(world)
    return directory, first_event.id


def test_prepare_previews_all_profiles_and_planned_events_offline(tmp_path: Path, capsys) -> None:
    scenario_path = make_scenario_file(tmp_path / "scenario.json")
    assert main(["prepare", "--scenario", str(scenario_path)]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview["frozen"] is False
    assert preview["profile_count"] == 100
    assert len(preview["scenario"]["profiles"]) == 100
    assert preview["planned_event_count"] == len(preview["scenario"]["scheduled_events"]) == 1
    assert preview["scenario_hash"]


def test_prepare_freeze_writes_reviewable_canonical_snapshot(tmp_path: Path, capsys) -> None:
    scenario_path = make_scenario_file(tmp_path / "scenario.json")
    output = tmp_path / "prepared" / "scenario_preview.json"

    assert (
        main(["prepare", "--scenario", str(scenario_path), "--freeze", "--output", str(output)])
        == 0
    )

    preview = json.loads(output.read_text(encoding="utf-8"))
    assert preview["frozen"] is True
    assert preview["approval"] == {"approved": True, "method": "prepare --freeze"}
    assert preview == json.loads(capsys.readouterr().out)


def test_freeze_records_manual_generated_field_edits_and_checks_content_hash(
    tmp_path: Path, capsys
) -> None:
    scenario_path = make_scenario_file(tmp_path / "scenario.json")
    draft_path = tmp_path / "draft.json"
    assert main(["prepare", "--scenario", str(scenario_path), "--output", str(draft_path)]) == 0
    capsys.readouterr()
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    generated = json.loads(json.dumps(draft["scenario"]))
    draft["generated_scenario"] = generated
    draft["generation_manifest"] = {
        "schema_version": 1,
        "generator": "test-generator",
        "model_id": "test-model",
        "prompt_version": "test-v1",
        "output_hash": "abc",
        "status": "generated",
        "estimated_cost_usd": 0.01,
        "cache_hit": False,
    }
    draft["scenario"]["profiles"][0]["stress_sensitivity"] = 0.77
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    frozen_path = tmp_path / "frozen.json"

    assert (
        main(
            [
                "prepare",
                "--scenario",
                str(draft_path),
                "--freeze",
                "--output",
                str(frozen_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    assert frozen["generation_manifest"]["manual_edits"] == ["profiles[0].stress_sensitivity"]
    assert frozen["generation_manifest"]["approved"] is True
    assert frozen["scenario"]["provenance"]["llm_scenario_generation"]["manual_edits"] == [
        "profiles[0].stress_sensitivity"
    ]

    frozen["scenario"]["seed"] += 1
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "run",
                "--offline",
                "--scenario",
                str(frozen_path),
                "--output",
                str(tmp_path / "tampered-run"),
            ]
        )
    assert raised.value.code == 2
    assert "hash does not match" in capsys.readouterr().err


def test_prepare_requires_output_for_explicit_freeze(tmp_path: Path, capsys) -> None:
    scenario_path = make_scenario_file(tmp_path / "scenario.json")
    with pytest.raises(SystemExit) as raised:
        main(["prepare", "--scenario", str(scenario_path), "--freeze"])

    assert raised.value.code == 2
    assert "requires --output" in capsys.readouterr().err


def test_run_rejects_a_preview_that_was_not_frozen(tmp_path: Path, capsys) -> None:
    scenario_path = make_scenario_file(tmp_path / "scenario.json")
    preview_path = tmp_path / "preview.json"
    assert (
        main(
            [
                "prepare",
                "--scenario",
                str(scenario_path),
                "--output",
                str(preview_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "run",
                "--offline",
                "--scenario",
                str(preview_path),
                "--output",
                str(tmp_path / "run"),
            ]
        )

    assert raised.value.code == 2
    assert "not frozen" in capsys.readouterr().err


def test_run_requires_explicitly_frozen_scenario_input(tmp_path: Path, capsys) -> None:
    scenario_path = make_scenario_file(tmp_path / "scenario.json")
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "run",
                "--offline",
                "--scenario",
                str(scenario_path),
                "--output",
                str(tmp_path / "run"),
            ]
        )

    assert raised.value.code == 2
    assert "reviewed frozen preview" in capsys.readouterr().err


def test_inspect_profiles_and_planned_events_show_full_pre_run_inputs(
    tmp_path: Path, capsys
) -> None:
    run_directory, _ = make_run(tmp_path / "run")

    assert main(["inspect", "--run", str(run_directory), "--profiles"]) == 0
    profiles = json.loads(capsys.readouterr().out)["profiles"]
    assert len(profiles) == 100
    assert profiles[0]["id"] == "A001"

    assert main(["inspect", "--run", str(run_directory), "--events", "--planned"]) == 0
    event_list = json.loads(capsys.readouterr().out)
    assert event_list["event_type"] == "planned"
    assert event_list["events"]


def test_inspect_agent_decisions_support_inclusive_ranges_and_clear_unknown_ids(
    tmp_path: Path, capsys
) -> None:
    run_directory, _ = make_run(tmp_path / "run")

    assert (
        main(["inspect", "--run", str(run_directory), "--agent", "A001", "--range", "0", "0"]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert [record["agent_id"] for record in result["decisions"]] == ["A001"]
    assert result["decisions"][0]["candidates"]

    assert (
        main(["inspect", "--run", str(run_directory), "--agent", "A001", "--range", "1", "20"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["decisions"] == []

    with pytest.raises(SystemExit) as raised:
        main(["inspect", "--run", str(run_directory), "--agent", "A999"])
    assert raised.value.code == 2
    assert "does not exist" in capsys.readouterr().err


def test_inspect_rejects_invalid_range_bounds(tmp_path: Path, capsys) -> None:
    run_directory, _ = make_run(tmp_path / "run")

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "inspect",
                "--run",
                str(run_directory),
                "--agent",
                "A001",
                "--range",
                "5",
                "2",
            ]
        )

    assert raised.value.code == 2
    assert "cannot be after" in capsys.readouterr().err


def test_inspect_event_returns_planned_provenance_and_realized_outcome(
    tmp_path: Path, capsys
) -> None:
    run_directory, event_id = make_run(tmp_path / "run")

    assert main(["inspect", "--run", str(run_directory), "--event", event_id]) == 0
    event = json.loads(capsys.readouterr().out)

    assert event["event_id"] == event_id
    assert len(event["planned"]) == 1
    assert len(event["realized"]) == 1
    assert event["realized"][0]["status"] == "failed"
    assert "no handler" in event["realized"][0]["detail"]


def test_inspect_rejects_an_unknown_event(tmp_path: Path, capsys) -> None:
    run_directory, _ = make_run(tmp_path / "run")

    with pytest.raises(SystemExit) as raised:
        main(["inspect", "--run", str(run_directory), "--event", "missing-event"])

    assert raised.value.code == 2
    assert "does not exist" in capsys.readouterr().err


def test_inspect_causal_trace_by_event_connects_resource_reference(tmp_path: Path, capsys) -> None:
    run_directory, event_id = make_run(tmp_path / "causal-run")

    assert (
        main(
            [
                "inspect",
                "--run",
                str(run_directory),
                "--cause",
                "--focus-event",
                event_id,
            ]
        )
        == 0
    )

    trace = json.loads(capsys.readouterr().out)
    node_ids = {node["id"] for node in trace["nodes"]}
    edge_kinds = {edge["relationship"] for edge in trace["edges"]}
    assert f"event:{event_id}" in node_ids
    assert "facility:toilet_1" in node_ids
    assert "resource" in edge_kinds
