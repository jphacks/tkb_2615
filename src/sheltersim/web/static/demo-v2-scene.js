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
  const doors = {
    sleeping: { inside: { x: 21, y: 57 }, outside: { x: 19, y: 57 } },
    medical: { inside: { x: 17, y: 16 }, outside: { x: 19, y: 16 } },
    quiet: { inside: { x: 17, y: 38 }, outside: { x: 19, y: 38 } },
    intake: { inside: { x: 17, y: 59 }, outside: { x: 19, y: 59 } },
    staff: { inside: { x: 81, y: 16 }, outside: { x: 79, y: 16 } },
    food: { inside: { x: 81, y: 38 }, outside: { x: 79, y: 38 } },
    toilet: { inside: { x: 81, y: 59 }, outside: { x: 79, y: 59 } },
  };
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  function inside(p, polygon) {
    let result = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      const a = polygon[i],
        b = polygon[j];
      if (
        a.y > p.y !== b.y > p.y &&
        p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x
      )
        result = !result;
    }
    return result;
  }
  function diagramZone(p) {
    for (const [id, b] of Object.entries(boxes))
      if (
        id !== "corridor" &&
        p.x >= b[0] - 0.001 &&
        p.x <= b[2] + 0.001 &&
        p.y >= b[1] - 0.001 &&
        p.y <= b[3] + 0.001
      )
        return id;
    return "corridor";
  }
  function corridorPath(a, b) {
    if (
      Math.abs(a.x - b.x) < 0.01 ||
      (Math.abs(a.y - 3) < 0.01 && Math.abs(b.y - 3) < 0.01)
    )
      return [a, b];
    return [a, { x: a.x, y: 3 }, { x: b.x, y: 3 }, b];
  }
  function diagramPath(a, b) {
    const from = diagramZone(a),
      to = diagramZone(b);
    if (from === to && from !== "corridor") return [a, b];
    const exit = doors[from],
      entry = doors[to];
    return [
      a,
      ...(exit ? [exit.inside, exit.outside] : []),
      ...corridorPath(exit?.outside || a, entry?.outside || b),
      ...(entry ? [entry.inside] : []),
      b,
    ].filter((p, i, all) => !i || distance(p, all[i - 1]) > 0.00001);
  }
  function edgeZones(frame, id) {
    const r = frame.geometry?.routes?.find((r) => r.edge_id === id);
    if (!r) return null;
    const zone = (p) =>
      frame.geometry.zones.find((z) => inside(p, z.polygon))?.zone_id;
    const from = zone(r.points[0]),
      to = zone(r.points.at(-1));
    return from && to ? { from, to, points: r.points } : null;
  }
  const projectedFrames = new WeakMap();
  function project(frame, mode = "schematic") {
    if (frame?.presentation_mode === "schematic") return frame;
    if (mode !== "schematic" || !supported(frame)) return frame;
    if (projectedFrames.has(frame)) return projectedFrames.get(frame);
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
      if (zone === "corridor")
        return { x: index % 2 ? 19 : 79, y: 10 + (index % 13) * 3.5 };
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
          (zone === "sleeping" ? 12 : 9) +
          ((row + 0.5) / rows) *
            (b[3] - b[1] - (zone === "sleeping" ? 15 : 13)),
      };
    };
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
    const facilityPoints = new Map(facilities.map((f) => [f.id, f.point]));
    const queuePositions = new Map(),
      presentationQueues = [];
    for (const zone of Object.keys(boxes)) {
      const waiting = [];
      for (const f of (frame.facilities || [])
        .filter((f) => f.zone_id === zone)
        .sort((a, b) => a.id.localeCompare(b.id))) {
        const queues =
          f.service_queues?.filter((q) => q.queue_agent_ids?.length) || [];
        const entries = queues.length
          ? queues.flatMap((q) =>
              q.queue_agent_ids.map((id, rank) => ({
                id,
                rank,
                queue: q.queue_id,
              })),
            )
          : [
              ...new Set([
                ...(f.queue_agent_ids || []),
                ...(f.resource_queue_agent_ids || []),
              ]),
            ].map((id, rank) => ({ id, rank, queue: f.id }));
        waiting.push(...entries);
      }
      const b = boxes[zone];
      waiting.forEach((entry, i) => {
        const columns = Math.min(
            5,
            Math.max(1, Math.ceil(Math.sqrt(waiting.length * 1.4))),
          ),
          rows = Math.ceil(waiting.length / columns);
        const row = Math.floor(i / columns),
          col = row % 2 ? columns - 1 - (i % columns) : i % columns;
        const p = {
          x: b[0] + 2 + ((col + 0.5) / columns) * (b[2] - b[0] - 4),
          y: b[3] - 4 - ((row + 0.5) / Math.max(1, rows)) * (b[3] - b[1] - 12),
        };
        queuePositions.set(entry.id, {
          ...p,
          queue: entry.queue,
          rank: entry.rank,
        });
      });
      if (waiting.length)
        presentationQueues.push({
          zone_id: zone,
          points: waiting.map((e) => queuePositions.get(e.id)),
        });
    }
    const journeys = new Map(
      (frame.movement?.active_journeys || []).map((j) => [j.agent_id, j]),
    );
    const approaching = new Map(),
      approachPositions = new Map();
    for (const j of journeys.values())
      if (j.facility_id && j.route_points?.length) {
        if (!approaching.has(j.facility_id)) approaching.set(j.facility_id, []);
        approaching.get(j.facility_id).push(j);
      }
    for (const [id, items] of approaching) {
      const facility = facilities.find((f) => f.id === id),
        b = boxes[facility?.zone_id];
      if (!b) continue;
      items
        .sort((a, b) => a.agent_id.localeCompare(b.agent_id))
        .forEach((j, i) => {
          const columns = Math.min(5, items.length),
            rows = Math.ceil(items.length / columns);
          approachPositions.set(j.agent_id, {
            x: b[0] + 2 + (((i % columns) + 0.5) / columns) * (b[2] - b[0] - 4),
            y:
              b[3] -
              4 -
              ((Math.floor(i / columns) + 0.5) / rows) * (b[3] - b[1] - 12),
          });
        });
    }
    const agents = (frame.agents || []).map((a) => {
      const j = journeys.get(a.agent_id);
      let displayed =
        queuePositions.get(a.agent_id) || seat(a.agent_id, a.zone_id);
      if (a.edge_id) {
        const edge = edgeZones(frame, a.edge_id);
        const i = j?.route_edge_ids?.indexOf(a.edge_id) ?? -1;
        const from = i >= 0 ? j.route_zone_ids[i] : edge?.from,
          to = i >= 0 ? j.route_zone_ids[i + 1] : edge?.to;
        if (edge && boxes[from] && boxes[to])
          displayed = along(
            diagramPath(seat(a.agent_id, from), seat(a.agent_id, to)),
            a.progress,
          );
      } else if (j?.route_points?.length && a.point) {
        const zone = j.destination_zone_id;
        const at = progressAt(j.route_points, a.point);
        const target =
          queuePositions.get(a.agent_id) ||
          approachPositions.get(a.agent_id) ||
          facilityPoints.get(j.facility_id) ||
          seat(a.agent_id, zone);
        if (at.distance < 0.02 && boxes[zone])
          displayed = along(
            diagramPath(seat(a.agent_id, zone), target),
            at.progress,
          );
      }
      return {
        ...a,
        point: displayed,
        presentation_queue: queuePositions.get(a.agent_id),
        physical_point: a.point,
        presentation_position: true,
      };
    });
    const staff = (frame.staff || []).map((s) => {
      let position = seat(s.id, s.zone_id);
      if (s.position?.edge_id) {
        const edge = edgeZones(frame, s.position.edge_id),
          i = s.operation?.route_edge_ids?.indexOf(s.position.edge_id) ?? -1;
        const from = i >= 0 ? s.operation.route_zone_ids[i] : edge?.from,
          to = i >= 0 ? s.operation.route_zone_ids[i + 1] : edge?.to;
        if (edge && boxes[from] && boxes[to])
          position = along(
            diagramPath(seat(s.id, from), seat(s.id, to)),
            s.position.progress,
          );
      }
      return {
        ...s,
        position,
        physical_position: s.position,
        presentation_position: true,
      };
    });
    const projected = {
      ...frame,
      exact: false,
      agents,
      presentation_queues: presentationQueues,
      presentation_doors: Object.fromEntries(
        Object.entries(doors).filter(([zone]) =>
          frame.geometry.routes?.some((r) => {
            const edge = edgeZones(frame, r.edge_id);
            return edge && (edge.from === zone || edge.to === zone);
          }),
        ),
      ),
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
    projectedFrames.set(frame, projected);
    return projected;
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
  function trimPath(path, start, end) {
    const total = path
      .slice(1)
      .reduce((sum, p, i) => sum + distance(p, path[i]), 0);
    const first = progressAt(path, start),
      last = progressAt(path, end);
    if (first.distance > 0.025 || last.distance > 0.025) return null;
    if (first.progress > last.progress)
      return trimPath([...path].reverse(), start, end);
    const points = [start];
    let walked = 0;
    for (let i = 1; i < path.length; i++) {
      walked += distance(path[i - 1], path[i]);
      const fraction = walked / (total || 1);
      if (
        fraction > first.progress + 0.000001 &&
        fraction < last.progress - 0.000001
      )
        points.push(path[i]);
    }
    points.push(end);
    return points;
  }
  function joinPaths(pieces) {
    if (!pieces.length || pieces.some((p) => !p?.length)) return null;
    for (const reverse of [false, true]) {
      let joined = reverse ? [...pieces[0]].reverse() : [...pieces[0]],
        valid = true;
      for (const part of pieces.slice(1)) {
        const end = joined.at(-1),
          forward = distance(end, part[0]) < 0.025,
          backward = distance(end, part.at(-1)) < 0.025;
        if (!forward && !backward) {
          valid = false;
          break;
        }
        joined.push(...(forward ? part : [...part].reverse()).slice(1));
      }
      if (valid) return joined;
    }
    return null;
  }
  function journeyPath(frame, j) {
    if (j?.route_points?.length > 1) return j.route_points;
    return j?.route_edge_ids?.length
      ? joinPaths(
          j.route_edge_ids.map(
            (id) =>
              frame.geometry?.routes?.find((r) => r.edge_id === id)?.points,
          ),
        )
      : null;
  }
  function recordedPath(a, b, p, q) {
    if (!p?.point || !q?.point || distance(p.point, q.point) < 0.00001)
      return null;
    const j = a.movement?.active_journeys?.find(
        (j) => j.agent_id === p.agent_id,
      ),
      k = b.movement?.active_journeys?.find((j) => j.agent_id === p.agent_id);
    const candidates = [];
    if (p.edge_id && p.edge_id === q.edge_id)
      candidates.push({
        path: a.geometry?.routes?.find((r) => r.edge_id === p.edge_id)?.points,
        evidence: p.edge_id,
      });
    for (const item of [j, k]) {
      if (!item || item.started_minute > b.minute || item.due_minute < a.minute)
        continue;
      candidates.push({ path: journeyPath(a, item), evidence: item.event_id });
    }
    if (
      j &&
      k &&
      j.event_id !== k.event_id &&
      j.due_minute <= k.started_minute
    ) {
      candidates.push({
        path: joinPaths([journeyPath(a, j), journeyPath(b, k)]),
        evidence: j.event_id + " → " + k.event_id,
      });
    }
    for (const c of candidates) {
      if (!c.path?.length) continue;
      const path = trimPath(c.path, p.point, q.point);
      if (path) return { path, evidence: c.evidence };
    }
    return null;
  }
  function pathLength(path) {
    return path.slice(1).reduce((n, p, i) => n + distance(path[i], p), 0);
  }
  function safeDiagramPath(path) {
    // Segment midpoints must lie in a known room or the corridor strips. A path
    // may cross a room boundary only through that room's declared display door.
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1],
        b = path[i];
      for (let k = 0; k <= 8; k++) {
        const p = {
          x: a.x + ((b.x - a.x) * k) / 8,
          y: a.y + ((b.y - a.y) * k) / 8,
        };
        if (p.x < 0.9 || p.x > 99.1 || p.y < 0.9 || p.y > 65.1) return false;
        const zone = diagramZone(p),
          start = diagramZone(a),
          end = diagramZone(b);
        if (zone !== start && zone !== end) return false;
        if (
          zone === "corridor" &&
          !(
            (((p.x >= 17 && p.x <= 21) || (p.x >= 77 && p.x <= 81)) &&
              p.y >= 1 &&
              p.y <= 65) ||
            (((p.y >= 1 && p.y <= 5) || (p.y >= 62 && p.y <= 65)) &&
              p.x >= 17 &&
              p.x <= 81)
          )
        )
          return false;
      }
      const from = diagramZone(a),
        to = diagramZone(b);
      if (from !== to) {
        const room = from === "corridor" ? to : from;
        if (!doors[room]) return false;
        const door = doors[room].inside;
        if (distance(a, door) > 0.01 && distance(b, door) > 0.01) return false;
      }
    }
    return true;
  }
  const plans = new WeakMap();
  function prepare(a, b, mode = "physical") {
    if (!a || !b) return null;
    let next = plans.get(a);
    if (!next) {
      next = new WeakMap();
      plans.set(a, next);
    }
    let modes = next.get(b);
    if (!modes) {
      modes = new Map();
      next.set(b, modes);
    }
    if (modes.has(mode)) return modes.get(mode);
    const diagram = mode === "schematic" && supported(a) && supported(b);
    const from = diagram ? project(a) : a,
      to = diagram ? project(b) : b;
    const target = new Map(b.agents.map((p) => [p.agent_id, p])),
      displayEnd = new Map(to.agents.map((p) => [p.agent_id, p]));
    const metrics = {
      total: a.agents.length,
      moving: a.people.filter((p) => p.activity === "moving").length,
      changedZones: [],
      recordedJourneys: [
        ...new Set(
          [a, b].flatMap((f) =>
            (f.movement?.active_journeys || [])
              .filter((j) => journeyPath(f, j)?.length > 1)
              .map((j) => j.agent_id),
          ),
        ),
      ],
      changedPositions: [],
      interpolated: [],
      fallback: [],
    };
    const compatible =
      a.run_id === b.run_id &&
      a.branch_id === b.branch_id &&
      b.minute === a.minute + 1 &&
      JSON.stringify(a.geometry) === JSON.stringify(b.geometry);
    const entries = from.agents.map((shown, index) => {
      const p = a.agents[index],
        q = target.get(p.agent_id),
        end = displayEnd.get(p.agent_id);
      if (q && p.zone_id !== q.zone_id) metrics.changedZones.push(p.agent_id);
      if (!q?.point || !p.point || distance(p.point, q.point) < 0.00001)
        return { shown };
      metrics.changedPositions.push(p.agent_id);
      const recorded = compatible ? recordedPath(a, b, p, q) : null;
      const path =
        recorded && shown.point && end?.point
          ? diagram
            ? diagramPath(shown.point, end.point)
            : recorded.path
          : null;
      if (
        !path ||
        pathLength(path) < 0.00001 ||
        (diagram && !safeDiagramPath(path))
      ) {
        metrics.fallback.push(p.agent_id);
        return { shown };
      }
      metrics.interpolated.push(p.agent_id);
      return {
        shown,
        path,
        length: pathLength(path),
        evidence: recorded.evidence,
      };
    });
    const staffRecord = (s) => ({
      agent_id: s.id,
      point: s.position?.point || s.position,
      edge_id: s.position?.edge_id,
      zone_id: s.position?.zone_id || s.zone_id,
    });
    const staffWorld = (f) => ({
      ...f,
      movement: {
        active_journeys: (f.staff || [])
          .filter((s) => s.operation?.task_id)
          .map((s) => ({
            ...s.operation,
            agent_id: s.id,
            event_id: s.operation.task_id,
            due_minute: s.operation.travel_due_minute ?? s.operation.due_minute,
            route_points: s.operation.route_points || [],
          })),
      },
    });
    const staffA = staffWorld(a),
      staffB = staffWorld(b);
    const staffEntries = (from.staff || []).map((shown, i) => {
      const original = a.staff[i],
        q = b.staff.find((s) => s.id === shown.id),
        end = to.staff.find((s) => s.id === shown.id);
      if (!q || !compatible) return { shown };
      const proof = recordedPath(
        staffA,
        staffB,
        staffRecord(original),
        staffRecord(q),
      );
      const start = diagram ? shown.position : staffRecord(original).point,
        finish = diagram ? end?.position : staffRecord(q).point;
      const path =
        proof && start && finish
          ? diagram
            ? diagramPath(start, finish)
            : proof.path
          : null;
      if (
        !path ||
        pathLength(path) < 0.00001 ||
        (diagram && !safeDiagramPath(path))
      )
        return { shown };
      return {
        shown,
        path,
        length: pathLength(path),
        evidence: proof.evidence,
      };
    });
    const interpolate = (e, t) => {
      if (!e.path) return e.shown;
      const point = along(e.path, t),
        ahead = along(e.path, Math.min(1, t + 0.0001)),
        behind = along(e.path, Math.max(0, t - 0.0001));
      return {
        ...e.shown,
        point,
        _walking: true,
        _distance: e.length * t,
        _direction: Math.atan2(ahead.y - behind.y, ahead.x - behind.x),
        _motionEvidence: e.evidence,
      };
    };
    const plan = {
      from,
      to,
      metrics,
      entries,
      staffEntries,
      sample(t) {
        if (t <= 0) return from;
        if (t >= 1) return to;
        return {
          ...from,
          exact: false,
          presentation_interpolation: {
            from: a.minute,
            to: b.minute,
            fraction: t,
          },
          agents: entries.map((e) => interpolate(e, t)),
          staff: staffEntries.map((e) => {
            const s = interpolate(e, t);
            return e.path ? { ...s, position: s.point } : s;
          }),
        };
      },
    };
    modes.set(mode, plan);
    return plan;
  }
  function sample(a, b, t, mode = "physical") {
    if (!a || !b || a.run_id !== b.run_id || a.branch_id !== b.branch_id)
      return a;
    return prepare(a, b, mode).sample(t);
  }
  window.HinanjoScene = {
    project,
    supported,
    sample,
    prepare,
    along,
    progressAt,
    doors,
    diagramPath,
    safeDiagramPath,
    recordedPath,
  };
})();
