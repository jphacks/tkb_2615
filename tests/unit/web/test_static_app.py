"""The saved-Run map note names the Agents it cannot draw instead of hiding them (#161)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[3] / "src" / "sheltersim" / "web" / "static" / "app.js"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is required to run the browser view")


def _map_note(map_data: dict[str, object]) -> str:
    """Run only mapNote from app.js on one zone_map response."""
    source = _SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^function mapNote\(map\) \{.*?^\}\n", source, re.S | re.M)
    assert match is not None
    harness = match.group(0) + f"process.stdout.write(mapNote({json.dumps(map_data)}));"
    completed = subprocess.run(
        [_NODE, "-e", harness], capture_output=True, text=True, check=True, timeout=30
    )
    return completed.stdout


def test_map_note_lists_the_zones_of_agents_it_cannot_draw() -> None:
    note = _map_note(
        {
            "layout": {"title": "合成体育館"},
            "agents": [{"id": "A003"}],
            "undrawn_agents": [
                {"id": "A001", "zone_id": "food"},
                {"id": "A002", "zone_id": "food"},
                {"id": "A004", "zone_id": "staff"},
            ],
        }
    )
    assert note.startswith("合成体育館 · 1人表示")
    assert note.endswith(" · 3人は地図にない区画（food、staff）にいるため非表示")


def test_map_note_is_unchanged_when_every_agent_is_drawn() -> None:
    note = _map_note({"layout": {"title": "Scenarioの図面"}, "agents": [{"id": "A001"}]})
    assert note == "Scenarioの図面 · 1人表示 · 座標は表示用 · 配置編集はブラウザ内のみ"
