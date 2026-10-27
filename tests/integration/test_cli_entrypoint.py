"""The command line entry: help without a command, --version through python -m, and the
profile preview command.

The package version itself is checked in tests/unit/cli/test_main.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sheltersim.__main__ import main

INITIAL_PROFILES = Path(__file__).parents[2] / "fixtures" / "agents_initial.json"


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
