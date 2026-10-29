"""The project screen (project.js) offers a clarification's choices as buttons and
waits for a fork created in the background."""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).parents[3] / "src/sheltersim/web/static"


def test_clarification_choices_are_buttons_that_preview_again() -> None:
    """A choice adds a zone_id line and previews again, as /demo-v2 does (#35)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "project.js").read_text(encoding="utf-8")
    assert '<div id="ws-intervention-choices"' in html
    assert "renderInterventionChoices(result);" in script
    assert 'result?.status !== "clarification_required"' in script
    assert "`zone_id: ${choice}`" in script
    assert 'el("intervention-preview").click();' in script


def test_a_background_fork_is_awaited_until_the_child_exists() -> None:
    """The server returns a fork at once and creates it in a worker process (#48)."""
    script = (STATIC / "project.js").read_text(encoding="utf-8")
    assert 'if(result.status==="running"){view.pendingFork=child;' in script
    assert "data.last_fork?.error" in script


def test_buttons_and_labels_follow_the_selected_branch_status() -> None:
    """Branches of a Run compute at once, so the screen reads each branch's status, and
    a Run without branch statuses still uses its own (#48)."""
    script = (STATIC / "project.js").read_text(encoding="utf-8")
    assert "function branchState(data, branch)" in script
    assert "data?.branches.some(b => b.status) ? null : data?.status" in script
    assert 'const calculationActive = selectedState === "running";' in script
    assert 'el("compute").disabled = calculationActive;' in script
    assert '" · 計算中"' in script
    assert "`${runRoot()}/cancel`,{branch_id:view.branch}" in script
