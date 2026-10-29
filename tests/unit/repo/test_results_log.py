"""Every pytest run leaves its failures on disk and on the last output line (#136)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).parents[2]

SAMPLE = """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("fixture could not start")

def test_passes():
    pass

def test_fails():
    print("state before the assert")
    assert 1 + 1 == 3

def test_errors_in_setup(broken):
    pass

@pytest.mark.skip(reason="not here")
def test_skipped():
    pass
"""


def _run(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONPATH": str(TESTS)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "results_log", "-p", "no:cacheprovider", *args],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


def _latest(project: Path) -> Path:
    results = project / ".test-results"
    return results / (results / "latest.txt").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("parallel", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_a_run_keeps_failures_with_tracebacks_after_it_ends(
    project: Path, parallel: tuple[str, ...]
) -> None:
    completed = _run(project, *parallel)

    run_dir = _latest(project)
    assert [path.name for path in (project / ".test-results").iterdir() if path.is_dir()] == [
        run_dir.name
    ]
    summary = (run_dir / "summary.txt").read_text(encoding="utf-8")
    assert "result: 1 failed, 1 error, 1 passed, 1 skipped" in summary
    assert "FAILED test_sample.py::test_fails (call" in summary
    assert "assert (1 + 1) == 3" in summary
    assert "state before the assert" in summary
    assert "ERROR test_sample.py::test_errors_in_setup (setup" in summary
    assert "fixture could not start" in summary

    records = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failed = [item for item in records if item["outcome"] == "failed"]
    assert [item["nodeid"] for item in failed] == ["test_sample.py::test_fails"]
    assert "assert (1 + 1) == 3" in failed[0]["longrepr"]
    assert {item["outcome"] for item in records} == {"failed", "error", "passed", "skipped"}
    if parallel:
        assert all(item["worker"].startswith("gw") for item in records)

    # Failures are named in the order they were reported, which xdist does not fix.
    counts, named, location = completed.stdout.strip().splitlines()[-1].split("; ")
    assert counts == "test results: 1 failed, 1 error, 1 passed, 1 skipped"
    assert set(named.removeprefix("failed: ").split(", ")) == {
        "test_sample.py::test_fails",
        "test_sample.py::test_errors_in_setup",
    }
    assert location == f"tracebacks in {Path('.test-results') / run_dir.name / 'summary.txt'}"


def test_a_passing_run_names_its_summary_on_the_last_line(project: Path) -> None:
    completed = _run(project, "-k", "passes")

    run_dir = _latest(project)
    assert completed.returncode == 0
    assert completed.stdout.strip().splitlines()[-1] == (
        f"test results: 1 passed; details in {Path('.test-results') / run_dir.name / 'summary.txt'}"
    )
    assert "test_sample.py::test_passes" in (run_dir / "summary.txt").read_text(encoding="utf-8")


def test_a_collection_error_is_kept(project: Path) -> None:
    (project / "test_broken.py").write_text(
        "import missing_module_for_results_log\n", encoding="utf-8"
    )

    completed = _run(project, "test_broken.py")

    summary = (_latest(project) / "summary.txt").read_text(encoding="utf-8")
    assert "ERROR test_broken.py (collect)" in summary
    assert "missing_module_for_results_log" in summary
    assert "failed: test_broken.py;" in completed.stdout.strip().splitlines()[-1]


def test_only_the_newest_runs_are_kept(project: Path) -> None:
    results = project / ".test-results"
    for index in range(25):
        (results / f"20000101T0000{index:02d}Z-1").mkdir(parents=True)

    _run(project, "-k", "passes")

    kept = sorted(path.name for path in results.iterdir() if path.is_dir())
    assert len(kept) == 20
    assert kept[-1] == _latest(project).name
    assert kept[0] == "20000101T000006Z-1"


def test_collect_only_writes_nothing(project: Path) -> None:
    _run(project, "--collect-only", "-q")

    assert not (project / ".test-results").exists()
