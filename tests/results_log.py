"""Keep every pytest run's results on disk so a failure can be read after the run (#136).

Each run writes ``.test-results/<UTC time>-<pid>/`` under the rootdir:

- ``results.jsonl``: one line per reported test phase (outcome, duration, worker, and the
  traceback text for failures), appended while the run goes, so a killed run keeps what it had.
- ``summary.txt``: the command, counts, every failure and error with its traceback and
  captured output, and the slowest tests, written when the session ends.

``.test-results/latest.txt`` names the newest run directory, and only the newest
``KEEP_RUNS`` directories are kept. With pytest-xdist only the controller writes; worker
reports reach it through ``pytest_runtest_logreport``. After pytest's own summary line, the
run prints one more line with the failed test names and the summary path, so reading only
the last line of the output still shows what failed.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import pytest

RESULTS_DIRECTORY = ".test-results"
KEEP_RUNS = 20
SLOWEST = 15
NAMED_FAILURES = 5
# pytest's terminal summary order, so the final line reads like pytest's own.
CATEGORY_ORDER = ("failed", "error", "passed", "skipped", "xfailed", "xpassed")


def _worker(report: pytest.TestReport | pytest.CollectReport) -> str | None:
    node = getattr(report, "node", None)
    workerinput = getattr(node, "workerinput", None)
    return workerinput.get("workerid") if workerinput else None


def _sections(report: pytest.TestReport | pytest.CollectReport) -> str:
    return "".join(f"--- {title} ---\n{content.rstrip()}\n" for title, content in report.sections)


class ResultsLog:
    """Record test reports to files and name the failures on the last output line."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.root = Path(config.rootpath) / RESULTS_DIRECTORY
        started = datetime.now(UTC)
        self.started = started
        self.started_clock = time.monotonic()
        self.run_dir = self.root / f"{started:%Y%m%dT%H%M%SZ}-{os.getpid()}"
        self.counts: Counter[str] = Counter()
        self.failures: list[dict[str, Any]] = []
        self.durations: Counter[str] = Counter()
        self.exitstatus: int | None = None
        self.stream: IO[str] | None = None

    # pytest hooks

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "latest.txt").write_text(self.run_dir.name + "\n", encoding="utf-8")
        self.stream = (self.run_dir / "results.jsonl").open("a", encoding="utf-8")
        self._prune()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.durations[report.nodeid] += report.duration
        self._record(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._record(report)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.exitstatus = int(exitstatus)
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        (self.run_dir / "summary.txt").write_text(self._summary(), encoding="utf-8")

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None and self.exitstatus is not None:
            terminal.write_line(self.final_line())

    # recording

    def _record(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        category = self.config.hook.pytest_report_teststatus(report=report, config=self.config)[0]
        if not category:
            return
        self.counts[category] += 1
        record: dict[str, Any] = {
            "nodeid": report.nodeid,
            "when": getattr(report, "when", "collect"),
            "outcome": category,
            "duration": round(getattr(report, "duration", 0.0), 3),
            "worker": _worker(report),
        }
        if category in ("failed", "error"):
            record["longrepr"] = report.longreprtext
            record["sections"] = _sections(report)
            self.failures.append(record)
        if self.stream is not None:
            self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.stream.flush()

    def _prune(self) -> None:
        runs = sorted(path for path in self.root.iterdir() if path.is_dir())
        for old in runs[:-KEEP_RUNS]:
            shutil.rmtree(old, ignore_errors=True)

    # output

    def outcome_text(self) -> str:
        names = [name for name in CATEGORY_ORDER if self.counts[name]]
        names += sorted(name for name in self.counts if name not in CATEGORY_ORDER)
        return ", ".join(f"{self.counts[name]} {name}" for name in names) or "no tests ran"

    def summary_path(self) -> str:
        path = self.run_dir / "summary.txt"
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return str(path)

    def final_line(self) -> str:
        line = f"test results: {self.outcome_text()}"
        if self.failures:
            names = [item["nodeid"] for item in self.failures[:NAMED_FAILURES]]
            more = len(self.failures) - len(names)
            line += "; failed: " + ", ".join(names) + (f" (+{more} more)" if more else "")
            return line + f"; tracebacks in {self.summary_path()}"
        return line + f"; details in {self.summary_path()}"

    def _summary(self) -> str:
        elapsed = time.monotonic() - self.started_clock
        args = " ".join(self.config.invocation_params.args)
        lines = [
            f"pytest run {self.run_dir.name}",
            f"command: pytest {args}".rstrip(),
            f"started: {self.started.isoformat(timespec='seconds')}",
            f"duration: {elapsed:.1f}s",
            f"exit status: {self.exitstatus}",
            f"result: {self.outcome_text()}",
            "",
        ]
        for item in self.failures:
            worker = f", {item['worker']}" if item["worker"] else ""
            lines.append(f"{item['outcome'].upper()} {item['nodeid']} ({item['when']}{worker})")
            lines.append(item["longrepr"].rstrip())
            if item["sections"]:
                lines.append(item["sections"].rstrip())
            lines.append("")
        if self.durations:
            lines.append(f"slowest {SLOWEST} tests (setup + call + teardown):")
            for nodeid, seconds in self.durations.most_common(SLOWEST):
                lines.append(f"  {seconds:8.2f}s  {nodeid}")
        return "\n".join(lines) + "\n"


def pytest_configure(config: pytest.Config) -> None:
    # Under pytest-xdist the controller gets every worker's reports; workers write nothing.
    if hasattr(config, "workerinput") or config.option.collectonly:
        return
    config.pluginmanager.register(ResultsLog(config), "sheltersim_results_log")
