/* Pure offline presentation invariants; no server, Provider, or Run mutation. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const context = { window: {} };
vm.runInNewContext(
  fs.readFileSync(
    path.join(__dirname, "../src/sheltersim/web/static/demo-v2-scene.js"),
    "utf8",
  ),
  context,
);
const scene = context.window.HinanjoScene;
const rectangle = (x, y, w, h) => [
  { x, y },
  { x: x + w, y },
  { x: x + w, y: y + h },
  { x, y: y + h },
];
const ids = [
  "corridor",
  "sleeping",
  "medical",
  "quiet",
  "intake",
  "staff",
  "food",
  "toilet",
];
const frame = {
  run_id: "r",
  branch_id: "a",
  minute: 0,
  geometry: {
    width_m: 32,
    height_m: 20,
    zones: ids.map((zone_id, i) => ({
      zone_id,
      polygon: rectangle(i * 4, 0, 4, 20),
      usable_area_m2: 72,
    })),
    routes: [],
  },
  agents: [],
  people: [],
  staff: [{ id: "s", zone_id: "staff", position: null }],
  facilities: [],
  movement: { active_journeys: [] },
};
for (const n of [30, 100]) {
  const input = structuredClone(frame);
  for (let i = 0; i < n; i++) {
    const id = `A${String(i).padStart(3, "0")}`;
    input.agents.push({
      agent_id: id,
      zone_id: "sleeping",
      point: { x: 6, y: 10 },
      position_kind: "physical",
    });
    input.people.push({ id, zone_id: "sleeping", stress: i / n });
  }
  const before = JSON.stringify(input),
    out = scene.project(input);
  assert.equal(
    JSON.stringify(input),
    before,
    "projection must not modify recorded data",
  );
  assert.equal(out.agents.length, n);
  assert.equal(
    new Set(out.agents.map((a) => `${a.point.x},${a.point.y}`)).size,
    n,
    "every person has an individual token",
  );
  assert.deepEqual(
    Array.from(out.geometry.zones, (z) => z.zone_id),
    ids,
  );
  assert.deepEqual(
    Array.from(out.geometry.zones, (z) => z.usable_area_m2),
    ids.map(() => 72),
  );
  assert(out.staff[0].presentation_position);
  const changed = structuredClone(input);
  changed.agents[0].zone_id = "toilet";
  const next = scene.project(changed);
  assert.equal(
    JSON.stringify(out.agents[1].point),
    JSON.stringify(next.agents[1].point),
    "another person changing zone cannot move this token",
  );
  assert.equal(scene.project(input, "physical"), input);
}
const custom = structuredClone(frame);
custom.geometry.zones[0].zone_id = "custom";
assert.equal(
  scene.project(custom),
  custom,
  "unmapped geometry falls back intact",
);
const a = structuredClone(frame),
  b = structuredClone(frame);
a.agents = [
  {
    agent_id: "p",
    zone_id: "sleeping",
    point: { x: 0, y: 0 },
    edge_id: "edge",
  },
];
b.agents = [
  {
    agent_id: "p",
    zone_id: "sleeping",
    point: { x: 10, y: 10 },
    edge_id: "edge",
  },
];
a.geometry.routes = [
  {
    edge_id: "edge",
    points: [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 10, y: 10 },
    ],
  },
];
b.minute = 1;
b.geometry = structuredClone(a.geometry);
const source = JSON.stringify([a, b]),
  mid = scene.sample(a, b, 0.5);
assert.equal(mid.agents[0].point.x, 10);
assert.equal(
  mid.agents[0].point.y,
  0,
  "follow the recorded corner, not a diagonal shortcut",
);
assert(mid.agents[0]._walking);
assert.equal(
  JSON.stringify([a, b]),
  source,
  "interpolation must not mutate frames",
);
const unknown = structuredClone(b);
unknown.agents[0].edge_id = null;
assert.equal(
  scene.sample(a, unknown, 0.5).agents[0],
  a.agents[0],
  "unknown paths never create walks",
);
unknown.branch_id = "other";
assert.equal(
  scene.sample(a, unknown, 0.5),
  a,
  "never interpolate between branches",
);
assert.equal(scene.sample(a, b, 0), a);
assert.equal(scene.sample(a, b, 1), b);
const staffA = structuredClone(a),
  staffB = structuredClone(b);
staffA.staff = [
  {
    id: "s",
    zone_id: "sleeping",
    position: { point: { x: 0, y: 0 }, edge_id: "edge" },
  },
];
staffB.staff = [
  {
    id: "s",
    zone_id: "sleeping",
    position: { point: { x: 10, y: 10 }, edge_id: "edge" },
  },
];
const staffSource = JSON.stringify([staffA, staffB]),
  staffMid = scene.sample(staffA, staffB, 0.5);
assert.equal(staffMid.staff[0].position.x, 10);
assert.equal(staffMid.staff[0].position.y, 0);
assert(staffMid.staff[0]._walking);
assert.equal(JSON.stringify([staffA, staffB]), staffSource);
console.log(
  "PASS: 30/100 individuals, stable diagram slots, IDs/areas preserved, immutable input, physical/custom fallback, recorded corner interpolation, unknown path hold, branch isolation",
);
