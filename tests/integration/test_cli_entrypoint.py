"""Offline smoke tests for the package and initial CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import sheltersim
from sheltersim.__main__ import main

INITIAL_PROFILES = Path(__file__).parents[1] / "fixtures" / "agents_initial.json"


def test_package_has_version() -> None:
    assert sheltersim.__version__ == "0.1.0"


def test_cli_without_command_displays_help(capsys) -> None:
    assert main([]) == 0
    assert "usage: sheltersim" in capsys.readouterr().out


def test_module_entrypoint_shows_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sheltersim", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "sheltersim 0.1.0"


def test_profiles_preview_cli_displays_all_generated_agents(capsys) -> None:
    assert main(["profiles-preview", str(INITIAL_PROFILES)]) == 0

    document = json.loads(capsys.readouterr().out)
    assert len(document["profiles"]) == 100
    assert document["manifest"]["seed"] == 2026
