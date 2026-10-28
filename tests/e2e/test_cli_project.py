"""Working on a durable Project from the CLI (sheltersim project ...): prepare and approve
a Scenario, compute and fork it, read its frame, history and usage, rerun, archive and
delete, as an operator without the browser would.

The regional research that prepare runs answers from the fake public services of
tests/support/public_data.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sheltersim import cli


class ProjectCli:
    """Run sheltersim project commands on one store and return their JSON output."""

    def __init__(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.root, self.capsys = root, capsys

    def __call__(self, action: str, *arguments: str) -> dict:
        assert cli.main(["project", "--root", str(self.root), action, *arguments]) == 0
        return json.loads(self.capsys.readouterr().out)

    def refused(self, action: str, *arguments: str) -> str:
        with pytest.raises(SystemExit) as stopped:
            cli.main(["project", "--root", str(self.root), action, *arguments])
        assert stopped.value.code == 2
        return self.capsys.readouterr().err


def test_a_project_is_prepared_computed_forked_and_cleaned_up_from_the_cli(
    tmp_path, capsys, monkeypatch, public_data, generated_request
):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    project = ProjectCli(tmp_path / "projects", capsys)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"request": generated_request()}, ensure_ascii=False))
    history_query = tmp_path / "history.json"
    history_query.write_text(json.dumps({"kind": "kpis", "start": 0, "end": 30, "limit": 100}))
    p = ("--project-id", "p")

    project("create", *p)
    draft = project("prepare", *p, "--text", "", "--input-json", str(request))
    assert draft["can_start"], draft["approval_blockers"]
    approved = project(
        "approve-draft",
        *p,
        "--run-id",
        "r",
        "--preview-id",
        draft["preview_id"],
        "--approved-hash",
        draft["approved_hash"],
    )
    assert approved["scenario_hash"] == draft["approved_hash"]

    computed = project("resume", *p, "--run-id", "r", "--minute", "30")
    frame = project("frame", *p, "--run-id", "r", "--minute", "20")
    forked = project("fork", *p, "--run-id", "r", "--minute", "20", "--child-id", "later")
    child = project("resume", *p, "--run-id", "r", "--branch-id", "later", "--minute", "40")
    history = project("history", *p, "--run-id", "r", "--input-json", str(history_query))
    usage = project("usage", *p, "--run-id", "r")

    assert (computed["minute"], computed["branch_id"]) == (30, "main")
    assert len(frame["agents"]) == len(draft["scenario"]["profiles"])
    assert (forked["branch_id"], forked["minute"]) == ("later", 20)
    assert child["minute"] == 40
    assert [row["minute_index"] for row in history["items"]] == list(range(30))
    assert usage["estimated_cost_usd"] == 0
    branches = {
        branch["branch_id"]: (branch["fork_minute"], branch["computed_until"])
        for branch in project("show", *p)["runs"][0]["branches"]
    }
    assert branches == {"main": (0, 30), "later": (20, 40)}

    rerun = project("rerun", *p, "--run-id", "r", "--new-run-id", "again")
    assert rerun["scenario_hash"] == approved["scenario_hash"]
    project("archive", *p, "--run-id", "again")
    runs = {run["run_id"]: run["archived"] for run in project("show", *p)["runs"]}
    assert runs == {"r": False, "again": True}
    error = project.refused("delete", *p, "--run-id", "again", "--confirmation", "r")
    assert "run ID confirmation" in error
    assert project("delete", *p, "--run-id", "again", "--confirmation", "again") == {
        "deleted": "again"
    }
    assert [run["run_id"] for run in project("show", *p)["runs"]] == ["r"]
    assert [item["project_id"] for item in project("list")["projects"]] == ["p"]
