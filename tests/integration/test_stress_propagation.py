"""Contact-based stress propagation runs in a full Run, survives a Checkpoint and
writes a compact contact trace only when tracing is on.

The policy itself is checked in tests/unit/policies/test_social.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

FIXTURE = Path(__file__).parents[2] / "fixtures" / "scenario_100_agents.json"


def test_enabled_policy_is_checkpointable_and_writes_compact_contact_trace(
    tmp_path: Path,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
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
    manifest = json.loads((tmp_path / "social-run" / "manifest.json").read_text())
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
