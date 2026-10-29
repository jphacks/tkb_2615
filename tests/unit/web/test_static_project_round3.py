"""The Project effect explanation shows why a natural-language preview did not use Opus."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.natural_language import compile_natural_language
from sheltersim.llm.provider import LLMRouter
from sheltersim.llm.schemas import FacilityTarget, ParseRequest, ScenarioSummary, ZoneTarget

_SCRIPT = Path(__file__).parents[3] / "src" / "sheltersim" / "web" / "static" / "project-round3.js"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is required to run the browser view")


def _render(result: dict[str, object]) -> list[list[str | None]]:
    """Run only renderEffectExplanation with stub DOM helpers; return (tag, text, class)."""
    source = _SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"  function renderEffectExplanation\(detail\)\{.*?\n  \}\n", source, re.S)
    assert match is not None
    detail = json.dumps({"result": result, "run": "run", "branch": "main", "minute": 60})
    harness = (
        "const lines=[];const box={replaceChildren(){}};const $=()=>box;"
        "const state={modelContracts:null};"
        "const text=(parent,tag,value,className)=>"
        "{lines.push([tag,String(value),className||null]);};"
        + match.group(0)
        + f"renderEffectExplanation({detail});process.stdout.write(JSON.stringify(lines));"
    )
    completed = subprocess.run(
        [_NODE, "-e", harness],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def _offline_preview() -> dict[str, object]:
    summary = ScenarioSummary(
        duration_minutes=14_400,
        facilities=[
            FacilityTarget(
                id="toilet-1",
                name="North restroom",
                kind="toilet",
                zone_id="main",
                capacity=4,
                working_units=4,
            )
        ],
        zones=[ZoneTarget(id="main", name="Main hall")],
        available_policies=["facility_capacity"],
    )
    request = ParseRequest(
        text="Day 3 toilet 1 outage",
        branch_id="main",
        selected_minute=0,
        scenario_summary=summary,
    )
    result = compile_natural_language(request, LLMRouter(LLMSettings(mode=LLMMode.OFFLINE)))
    return result.model_dump(mode="json")


def test_rule_fallback_preview_shows_the_reason_opus_was_not_used() -> None:
    preview = _offline_preview()
    assert preview["fallback_used"] is True

    lines = _render(preview)

    assert ["h4", "制限・AIを使わなかった理由", None] in lines
    paragraphs = [line[1] for line in lines if line[0] == "p"]
    assert any("規則ベースの処理で作成した結果" in item for item in paragraphs)
    assert any("（理由コード: offline）" in item for item in paragraphs)
    for limitation in preview["limitations"]:
        assert limitation in paragraphs


def test_opus_preview_without_limitations_shows_no_fallback_notice() -> None:
    preview = {**_offline_preview(), "fallback_used": False, "limitations": []}

    lines = _render(preview)

    assert not any(line[1].startswith("制限") for line in lines if line[0] == "h4")
    assert not any("規則ベースの処理" in line[1] for line in lines)
