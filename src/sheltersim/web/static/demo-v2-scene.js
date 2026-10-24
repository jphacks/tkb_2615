/* Read-only presentation adapter. Diagram units and offsets never enter WorldState. */
(() => {
  "use strict";
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const boxes = {
    sleeping: [21, 5, 77, 62],
    medical: [1, 1, 17, 20],
    quiet: [1, 23, 17, 43],
    intake: [1, 46, 17, 64],
    staff: [81, 1, 99, 20],
    food: [81, 23, 99, 43],
    toilet: [81, 46, 99, 64],
    corridor: [17, 1, 81, 65],
  };
  const rectangle = ([l, t, r, b]) => [
    { x: l, y: t },
    { x: r, y: t },
    { x: r, y: b },
    { x: l, y: b },
  ];
  const bbox = (polygon) => [
    Math.min(...polygon.map((p) => p.x)),
    Math.min(...polygon.map((p) => p.y)),
    Math.max(...polygon.map((p) => p.x)),
    Math.max(...polygon.map((p) => p.y)),
  ];
  const hash = (id) => {
    let n = 2166136261;
    for (const c of id) n = Math.imul(n ^ c.charCodeAt(0), 16777619);
    return n >>> 0;
  };
  const supported = (frame) =>
    Boolean(
      frame?.geometry?.zones?.length &&
      !frame.geometry_overlay?.temporary_rooms?.length &&
      (frame.agents || []).every((a) => boxes[a.zone_id]) &&
      frame.geometry.zones.every((z) => boxes[z.zone_id]) &&
      frame.geometry.zones.some((z) => z.zone_id === "sleeping"),
    );
  function project(frame, mode = "schematic") {
    if (mode !== "schematic" || !supported(frame)) return frame;
    const originals = new Map(frame.geometry.zones.map((z) => [z.zone_id, z]));
    const mapPoint = (p, zone) => {
      if (!p || !originals.has(zone)) return null;
      const from = bbox(originals.get(zone).polygon),
        to = boxes[zone];
      return {
        x:
          to[0] +
          clamp((p.x - from[0]) / (from[2] - from[0]), 0, 1) * (to[2] - to[0]),
        y:
          to[1] +
          clamp((p.y - from[1]) / (from[3] - from[1]), 0, 1) * (to[3] - to[1]),
      };
    };
    const occupied = new Map(),
      slots = new Map();
    // Stable slot assignment uses all people, regardless of their current zone.
    // Therefore queue entry/exit does not move everyone else's display token.
    const ids = [
      ...new Set([
        ...(frame.people || []).map((p) => p.id),
        ...(frame.agents || []).map((p) => p.agent_id),
        ...(frame.staff || []).map((p) => p.id),
      ]),
    ].sort();
    const count = Math.max(1, ids.length);
    for (const id of ids) {
      let slot = hash(id) % count;
      while (occupied.has(slot)) slot = (slot + 1) % count;
      occupied.set(slot, id);
      slots.set(id, slot);
    }
    const seat = (id, zone, queued = false) => {
      const b = boxes[zone];
      if (!b) return null;
      const index = slots.get(id) ?? hash(id) % count;
      const columns = Math.ceil(
          Math.sqrt(count * (zone === "sleeping" ? 1.2 : 1)),
        ),
        rows = Math.ceil(count / columns);
      const row = Math.floor(index / columns),
        col = index % columns;
      return {
        x: b[0] + 2 + ((col + 0.5) / columns) * (b[2] - b[0] - 4),
        y:
          b[1] +
          (zone === "sleeping" ? 12 : 6) +
          ((row + 0.5) / rows) * (b[3] - b[1] - (zone === "sleeping" ? 15 : 8)),
      };
    };
    const people = new Map((frame.people || []).map((p) => [p.id, p]));
    // Corridor is a connected ring rendered as a simple perimeter polygon.
    const corridor = [
      { x: 17, y: 1 },
      { x: 81, y: 1 },
      { x: 81, y: 65 },
      { x: 17, y: 65 },
      { x: 17, y: 4 },
      { x: 20, y: 4 },
      { x: 20, y: 63 },
      { x: 78, y: 63 },
      { x: 78, y: 4 },
      { x: 17, y: 4 },
    ];
    const zones = frame.geometry.zones.map((z) => ({
      ...z,
      polygon:
        z.zone_id === "corridor" ? corridor : rectangle(boxes[z.zone_id]),
    }));
    const facilityGroups = new Map();
    for (const f of frame.facilities || []) {
      if (!facilityGroups.has(f.zone_id)) facilityGroups.set(f.zone_id, []);
      facilityGroups.get(f.zone_id).push(f.id);
    }
    const facilities = (frame.facilities || []).map((f) => {
      const b = boxes[f.zone_id];
      if (!b) return { ...f, point: null, service_queues: [] };
      const ids = facilityGroups.get(f.zone_id).sort(),
        i = ids.indexOf(f.id);
      const p = {
        x:
          b[0] +
          3 +
          (i % 4) *
            ((b[2] - b[0] - 6) / Math.max(1, Math.min(3, ids.length - 1))),
        y: b[3] - 3 - Math.floor(i / 4) * 3,
      };
      return {
        ...f,
        point: p,
        service_queues: [],
        presentation_position: true,
      };
    });
    const agents = (frame.agents || []).map((a) => {
      const p = people.get(a.agent_id),
        moving = Boolean(
          a._walking ||
          p?.activity === "moving" ||
          frame.movement?.active_journeys?.some(
            (j) => j.agent_id === a.agent_id,
          ),
        );
      return {
        ...a,
        point: moving
          ? mapPoint(a.point, a.zone_id)
          : seat(a.agent_id, a.zone_id, p?.activity === "queued"),
        physical_point: a.point,
        presentation_position: true,
      };
    });
    const overlaps = new Map();
    for (const a of agents) {
      if (!a.point) continue;
      const key = a.point.x.toFixed(4) + "," + a.point.y.toFixed(4);
      if (!overlaps.has(key)) overlaps.set(key, []);
      overlaps.get(key).push(a);
    }
    for (const group of overlaps.values())
      if (group.length > 1) {
        const columns = Math.ceil(Math.sqrt(group.length)),
          rows = Math.ceil(group.length / columns);
        const cx = (columns - 1) * 0.9,
          cy = (rows - 1) * 0.9;
        const anchor = {
          x: clamp(group[0].point.x, 1 + cx, 99 - cx),
          y: clamp(group[0].point.y, 1 + cy, 65 - cy),
        };
        group
          .sort((a, b) => a.agent_id.localeCompare(b.agent_id))
          .forEach((a, i) => {
            a.point = {
              x: anchor.x + ((i % columns) - (columns - 1) / 2) * 1.8,
              y: anchor.y + (Math.floor(i / columns) - (rows - 1) / 2) * 1.8,
            };
          });
      }
    const staff = (frame.staff || []).map((s) => ({
      ...s,
      position: seat(s.id, s.zone_id),
      physical_position: s.position,
      presentation_position: true,
    }));
    return {
      ...frame,
      agents,
      staff,
      facilities,
      presentation_mode: "schematic",
      recorded_geometry: frame.geometry,
      geometry: {
        ...frame.geometry,
        width_m: 100,
        height_m: 66,
        zones,
        routes: [],
        walls: [],
        obstacles: [],
        facility_points: {},
        service_points: [],
        arrival_approach_routes: [],
      },
      // Physical overlay coordinates cannot be drawn on a rearranged diagram.
      geometry_overlay: {},
      spatial_constraints: null,
    };
  }
  function along(points, progress) {
    const lengths = points
      .slice(1)
      .map((p, i) => Math.hypot(p.x - points[i].x, p.y - points[i].y));
    let remain = clamp(progress, 0, 1) * lengths.reduce((a, b) => a + b, 0);
    for (let i = 0; i < lengths.length; i++) {
      if (remain <= lengths[i] || i === lengths.length - 1) {
        const t = lengths[i] ? remain / lengths[i] : 0,
          a = points[i],
          b = points[i + 1];
        return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
      }
      remain -= lengths[i];
    }
    return points[0];
  }
  function progressAt(points, p) {
    let length = 0,
      best = Infinity,
      result = 0,
      total = 0;
    const lengths = points
      .slice(1)
      .map((q, i) => Math.hypot(q.x - points[i].x, q.y - points[i].y));
    total = lengths.reduce((a, b) => a + b, 0);
    for (let i = 0; i < lengths.length; i++) {
      const a = points[i],
        b = points[i + 1],
        dx = b.x - a.x,
        dy = b.y - a.y;
      const t = clamp(
        ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy || 1),
        0,
        1,
      );
      const d = Math.hypot(p.x - a.x - t * dx, p.y - a.y - t * dy);
      if (d < best) {
        best = d;
        result = (length + t * lengths[i]) / (total || 1);
      }
      length += lengths[i];
    }
    return { progress: result, distance: best };
  }
  function sample(a, b, t, mode = "physical") {
    if (
      !a ||
      !b ||
      t <= 0 ||
      a.run_id !== b.run_id ||
      a.branch_id !== b.branch_id
    )
      return a;
    if (t >= 1) return b;
    if (JSON.stringify(a.geometry) !== JSON.stringify(b.geometry)) return a;
    const end = new Map(b.agents.map((p) => [p.agent_id, p]));
    const journeys = new Map(
      (a.movement?.active_journeys || []).map((j) => [j.agent_id, j]),
    );
    const nextJourneys = new Map(
      (b.movement?.active_journeys || []).map((j) => [j.agent_id, j]),
    );
    const agents = a.agents.map((p) => {
      const q = end.get(p.agent_id);
      if (!p.point || !q?.point) return p;
      if (mode === "schematic" && p.zone_id !== q.zone_id) return p;
      let path = null;
      if (p.edge_id && p.edge_id === q.edge_id) {
        path = a.geometry?.routes?.find((r) => r.edge_id === p.edge_id)?.points;
      } else {
        const j = journeys.get(p.agent_id),
          k = nextJourneys.get(p.agent_id);
        if (j && k && j.event_id === k.event_id) {
          path = j.route_points;
          if (!path?.length && j.route_edge_ids?.length) {
            const pieces = j.route_edge_ids.map(
              (id) => a.geometry?.routes?.find((r) => r.edge_id === id)?.points,
            );
            if (pieces.every(Boolean)) {
              const joined = pieces[0].map((p) => ({ ...p }));
              for (const piece of pieces.slice(1)) {
                const last = joined.at(-1),
                  first = piece[0],
                  end = piece.at(-1);
                const forward =
                  Math.hypot(last.x - first.x, last.y - first.y) < 0.02;
                const backward =
                  Math.hypot(last.x - end.x, last.y - end.y) < 0.02;
                if (!forward && !backward) {
                  joined.length = 0;
                  break;
                }
                joined.push(
                  ...(forward ? piece : [...piece].reverse()).slice(1),
                );
              }
              path = joined;
            }
          }
        }
        if (
          !path?.length &&
          k?.route_points?.length &&
          k.started_minute >= a.minute &&
          k.started_minute < b.minute
        )
          path = k.route_points;
      }
      if (!path || path.length < 2) return p;
      const start = progressAt(path, p.point),
        finish = progressAt(path, q.point);
      if (
        start.distance > 0.02 ||
        finish.distance > 0.02 ||
        Math.abs(finish.progress - start.progress) < 0.000001
      )
        return p;
      const at = along(
        path,
        start.progress + (finish.progress - start.progress) * t,
      );
      return {
        ...p,
        point: at,
        _walking:
          Math.hypot(q.point.x - p.point.x, q.point.y - p.point.y) > 0.001,
        _direction: Math.atan2(q.point.y - p.point.y, q.point.x - p.point.x),
      };
    });
    return { ...a, agents };
  }
  window.HinanjoScene = { project, supported, sample, along, progressAt };
})();
