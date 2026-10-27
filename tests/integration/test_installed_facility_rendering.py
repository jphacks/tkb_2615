"""A toilet added by an intervention reaches the timeline frame and the demo-v2 map."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sheltersim.contracts.extensions import Point2D, point_in_polygon
from sheltersim.events.effects import EffectProgram, SupportMode, TargetSelector, TimedAction
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.storage.projects import digest

_STATIC = Path(__file__).parents[2] / "src" / "sheltersim" / "web" / "static"
_FORK, _SAVED, _UNSAVED = 30, 60, 45
_NODE = shutil.which("node")


def _add_two_toilets() -> EffectProgram:
    return EffectProgram(
        program_id="program-rendered-toilets",
        narrative="Add temporary toilets",
        source_text="仮設トイレを2基追加",
        simulation_start_minute=_FORK,
        effects=[
            TimedAction(
                effect_id="add-toilets",
                op="add_toilet",
                target_selector=TargetSelector(kind="zone", target_id="toilet"),
                start_minute=_FORK,
                parameters={"count": 2},
                units="toilet_stall",
                source_id="TEST-RENDERED-FACILITY",
                assumption_ids=[],
                policy_dependencies=["facility_capacity"],
                reversible=False,
            )
        ],
        assumptions=[],
        support_mode=SupportMode.NATIVE,
        compiler_provenance={"compiler_kind": "offline_rule", "compiler_version": "test-v1"},
    )


@pytest.fixture(scope="module")
def frames(
    tmp_path_factory: pytest.TempPathFactory, generated_scenario_preview: dict, saved_timeline
) -> dict[str, dict]:
    """Frames served by the timeline route, as the browser receives them."""
    scenario = ScenarioSpec.model_validate(copy.deepcopy(generated_scenario_preview["scenario"]))
    _, timeline = saved_timeline(tmp_path_factory.mktemp("rendering"), scenario)
    timeline.advance("main", _SAVED)
    program = _add_two_toilets()
    timeline.fork(
        "main",
        _FORK,
        "child",
        program=program,
        approved_program_hash=digest(program.model_dump(mode="json")),
    )
    timeline.advance("child", _SAVED)
    return {
        "main": timeline.frame("main", _SAVED),
        # A saved boundary is served from the writer's frame cache; another minute
        # is reconstructed from the checkpoint before it.
        "child_saved": timeline.frame("child", _SAVED),
        "child_unsaved": timeline.frame("child", _UNSAVED),
    }


def _toilets(frame: dict) -> dict[str, dict]:
    return {row["id"]: row for row in frame["facilities"] if row["kind"] == "toilet"}


def _added(frame: dict) -> set[str]:
    return {key for key in _toilets(frame) if key.startswith("effect-toilet-")}


@pytest.mark.parametrize("name", ["child_saved", "child_unsaved"])
def test_frame_carries_installed_toilets_with_floor_plan_points(frames, name) -> None:
    frame = frames[name]
    added = _added(frame)
    assert len(added) == 2
    zone = next(row for row in frame["geometry"]["zones"] if row["zone_id"] == "toilet")
    polygon = tuple(Point2D.model_validate(point) for point in zone["polygon"])
    points = {key: row["point"] for key, row in _toilets(frame).items()}
    for facility_id in added:
        assert points[facility_id] is not None, f"{facility_id} has no map point"
        assert point_in_polygon(Point2D.model_validate(points[facility_id]), polygon)
    assert len({(p["x"], p["y"]) for p in points.values()}) == len(points)
    assert frame["kpis"]["toilet_working_units"] == len(points)
    assert not _added(frames["main"])


_HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const [staticDir, framesPath] = process.argv.slice(1);
// Canvas stand-in: every drawing call is accepted and ignored; the renderer's
// returned hit list records what it drew where.
const sink = () => new Proxy(function () {}, {
  get: (target, key) => key === Symbol.toPrimitive ? () => 0 : key === "width" ? 0 : sink(),
  apply: () => sink(),
});
const context = (canvas) => new Proxy({ canvas, measureText: () => ({ width: 10 }) }, {
  get: (target, key) => key in target ? target[key] : () => sink(),
  set: () => true,
});
const canvas = () => {
  const element = { width: 0, height: 0 };
  element.getContext = () => context(element);
  return element;
};
globalThis.window = globalThis;
globalThis.devicePixelRatio = 1;
globalThis.document = { createElement: canvas };
for (const name of ["demo-v2-scene.js", "demo-v2-sprites.js", "demo-v2-map.js"]) {
  vm.runInThisContext(fs.readFileSync(`${staticDir}/${name}`, "utf8"), { filename: name });
}
const frames = JSON.parse(fs.readFileSync(framesPath, "utf8"));
const result = {};
for (const [name, frame] of Object.entries(frames)) {
  for (const mode of ["physical", "schematic"]) {
    const target = canvas();
    const hits = window.HinanjoMap.draw(
      { ctx: target.getContext("2d"), w: 900, h: 600 }, frame, { mode },
    );
    result[`${name}:${mode}`] = hits
      .filter((hit) => hit.type === "facility")
      .map((hit) => ({ id: hit.id, x: hit.x, y: hit.y }));
  }
}
process.stdout.write(JSON.stringify(result));
"""


def _drawn(frames: dict[str, dict], tmp_path: Path) -> dict[str, list[dict]]:
    path = tmp_path / "frames.json"
    path.write_text(json.dumps(frames), encoding="utf-8")
    completed = subprocess.run(
        [_NODE, "-e", _HARNESS, str(_STATIC), str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


@pytest.mark.skipif(_NODE is None, reason="node is required to run the map renderer")
@pytest.mark.parametrize("mode", ["physical", "schematic"])
def test_map_draws_installed_toilets_at_their_own_positions(frames, tmp_path, mode) -> None:
    drawn = _drawn(frames, tmp_path)
    for name in ("child_saved", "child_unsaved"):
        icons = {hit["id"]: (hit["x"], hit["y"]) for hit in drawn[f"{name}:{mode}"]}
        toilets = set(_toilets(frames[name]))
        assert toilets <= set(icons), f"{name} {mode}: toilets not drawn {toilets - set(icons)}"
        assert len({icons[key] for key in toilets}) == len(toilets), "toilet icons overlap"
    main = {hit["id"] for hit in drawn[f"main:{mode}"]}
    assert not {key for key in main if key.startswith("effect-toilet-")}


@pytest.mark.skipif(_NODE is None, reason="node is required to run the map renderer")
def test_physical_map_omits_a_facility_without_a_point(frames, tmp_path) -> None:
    # The failure before floor-plan placement: the schematic still shows the toilet,
    # so only the physical map reveals that it has nowhere to be.
    frame = json.loads(json.dumps(frames["child_saved"]))
    hidden = sorted(_added(frame))[0]
    for row in frame["facilities"]:
        if row["id"] == hidden:
            row["point"] = None
    # The map also falls back to the frame geometry's facility points.
    frame["geometry"]["facility_points"].pop(hidden, None)
    drawn = _drawn({"child": frame}, tmp_path)
    assert hidden not in {hit["id"] for hit in drawn["child:physical"]}
    assert hidden in {hit["id"] for hit in drawn["child:schematic"]}
