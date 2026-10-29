"""Contact-based stress propagation runs in a full Run, survives a Checkpoint and
writes a compact contact trace only when tracing is on.

The first test keeps the earlier sparse policy (WorldState schema 5) on
scenario_100_agents; the second runs the shared generated Scenario, whose social
policy (expressiveness-channel-openness-v1) is the one an operator's Scenario uses
(#137).

The policy itself is checked in tests/unit/policies/test_social.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from support.paths import SCENARIO_100_AGENTS

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec


def test_enabled_policy_is_checkpointable_and_writes_compact_contact_trace(
    tmp_path: Path,
) -> None:
    document = json.loads(SCENARIO_100_AGENTS.read_text(encoding="utf-8"))
    document["duration_minutes"] = 3
    document["scheduled_events"] = []
    document["profiles"] = document["profiles"][:2]
    document["model_configs"]["social_propagation"] = {
        "enabled": True,
        "max_contacts_per_agent": 1,
        "minimum_contact_minutes": 1,
        "stress_spread_per_minute": 0.01,
        "natural_recovery_per_minute": 0.001,
        "cooldown_minutes": 5,
    }
    scenario = ScenarioSpec.model_validate(document)

    world = run_scenario(scenario, tmp_path / "social-run")
    untraced_world = run_scenario(
        scenario,
        tmp_path / "social-run-off-trace",
        trace_level=TraceLevel.OFF,
    )

    assert world.schema_version == 5
    assert world.canonical_json() == untraced_world.canonical_json()
    assert world.model_state["social_propagation_v1"]["schema_version"] == 1
    restored = Checkpoint.capture(world).restore_world()
    assert restored.model_state == world.model_state
    assert restored.canonical_json() == world.canonical_json()
    manifest = json.loads((tmp_path / "social-run" / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["model_assumptions"]["social_propagation"]["source_id"]
        == "ASM-SOCIAL-PROPAGATION-V1"
    )
    traces = [
        json.loads(line)
        for line in (tmp_path / "social-run" / "social_contacts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(traces) == 3
    assert all(item["selected_contact_count"] <= 2 for item in traces)
    assert all(len(item["contacts"]) <= 20 for item in traces)
    assert (tmp_path / "social-run-off-trace" / "social_contacts.jsonl").read_text(
        encoding="utf-8"
    ) == ""


# Contacts count only after minimum_contact_minutes (5) together, so the first ones
# appear at minute 4; fifteen minutes gives several minutes with contacts.
GENERATED_MINUTES = 15


def test_generated_scenario_propagation_is_checkpointable_and_writes_compact_contact_trace(
    tmp_path: Path, generated_previews
) -> None:
    document = copy.deepcopy(generated_previews()["scenario"])
    document["duration_minutes"] = GENERATED_MINUTES
    scenario = ScenarioSpec.model_validate(document)
    config = scenario.model_configs["social_propagation"]
    assert config["enabled"] is True

    world = run_scenario(scenario, tmp_path / "social-run")
    untraced_world = run_scenario(
        scenario,
        tmp_path / "social-run-off-trace",
        trace_level=TraceLevel.OFF,
    )

    assert world.canonical_json() == untraced_world.canonical_json()
    restored = Checkpoint.capture(world).restore_world()
    assert restored.model_state == world.model_state
    assert restored.canonical_json() == world.canonical_json()
    manifest = json.loads((tmp_path / "social-run" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_assumptions"]["social_propagation"]["source_id"] == config["source_id"]
    traces = [
        json.loads(line)
        for line in (tmp_path / "social-run" / "social_contacts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(traces) == GENERATED_MINUTES
    # Otherwise the checks below would pass on an empty trace.
    assert any(item["selected_contact_count"] for item in traces)
    assert all(
        item["selected_contact_count"] <= config["max_contacts_per_agent"] * len(world.agents)
        for item in traces
    )
    assert all(len(item["contacts"]) <= 20 for item in traces)
    assert (tmp_path / "social-run-off-trace" / "social_contacts.jsonl").read_text(
        encoding="utf-8"
    ) == ""
