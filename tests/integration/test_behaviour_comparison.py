"""benchmarks/compare_behaviour.py collects multi-seed Runs and compares two sets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from support.paths import BENCHMARKS

COMMAND = BENCHMARKS / "compare_behaviour.py"


def _run(*arguments: object) -> str:
    done = subprocess.run(
        [sys.executable, str(COMMAND), *map(str, arguments)],
        env={**os.environ, "SHELTERSIM_LLM_MODE": "offline"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return done.stdout


def _collect(directory: Path, first_seed: int) -> None:
    _run(
        "--collect",
        directory,
        "--seeds",
        2,
        "--first-seed",
        first_seed,
        "--minutes",
        30,
        "--evacuees",
        6,
        "--jobs",
        2,
    )


def test_same_seeds_of_the_same_code_report_no_change(tmp_path: Path) -> None:
    _collect(tmp_path / "a", 11)
    _collect(tmp_path / "b", 11)

    report = _run(tmp_path / "a", tmp_path / "b")

    assert "同じseedどうし" in report
    assert "平均ストレス" in report
    assert "振る舞いは変わっていません" in report
    assert "注意" not in report


def test_other_seeds_are_compared_as_two_groups(tmp_path: Path) -> None:
    _collect(tmp_path / "a", 11)
    _collect(tmp_path / "b", 21)

    report = _run(tmp_path / "a", tmp_path / "b", "--output", tmp_path / "report.md")

    assert "2つの集団" in report
    assert "振る舞いは変わっていません" not in report
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == report
