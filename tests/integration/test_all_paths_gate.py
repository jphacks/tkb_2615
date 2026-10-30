"""The all-paths gate (benchmarks/gate_all_paths.py): its command, determinism check and
expected hashes.

The rule for what the gate covers and when its hashes may change is in docs/testing.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support.paths import BENCHMARKS, GATE_ALL_PATHS

GATE = BENCHMARKS / "gate_all_paths.py"
# Starts and ends quickly, on Scenarios small enough for two runs of each variant.
SMALL = "branch_commands"


def _gate(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), *map(str, arguments)],
        env={**os.environ, "SHELTERSIM_LLM_MODE": "offline"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_a_scenario_matches_its_expected_hashes_and_writes_hashes_and_timing(tmp_path: Path):
    done = _gate("--output", tmp_path / "run", "--only", SMALL, "--expect", GATE_ALL_PATHS)

    assert done.returncode == 0, done.stdout + done.stderr
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["problems"] == []
    scenario = summary["scenarios"][SMALL]
    assert scenario["wall_seconds"] >= 0
    for hashes in scenario["runs"].values():
        assert set(hashes) == {"world_hash", "events_hash", "metrics_hash"}


def test_a_changed_expected_hash_fails_and_names_the_run(tmp_path: Path):
    expected = json.loads(GATE_ALL_PATHS.read_text(encoding="utf-8"))
    run = sorted(expected["scenarios"][SMALL]["runs"])[0]
    expected["scenarios"][SMALL]["runs"][run]["events_hash"] = "0" * 64
    changed = tmp_path / "expected.json"
    changed.write_text(json.dumps(expected), encoding="utf-8")

    done = _gate("--output", tmp_path / "run", "--only", SMALL, "--expect", changed)

    assert done.returncode == 1
    assert f"expected {SMALL}/{run}: events_hash" in done.stdout


def test_the_determinism_check_runs_each_variant_in_a_process_of_its_own(tmp_path: Path):
    done = _gate("--output", tmp_path / "run", "--only", SMALL, "--determinism")

    assert done.returncode == 0, done.stdout + done.stderr
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    # This scenario saves nothing, so the worker counts of the saved-record parser do not apply.
    assert summary["determinism"][SMALL] == {
        "second_process": "same",
        "hash_seed": "same",
        "python_kernels": "same",
    }


def test_the_expected_hashes_list_every_scenario_and_every_selectable_model(tmp_path: Path):
    """A new model or scenario has to enter the gate."""
    listing = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json; sys.path.insert(0, sys.argv[1]);"
            "import gate_scenarios as g;"
            "print(json.dumps([sorted(g.all_scenarios()), g.selectable_models()]))",
            str(BENCHMARKS),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    names, models = json.loads(listing.stdout)
    expected = set(json.loads(GATE_ALL_PATHS.read_text(encoding="utf-8"))["scenarios"])

    assert set(names) == expected
    assert {f"model__{domain}__{model_id}" for domain, model_id in models} <= expected


@pytest.mark.long_simulation
def test_every_scenario_of_the_gate_matches_its_expected_hashes(tmp_path: Path):
    done = _gate("--output", tmp_path / "run", "--expect", GATE_ALL_PATHS)

    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
