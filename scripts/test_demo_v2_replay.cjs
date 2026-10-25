/* Deterministic UI clock/buffer/door tests: no HTTP, WorldState edits, or Provider. */
const assert = require("node:assert/strict"),
  fs = require("node:fs"),
  vm = require("node:vm"),
  path = require("node:path");
let now = 0,
  nextId = 0;
const rafs = new Map();
const context = {
  window: {},
  performance: { now: () => now },
  requestAnimationFrame: (callback) => {
    const id = ++nextId;
    rafs.set(id, callback);
    return id;
  },
  cancelAnimationFrame: (id) => rafs.delete(id),
};
for (const file of ["scene", "clock", "replay"])
  vm.runInNewContext(
    fs.readFileSync(
      path.join(__dirname, `../src/sheltersim/web/static/demo-v2-${file}.js`),
      "utf8",
    ),
    context,
  );
const Clock = context.window.HinanjoReplayClock,
  Scene = context.window.HinanjoScene;
const c = new Clock(0, 60);
c.available = 100;
c.start(0);
assert.equal(c.tick(1000), 1);
c.setRate(120, 1500);
assert.equal(c.position, 1.5);
assert.equal(c.tick(2000), 2.5);
c.pause(2000);
assert.equal(c.tick(5000), 2.5);
c.start(5000);
assert.equal(c.tick(5500), 3.5);
c.available = 4;
assert.equal(c.tick(10000), 4);
c.available = 5;
assert.equal(
  c.tick(10100),
  4.2,
  "buffer waits must not produce catch-up jumps",
);
const slow = new Clock(0, 1);
slow.available = 10;
slow.start(0);
assert.equal(slow.tick(60000), 1);
for (const [a, b] of [
  [
    { x: 50, y: 30 },
    { x: 90, y: 52 },
  ],
  [
    { x: 8, y: 10 },
    { x: 90, y: 30 },
  ],
  [
    { x: 19, y: 50 },
    { x: 79, y: 50 },
  ],
]) {
  const route = Scene.diagramPath(a, b);
  assert(
    Scene.safeDiagramPath(route),
    "converted path uses doors and corridor",
  );
  assert(
    !Scene.safeDiagramPath([a, b]),
    "direct wall-crossing shortcuts must fail",
  );
}
const frame = (m) => ({
  run_id: "r",
  branch_id: "a",
  minute: m,
  computed_until: 24,
  agents: [],
  people: [],
  staff: [],
  geometry: null,
});
let pending = [],
  states = [],
  commits = [],
  draws = [];
const player = new context.window.HinanjoReplayPlayer({
  minute: 0,
  hi: 24,
  rate: 60,
  payload: frame(0),
  mode: () => "physical",
  fetch: (m) => new Promise((resolve) => pending.push({ m, resolve })),
  onFrame: (_, m) => commits.push(m),
  onDraw: (p) => draws.push(p.clock.position),
  onState: (s) => states.push(s),
  onError: (e) => {
    throw e;
  },
  onStats: () => {},
});
const advance = (ms) => {
  now += ms;
  const work = [...rafs.values()];
  rafs.clear();
  for (const callback of work) callback(now);
};
async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}
(async () => {
  now = 0;
  player.play();
  advance(2000);
  assert.equal(player.clock.position, 0, "unfetched time never advances");
  pending.shift().resolve(frame(1));
  await settle();
  advance(500);
  assert.equal(player.clock.position, 0.5);
  player.pause();
  const frozen = player.clock.position;
  advance(3000);
  assert.equal(player.clock.position, frozen);
  assert.equal(rafs.size, 0);
  player.play();
  assert.equal(rafs.size, 1, "resume creates exactly one draw loop");
  advance(500);
  assert.equal(player.clock.position, 1);
  player.dispose();
  const count = commits.length;
  for (const request of pending) request.resolve(frame(request.m));
  await settle();
  advance(5000);
  assert.equal(commits.length, count);
  assert.equal(
    player.buffer.size,
    0,
    "late responses cannot resurrect disposed playback",
  );
  const eager = new context.window.HinanjoReplayPlayer({
    minute: 0,
    hi: 24,
    rate: 60,
    payload: frame(0),
    mode: () => "physical",
    fetch: async (m) => frame(m),
    onFrame: () => {},
    onDraw: () => {},
    onState: () => {},
    onError: (e) => {
      throw e;
    },
    onStats: () => {},
  });
  eager.play();
  for (let i = 0; i < 20; i++) await settle();
  assert.equal(eager.head, 8);
  assert.equal(eager.buffer.size, 9, "lookahead memory is bounded");
  eager.dispose();
  const batches = [];
  const ranged = new context.window.HinanjoReplayPlayer({
    minute: 0,
    hi: 24,
    rate: 60,
    payload: frame(0),
    mode: () => "physical",
    fetchRange: async (start, end) => {
      batches.push([start, end]);
      return Array.from({ length: end - start + 1 }, (_, i) =>
        frame(start + i),
      );
    },
    onFrame: () => {},
    onDraw: () => {},
    onState: () => {},
    onError: (e) => {
      throw e;
    },
    onStats: () => {},
  });
  ranged.play();
  for (let i = 0; i < 20; i++) await settle();
  assert.equal(ranged.head, 8);
  advance(1000);
  for (let i = 0; i < 20; i++) await settle();
  assert.equal(ranged.head, 16);
  assert(ranged.buffer.size <= 17);
  assert.deepEqual(
    batches,
    [
      [1, 8],
      [9, 16],
    ],
    "batch reads are bounded and preserve every minute",
  );
  ranged.dispose();
  console.log(
    "PASS: exact 1x/60x/120x clocks, continuous rate changes, buffer wait, pause/resume, one RAF, stale response disposal, bounded lookahead, doors and no wall shortcuts",
  );
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
