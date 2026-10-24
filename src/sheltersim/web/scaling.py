"""Read-only spatial level of detail for large recorded frames."""

from __future__ import annotations

import math
from typing import Any


def aggregate_frame(frame: dict[str, Any], *, cell_m: float = 5.0, detail_limit: int = 200) -> dict[str, Any]:
    """Keep exact frame time; aggregate physical/display coordinate spaces separately."""
    if not math.isfinite(cell_m) or cell_m <= 0 or type(detail_limit) is not int or detail_limit < 0:
        raise ValueError("invalid map level-of-detail settings")
    people = {p["id"]: p for p in frame.get("people", [])}
    cells: dict[tuple[str, int, int], dict[str, Any]] = {}
    unavailable = 0
    for agent in frame.get("agents", []):
        point = agent.get("point")
        if point is None:
            unavailable += 1
            continue
        x, y = math.floor(point["x"] / cell_m), math.floor(point["y"] / cell_m)
        key = agent["position_kind"], x, y
        cell = cells.setdefault(key, {"position_kind": key[0], "x": x*cell_m, "y": y*cell_m, "count": 0, "stress_total": 0.0, "stress_max": 0.0})
        stress = people.get(agent["agent_id"], {}).get("stress", 0.0)
        cell["count"] += 1
        cell["stress_total"] += stress
        cell["stress_max"] = max(cell["stress_max"], stress)
    for cell in cells.values():
        cell["stress_mean"] = cell.pop("stress_total") / cell["count"]
    return {"schema_version": 1, "run_id": frame["run_id"], "branch_id": frame["branch_id"], "minute": frame["minute"], "cell_m": cell_m, "cells": [cells[key] for key in sorted(cells)], "unavailable_positions": unavailable, "agents": frame.get("agents", []) if len(frame.get("agents", [])) <= detail_limit else [], "aggregation": "spatial display; individual simulation state retained"}
