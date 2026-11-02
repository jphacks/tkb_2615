"""The replay player of demo-v2 plays the saved points of a Run of an older model.

Such a Run's frames show the saved point before a minute (minute is the saved one) and
name the minute they answer in saved_point_rounding (core/model_version.py); any other
frame must still be of the minute it was read for.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_rounded_frames_answer_their_requested_minute_and_others_must_match():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the replay player check")
    static = Path("src/sheltersim/web/static").resolve()
    script = r"""
const assert = require("node:assert/strict"), fs = require("node:fs"),
  vm = require("node:vm"), path = require("node:path");
const context = {window: {}, performance: {now: () => 0},
  requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}};
for (const file of ["clock", "replay"])
  vm.runInNewContext(fs.readFileSync(path.join(process.argv[1], `demo-v2-${file}.js`),
    "utf8"), context);
const frame = (minute, requested) => ({minute, computed_until: 60, agents: [],
  ...(requested === undefined ? {} : {saved_point_rounding: {requested_minute: requested,
    saved_minute: minute}})});
async function fill(range) {
  const errors = [];
  const player = new context.window.HinanjoReplayPlayer({minute: 30, hi: 33, rate: 60,
    payload: frame(30), mode: () => "physical", fetchRange: async (s, e) => range(s, e),
    onFrame: () => {}, onDraw: () => {}, onState: () => {}, onStats: () => {},
    onError: (e) => errors.push(e.message)});
  player.clock.running = true;
  await player.fill(player.version);
  return {head: player.head, errors};
}
(async () => {
  const rounded = await fill((s, e) =>
    Array.from({length: e - s + 1}, (_, i) => frame(30, s + i)));
  assert.deepEqual(rounded, {head: 33, errors: []});
  const wrong = await fill((s, e) => Array.from({length: e - s + 1}, () => frame(30)));
  assert.equal(wrong.head, 30);
  assert.equal(wrong.errors.length, 1);
})().catch((e) => { console.error(e); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", script, str(static)], check=True)
