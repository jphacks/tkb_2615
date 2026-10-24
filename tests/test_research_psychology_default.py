"""Generated Scenarios select the research psychology model; legacy models stay replayable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim.__main__ import main
from sheltersim.contracts.policies import (
    LEGACY_PSYCHOLOGY_MODELS,
    legacy_psychology_notice,
    selected_models,
)
from sheltersim.core.checkpoint import Checkpoint
from sheltersim.core.world import WorldState
from sheltersim.domain.psychological_state import INTEGRATED_PSYCHOLOGY_POLICY_VERSION
from sheltersim.domain.simulator_state import initialize_simulator_extensions
from sheltersim.experiments.comparison import compare_run_sets, render_comparison_markdown
from sheltersim.experiments.initial_scenario import (
    InitialScenarioBuilder,
    InitialScenarioRequest,
)
from sheltersim.experiments.runner import run_scenario
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.policies.psychology import (
    IntegratedDistressLifeStatePolicy,
    LifeStatePsychologyPolicy,
    PsychologyParameters,
)
from sheltersim.policies.registry import create_psychology

ROOT = Path(__file__).parents[1]
SMALL_FIXTURE = ROOT / "fixtures" / "scenarios" / "h00_small_replay.json"


def _generated(duration_days: float = 1) -> ScenarioSpec:
    return InitialScenarioBuilder().build(InitialScenarioRequest(
        municipality="Synthetic municipality", disaster="earthquake",
        evacuees=10, duration_days=duration_days, seed=7,
    )).scenario


def test_generated_scenarios_select_the_research_psychology_model() -> None:
    scenario = _generated()
    assert selected_models(scenario.model_configs)["psychology"] == (
        INTEGRATED_PSYCHOLOGY_POLICY_VERSION
    )
    assert isinstance(create_psychology(scenario.model_configs), IntegratedDistressLifeStatePolicy)
    # Every mean-reversion coefficient is explicit in the Scenario, not an implicit default.
    defaults = PsychologyParameters()
    psychology = scenario.model_configs["psychology"]
    for name in (
        "natural_recovery_rate_per_minute",
        "chronic_retention",
        "chronic_acute_weight",
        "chronic_high_distress_weight",
        "high_distress_threshold",
    ):
        assert psychology[name] == getattr(defaults, name)


def test_generated_psychology_selection_is_recorded_in_provenance_and_manifest() -> None:
    scenario = _generated()
    source_id = IntegratedDistressLifeStatePolicy.source_id
    assert any(row["id"] == source_id for row in scenario.provenance["sources"])
    assumption = next(
        row for row in scenario.provenance["assumptions"] if row["subject"] == "psychology_model"
    )
    assert assumption["source_id"] == source_id
    assert assumption["value"] == {"policy_version": INTEGRATED_PSYCHOLOGY_POLICY_VERSION}
    manifest = json.loads((ROOT / "fixtures" / "source-manifest.json").read_text("utf-8"))
    assert any(row["id"] == source_id for row in manifest["assumptions"])


def test_generated_research_state_restores_from_a_checkpoint() -> None:
    world = WorldState.from_scenario(_generated())
    initialize_simulator_extensions(world)
    restored = Checkpoint.capture(world).restore_world()
    assert restored.canonical_json() == world.canonical_json()


def test_generated_research_scenario_runs_and_records_its_model(tmp_path) -> None:
    run_scenario(_generated(duration_days=0.25), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert manifest["policy_implementations"]["psychology"] == (
        INTEGRATED_PSYCHOLOGY_POLICY_VERSION
    )


def test_saved_scenarios_keep_their_legacy_psychology_selection() -> None:
    document = _generated().model_dump(mode="json")
    document["model_configs"]["policy_registry"]["schema_version"] = 2
    document["model_configs"]["policy_registry"].pop("suite_id")
    document["model_configs"]["policy_registry"]["psychology"] = "life-state-feedback-v1"
    legacy = ScenarioSpec.model_validate(document)
    assert type(create_psychology(legacy.model_configs)) is LifeStatePsychologyPolicy
    # Inputs without a registry still resolve to the unchanged implicit default.
    unregistered = ScenarioSpec.load(SMALL_FIXTURE)
    assert selected_models(unregistered.model_configs)["psychology"] == "causal-stress-v1"


@pytest.mark.parametrize("model_id", sorted(LEGACY_PSYCHOLOGY_MODELS))
def test_legacy_psychology_models_have_a_notice(model_id: str) -> None:
    notice = legacy_psychology_notice(model_id)
    assert notice is not None
    assert model_id in notice
    assert INTEGRATED_PSYCHOLOGY_POLICY_VERSION in notice


@pytest.mark.parametrize(
    "model_id", [INTEGRATED_PSYCHOLOGY_POLICY_VERSION, "mean-reverting-distress-v1", None]
)
def test_research_or_unknown_models_have_no_notice(model_id) -> None:
    assert legacy_psychology_notice(model_id) is None


def test_cli_reports_and_warns_about_a_legacy_psychology_model(tmp_path, capsys) -> None:
    legacy_document = ScenarioSpec.load(SMALL_FIXTURE).model_dump(mode="json")
    legacy_document["model_configs"]["policy_registry"] = {
        "schema_version": 2,
        "psychology": "causal-stress-v1",
    }
    legacy_scenario = tmp_path / "legacy.json"
    legacy_scenario.write_text(json.dumps(legacy_document), encoding="utf-8")
    frozen = tmp_path / "frozen.json"
    assert main(["prepare", "--scenario", str(legacy_scenario), "--freeze",
                 "--output", str(frozen)]) == 0
    assert "warning: psychology model causal-stress-v1 is a legacy model" in (
        capsys.readouterr().err
    )
    run_directory = tmp_path / "run"
    assert main(["run", "--offline", "--scenario", str(frozen),
                 "--output", str(run_directory)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["psychology_model"] == "causal-stress-v1"
    assert "warning: psychology model causal-stress-v1 is a legacy model" in captured.err

    report = compare_run_sets([run_directory], [run_directory])
    assert report["psychology_models"] == {
        "baseline": "causal-stress-v1", "candidate": "causal-stress-v1",
    }
    markdown = render_comparison_markdown(report)
    assert "- Psychology model: baseline `causal-stress-v1`, candidate `causal-stress-v1`" in (
        markdown
    )
    assert markdown.count("- Warning: psychology model causal-stress-v1") == 1


def test_comparison_markdown_has_no_warning_for_the_research_model() -> None:
    markdown = render_comparison_markdown({
        "psychology_models": {
            "baseline": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
            "candidate": INTEGRATED_PSYCHOLOGY_POLICY_VERSION,
        },
        "final_metric_differences": {},
    })
    assert f"baseline `{INTEGRATED_PSYCHOLOGY_POLICY_VERSION}`" in markdown
    assert "Warning" not in markdown
