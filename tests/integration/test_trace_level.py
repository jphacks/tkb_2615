"""The trace level changes how much a Run writes, not the simulation state hash or the
metrics and events a Run saves.

On a generated Scenario run with run_scenario, so the decisions come from the
controller a Run uses, with people walking on the floor plan. Until #137 the first
test called the behavior policy by hand on scenario_100_agents and the second ran
h00_small_replay; neither has a floor plan or facility awareness.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sheltersim.core.engine import TraceLevel
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec

# 90 minutes: every person decides several times, and three runs stay short.
DURATION_DAYS = 0.0625


@pytest.fixture(scope="module")
def runs(tmp_path_factory, generated_previews) -> dict[TraceLevel, tuple[Path, str]]:
    """The same generated Scenario run at each trace level: output folder and final state."""
    preview = generated_previews(duration_days=DURATION_DAYS)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    root = tmp_path_factory.mktemp("trace-level")
    return {
        level: (
            root / level.value,
            run_scenario(scenario, root / level.value, trace_level=level).canonical_json(),
        )
        for level in TraceLevel
    }


def _decisions(directory: Path) -> list[dict]:
    lines = (directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


def test_trace_level_changes_artifact_detail_not_simulation_hash(runs) -> None:
    off, summary, full = (runs[level][0] for level in TraceLevel)

    assert runs[TraceLevel.OFF][1] == runs[TraceLevel.SUMMARY][1] == runs[TraceLevel.FULL][1]
    assert _decisions(off) == []
    assert _decisions(summary)
    assert len(_decisions(summary)) == len(_decisions(full))
    assert all("candidates" not in decision for decision in _decisions(summary))
    assert all("candidates" in decision for decision in _decisions(full))


def test_same_seed_has_identical_canonical_outputs_at_every_trace_level(runs) -> None:
    directories = [runs[level][0] for level in TraceLevel]

    for filename in ("metrics.csv", "operational_kpis.csv", "events_realized.jsonl"):
        contents = {(directory / filename).read_bytes() for directory in directories}
        assert len(contents) == 1, filename
    manifests = [_manifest(directory) for directory in directories]
    assert len({manifest["final_state_hash"] for manifest in manifests}) == 1
    assert len({manifest["scenario_hash"] for manifest in manifests}) == 1
