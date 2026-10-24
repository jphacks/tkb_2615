/* Presentation only: all geometry, positions and counts come from recorded frames. */
(() => {
  "use strict";
  const rooms = {
    sleeping: {
      name: "避難スペース",
      short: "避難スペース",
      floor: "#e7c58d",
      edge: "#b28c4c",
      icon: "home",
    },
    quiet: {
      name: "福祉・休養スペース",
      short: "福祉\n休養",
      floor: "#e4e0ed",
      edge: "#9c95b1",
      icon: "rest",
    },
    medical: {
      name: "救護室",
      short: "救護",
      floor: "#e1efec",
      edge: "#8ca9a5",
      icon: "medical",
    },
    food: {
      name: "物資配布",
      short: "物資\n配布",
      floor: "#eee1c5",
      edge: "#b7a381",
      icon: "box",
    },
    water: {
      name: "給水",
      short: "給水",
      floor: "#dcecf3",
      edge: "#8faebd",
      icon: "water",
    },
    toilet: {
      name: "トイレ",
      short: "トイレ",
      floor: "#d6eaf2",
      edge: "#7e9fac",
      icon: "toilet",
    },
    intake: {
      name: "受付",
      short: "受付",
      floor: "#e1eae6",
      edge: "#91aaa1",
      icon: "desk",
    },
    staff: {
      name: "スタッフ本部",
      short: "運営\n本部",
      floor: "#dce5ec",
      edge: "#8fa1b1",
      icon: "staff",
    },
    corridor: {
      name: "通路",
      short: "通路",
      floor: "#d4d9d5",
      edge: "#a4afa9",
      icon: "route",
    },
  };
  const palette = [
    "#148ac0",
    "#00a884",
    "#e8a927",
    "#e75653",
    "#6979c2",
    "#39a566",
  ];
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  function round(ctx, x, y, w, h, r = 4) {
    ctx.beginPath();
    ctx.roundRect(x, y, Math.max(0, w), Math.max(0, h), r);
  }
  function polygon(ctx, points) {
    ctx.beginPath();
    points.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.closePath();
  }
  function symbol(ctx, kind, x, y, s, color = "#28527b") {
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(s / 20, s / 20);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (kind === "medical") {
      ctx.fillRect(-2.5, -8, 5, 16);
      ctx.fillRect(-8, -2.5, 16, 5);
    } else if (kind === "water") {
      ctx.beginPath();
      ctx.moveTo(0, -9);
      ctx.bezierCurveTo(-4, -3, -8, 1, -6, 5);
      ctx.bezierCurveTo(-3, 12, 9, 8, 6, 1);
      ctx.closePath();
      ctx.fill();
    } else if (kind === "wc") {
      ctx.strokeRect(-5, -8, 10, 4);
      ctx.beginPath();
      ctx.ellipse(0, 2, 6, 7, 0, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.ellipse(0, 1, 3, 4, 0, 0, Math.PI * 2);
      ctx.stroke();
    } else if (kind === "toilet" || kind === "staff") {
      for (const xx of [-4, 4]) {
        ctx.beginPath();
        ctx.arc(xx, -5, 2.2, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillRect(xx - 2, -1, 4, 7);
        ctx.fillRect(xx - 1.5, 6, 1.2, 3);
        ctx.fillRect(xx + 0.3, 6, 1.2, 3);
      }
    } else if (kind === "home") {
      ctx.beginPath();
      ctx.moveTo(-9, -1);
      ctx.lineTo(0, -9);
      ctx.lineTo(9, -1);
      ctx.moveTo(-6, -3);
      ctx.lineTo(-6, 8);
      ctx.lineTo(6, 8);
      ctx.lineTo(6, -3);
      ctx.stroke();
    } else if (kind === "box") {
      ctx.strokeRect(-7, -6, 14, 13);
      ctx.beginPath();
      ctx.moveTo(-7, -2);
      ctx.lineTo(7, -2);
      ctx.moveTo(0, -6);
      ctx.lineTo(0, 2);
      ctx.stroke();
    } else if (kind === "rest") {
      ctx.beginPath();
      ctx.moveTo(-7, 8);
      ctx.lineTo(-7, -4);
      ctx.moveTo(-7, 2);
      ctx.lineTo(7, 2);
      ctx.lineTo(7, 8);
      ctx.moveTo(-7, 6);
      ctx.lineTo(7, 6);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(-3, -1, 2, 0, Math.PI * 2);
      ctx.fill();
    } else if (kind === "route") {
      ctx.beginPath();
      ctx.moveTo(-8, 0);
      ctx.lineTo(8, 0);
      ctx.moveTo(3, -5);
      ctx.lineTo(8, 0);
      ctx.lineTo(3, 5);
      ctx.stroke();
    } else {
      ctx.strokeRect(-8, -3, 16, 7);
      ctx.beginPath();
      ctx.moveTo(-6, 4);
      ctx.lineTo(-6, 9);
      ctx.moveTo(6, 4);
      ctx.lineTo(6, 9);
      ctx.stroke();
    }
    ctx.restore();
  }
  function person(ctx, x, y, size, color, staff, selected, motion = {}) {
    ctx.save();
    ctx.translate(x, y);
    const phase = motion.phase || 0;
    const step = motion.walking ? Math.sin(phase * 8) * size * 0.28 : 0;
    const breathe = motion.active ? Math.sin(phase * 2) * size * 0.035 : 0;
    ctx.fillStyle = "#203b4936";
    ctx.beginPath();
    ctx.ellipse(1, size * 0.85, size * 0.85, size * 0.28, 0, 0, Math.PI * 2);
    ctx.fill();
    if (selected) {
      ctx.strokeStyle = "#0878ff";
      ctx.fillStyle = "#ffffffa8";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.ellipse(0, -size * 0.3, size * 1.1, size * 1.75, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    ctx.fillStyle = "#314c64";
    round(ctx, -size * 0.45, size * 0.22 + step, size * 0.36, size * 0.73, 1);
    ctx.fill();
    round(ctx, size * 0.09, size * 0.22 - step, size * 0.36, size * 0.73, 1);
    ctx.fill();
    ctx.translate(0, breathe);
    ctx.scale(
      motion.walking && Math.cos(motion.direction || 0) < 0 ? -0.95 : 1,
      1,
    );
    ctx.fillStyle = color;
    round(ctx, -size * 0.65, -size * 0.62, size * 1.3, size * 1.1, size * 0.28);
    ctx.fill();
    ctx.fillStyle = "#f1c39b";
    round(ctx, -size * 0.82, -size * 0.4 - step, size * 0.22, size * 0.76, 1);
    ctx.fill();
    round(ctx, size * 0.6, -size * 0.4 + step, size * 0.22, size * 0.76, 1);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(0, -size * 0.99, size * 0.47, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = staff ? "#ecf8ff" : "#3d444b";
    ctx.beginPath();
    ctx.ellipse(
      0,
      -size * 1.25,
      size * 0.49,
      size * 0.25,
      0,
      Math.PI,
      Math.PI * 2,
    );
    ctx.fill();
    if (staff) {
      ctx.fillStyle = "#d9ffff";
      ctx.fillRect(-size * 0.47, -size * 0.4, size * 0.94, size * 0.2);
    }
    if (motion.walking) {
      ctx.fillStyle = "#edbd96";
      ctx.fillRect(size * 0.35, -size * 0.98, size * 0.3, size * 0.2);
    }
    ctx.restore();
  }
  function drawActors(ctx, frame, point, scale, w, h, options, hits) {
    const layer = options.layer || "normal",
      compact = w < 460;
    const people = new Map((frame.people || []).map((p) => [p.id, p]));
    const groups = new Map();
    for (const a of frame.agents || []) {
      if (!a.point || (!options.diagram && a.position_kind === "display_only"))
        continue;
      const key = `${a.point.x.toFixed(4)},${a.point.y.toFixed(4)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    const actorSize = clamp(
      scale * (options.diagram ? 0.95 : 0.38),
      compact ? 4.3 : 6,
      compact ? 6.8 : 9.2,
    );
    const colorOf = (p) => {
      if (layer === "stress" || layer === "health") {
        const needs = [
          p?.needs?.fatigue,
          p?.needs?.hunger,
          p?.needs?.hydration_need,
        ];
        const n =
          layer === "stress"
            ? p?.stress
            : needs.every((v) => typeof v === "number")
              ? Math.max(...needs)
              : undefined;
        return typeof n === "number"
          ? `hsl(${120 * (1 - clamp(n, 0, 1))} 72% 43%)`
          : "#8494a5";
      }
      if (p?.activity === "queued") return "#efa829";
      let seed = 0;
      for (const c of p?.id || "a") seed = (seed * 31 + c.charCodeAt(0)) >>> 0;
      return palette[seed % palette.length];
    };
    for (const group of [...groups.values()].sort(
      (a, b) => a[0].point.y - b[0].point.y,
    )) {
      const a = group[0],
        p = point(a.point),
        personData = people.get(a.agent_id),
        selected = group.some((x) => x.agent_id === options.selected);
      if (p.x < -20 || p.x > w + 20 || p.y < -30 || p.y > h + 30) continue;
      if (group.length > 1) {
        // A stacked symbol represents co-located people; these offsets are not world positions.
        for (let i = Math.min(3, group.length - 1); i >= 0; i--) {
          const pp = people.get(group[i].agent_id);
          person(
            ctx,
            p.x + (i - 1.5) * actorSize * 0.55,
            p.y + (i % 2) * 2,
            actorSize,
            colorOf(pp),
            false,
            selected && i === 0,
          );
        }
        ctx.font = "bold 11px sans-serif";
        const label = String(group.length),
          bw = Math.max(22, ctx.measureText(label).width + 14);
        ctx.fillStyle = "#0c7e75";
        ctx.strokeStyle = "white";
        ctx.lineWidth = 1.7;
        round(ctx, p.x + actorSize * 0.6, p.y - actorSize * 2.5, bw, 20, 10);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "white";
        ctx.font = "bold 11px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(
          label,
          p.x + actorSize * 0.6 + bw / 2,
          p.y - actorSize * 2.5 + 14,
        );
      } else {
        if (options.diagram && a.zone_id === "sleeping") {
          ctx.fillStyle = "#90b5a470";
          ctx.strokeStyle = "#fff8e4b0";
          ctx.lineWidth = 1;
          round(
            ctx,
            p.x - actorSize * 1.4,
            p.y - actorSize * 0.8,
            actorSize * 2.8,
            actorSize * 2.8,
            3,
          );
          ctx.fill();
          ctx.stroke();
        }
        let seed = 0;
        for (const c of a.agent_id) seed += c.charCodeAt(0);
        person(ctx, p.x, p.y, actorSize, colorOf(personData), false, selected, {
          active: options.animate,
          phase: (options.time || 0) / 1000 + seed,
          walking: a._walking,
          direction: a._direction,
        });
      }
      hits.push({
        type: "person",
        id: a.agent_id,
        ids: group.map((x) => x.agent_id),
        x: p.x,
        y: p.y,
        r: Math.max(12, actorSize * 1.8),
        label:
          group.length > 1
            ? `同じ記録位置に${group.length}人 · クリックして一覧`
            : `${a.agent_id} · ストレス ${typeof personData?.stress === "number" ? Math.round(personData.stress * 100) : "未記録"}`,
      });
    }
    for (const s of frame.staff || []) {
      const physical = s.position?.point || s.position;
      if (typeof physical?.x !== "number" || typeof physical?.y !== "number")
        continue;
      const p = point(physical);
      person(
        ctx,
        p.x,
        p.y,
        actorSize,
        "#188dd0",
        true,
        s.id === options.selected,
        {
          active: options.animate,
          phase: (options.time || 0) / 1000 + s.id.length,
        },
      );
      hits.push({
        type: "person",
        id: s.id,
        ids: [s.id],
        x: p.x,
        y: p.y,
        r: actorSize * 1.8,
        label: `${s.id} · スタッフ`,
      });
    }
  }
  function draw(sf, frame, options = {}) {
    const { ctx, w, h } = sf,
      hits = [];
    ctx.fillStyle = "#dce6dc";
    ctx.fillRect(0, 0, w, h);
    if (!frame?.geometry) {
      ctx.fillStyle = "#56728c";
      ctx.textAlign = "center";
      ctx.font = "13px sans-serif";
      ctx.fillText(
        frame
          ? "物理座標が記録されていません"
          : "シナリオを作成すると施設が表示されます",
        w / 2,
        h / 2,
      );
      return hits;
    }
    const g = frame.geometry,
      layer = options.layer || "normal",
      zoom = options.zoom || 1;
    const compact = w < 460,
      pad = compact ? 12 : 22;
    const viewW = options.viewportBounds?.width || g.width_m,
      viewH = options.viewportBounds?.height || g.height_m;
    const scale = Math.min((w - pad * 2) / viewW, (h - pad * 2) / viewH) * zoom;
    const ox = (w - viewW * scale) / 2 + (options.pan?.x || 0),
      oy = (h - viewH * scale) / 2 + (options.pan?.y || 0);
    const point = (p) => ({ x: ox + p.x * scale, y: oy + p.y * scale });
    // Paving and foliage are decorative backdrop, not a reconstruction of a real site.
    ctx.strokeStyle = "#c2cec355";
    ctx.lineWidth = 1;
    for (let x = 0; x < w; x += 32) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    for (let y = 0; y < h; y += 32) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }
    for (const [x, y] of [
      [8, 8],
      [w - 8, 10],
      [6, h - 8],
      [w - 8, h - 5],
    ]) {
      ctx.fillStyle = "#719d71";
      for (let i = 0; i < 5; i++) {
        ctx.beginPath();
        ctx.arc(
          x + Math.cos(i * 1.3) * 11,
          y + Math.sin(i * 1.3) * 11,
          13,
          0,
          Math.PI * 2,
        );
        ctx.fill();
      }
      ctx.fillStyle = "#97b87d";
      ctx.beginPath();
      ctx.arc(x - 5, y - 4, 12, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.shadowColor = "#203d4b45";
    ctx.shadowBlur = 16;
    ctx.shadowOffsetY = 7;
    ctx.fillStyle = "#758587";
    round(
      ctx,
      ox - 7,
      oy - 6,
      g.width_m * scale + 14,
      g.height_m * scale + 17,
      3,
    );
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    const counts = { ...options.crowdCounts };
    for (const a of frame.agents || [])
      counts[a.zone_id] = (counts[a.zone_id] || 0) + 1;
    const bounds = [];
    for (const z of g.zones || []) {
      const key = z.zone_id.split("_")[0],
        spec = rooms[key] || {
          name: z.zone_id,
          short: z.zone_id,
          floor: "#e3e8df",
          edge: "#a2ada4",
          icon: "home",
        };
      const poly = z.polygon.map(point),
        xs = poly.map((p) => p.x),
        ys = poly.map((p) => p.y);
      const b = {
        z,
        key,
        spec,
        x: Math.min(...xs),
        y: Math.min(...ys),
        w: Math.max(...xs) - Math.min(...xs),
        h: Math.max(...ys) - Math.min(...ys),
        poly,
      };
      bounds.push(b);
      ctx.save();
      polygon(ctx, poly);
      ctx.fillStyle = spec.floor;
      if (layer === "crowd") {
        const density =
          (counts[z.zone_id] || 0) / Math.max(1, z.usable_area_m2);
        ctx.fillStyle = `hsl(${125 - clamp(density * 180, 0, 125)} 46% 82%)`;
      }
      ctx.fill();
      ctx.clip();
      if (key === "sleeping") {
        const plank = clamp(scale * 0.45, 8, 14);
        for (let y = b.y; y < b.y + b.h; y += plank) {
          ctx.fillStyle =
            Math.round((y - b.y) / plank) % 2 ? "#f4dab115" : "#bd945208";
          ctx.fillRect(b.x, y, b.w, plank);
          ctx.strokeStyle = "#fff4dc45";
          ctx.beginPath();
          ctx.moveTo(b.x, y);
          ctx.lineTo(b.x + b.w, y);
          ctx.stroke();
          for (
            let x = b.x + (Math.round((y - b.y) / plank) % 3) * plank * 2;
            x < b.x + b.w;
            x += plank * 6
          ) {
            ctx.strokeStyle = "#bd945335";
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(x, y + plank);
            ctx.stroke();
          }
        }
        if (b.w > 140 && b.h > 130) {
          ctx.strokeStyle = "#fff8d8a0";
          ctx.lineWidth = 1.6;
          const margin = Math.min(b.w, b.h) * 0.12;
          ctx.strokeRect(
            b.x + margin,
            b.y + margin,
            b.w - margin * 2,
            b.h - margin * 2,
          );
          ctx.beginPath();
          ctx.moveTo(b.x + margin, b.y + b.h / 2);
          ctx.lineTo(b.x + b.w - margin, b.y + b.h / 2);
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(
            b.x + b.w / 2,
            b.y + b.h / 2,
            Math.min(b.w, b.h) * 0.13,
            0,
            Math.PI * 2,
          );
          ctx.stroke();
        }
      } else {
        const tile = clamp(scale * 0.8, 16, 32);
        ctx.strokeStyle = "#ffffff65";
        ctx.lineWidth = 0.8;
        for (let x = b.x; x < b.x + b.w; x += tile) {
          ctx.beginPath();
          ctx.moveTo(x, b.y);
          ctx.lineTo(x, b.y + b.h);
          ctx.stroke();
        }
        for (let y = b.y; y < b.y + b.h; y += tile) {
          ctx.beginPath();
          ctx.moveTo(b.x, y);
          ctx.lineTo(b.x + b.w, y);
          ctx.stroke();
        }
      }
      ctx.restore();
      polygon(ctx, poly);
      ctx.lineWidth = clamp(scale * 0.18, 3, 6);
      ctx.strokeStyle = spec.edge;
      ctx.stroke();
      ctx.save();
      ctx.translate(0, -2);
      polygon(ctx, poly);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#f7fbf1";
      ctx.stroke();
      ctx.restore();
    }
    if (layer === "operations")
      for (const r of g.routes || []) {
        ctx.beginPath();
        (r.points || [])
          .map(point)
          .forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.strokeStyle = "#277dc38c";
        ctx.setLineDash([5, 5]);
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.setLineDash([]);
      }
    // Recorded walls/obstacles are rendered at their physical coordinates.
    for (const obstacle of g.obstacles || []) {
      if (!obstacle.polygon) continue;
      polygon(ctx, obstacle.polygon.map(point));
      ctx.fillStyle = "#a8b4b3";
      ctx.fill();
      ctx.strokeStyle = "#647a7d";
      ctx.stroke();
    }
    for (const wall of g.walls || []) {
      if (!wall.start || !wall.end) continue;
      const a = point(wall.start),
        b = point(wall.end);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.lineWidth = 5;
      ctx.strokeStyle = "#7b8b8d";
      ctx.stroke();
    }
    for (const room of Object.values(
      frame.geometry_overlay?.temporary_rooms || {},
    )) {
      if (!room.polygon_m?.length) continue;
      const poly = room.polygon_m.map(point);
      polygon(ctx, poly);
      ctx.fillStyle = room.active ? "#dcf2e783" : "#a9b3b333";
      ctx.fill();
      ctx.strokeStyle = room.active ? "#519d82" : "#8e9f9b";
      ctx.lineWidth = 2.5;
      ctx.setLineDash(room.active ? [] : [5, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
      for (const [index, bed] of (room.bed_points_m || []).entries()) {
        const p = point(bed),
          size = clamp(scale * 0.52, 8, 20),
          occupied = (room.observed_occupied_beds || []).some(
            (b) => b.bed_index === index,
          );
        ctx.fillStyle = occupied ? "#dba868" : "#86bca0";
        ctx.strokeStyle = "#f8fff8";
        ctx.lineWidth = 1;
        round(
          ctx,
          p.x - size * 0.45,
          p.y - size * 0.65,
          size * 0.9,
          size * 1.3,
          2,
        );
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#edf7df";
        round(
          ctx,
          p.x - size * 0.32,
          p.y - size * 0.5,
          size * 0.64,
          size * 0.3,
          1,
        );
        ctx.fill();
      }
      const first = poly[0];
      ctx.font = "bold 10px sans-serif";
      ctx.textAlign = "left";
      ctx.fillStyle = "#256253";
      ctx.fillText(
        room.room_id + " · " + (room.active ? "使用中" : "無効"),
        first.x + 5,
        first.y + 14,
      );
    }
    for (const f of frame.facilities || []) {
      const physical = f.point || g.facility_points?.[f.id];
      const positions = f.service_queues?.length
        ? f.service_queues.map((q) => q.point).filter(Boolean)
        : physical
          ? [physical]
          : [];
      const distinct = [
        ...new Map(positions.map((p) => [p.x + "," + p.y, p])).values(),
      ];
      for (const physical of distinct) {
        const p = point(physical),
          s = clamp(scale * 0.85, compact ? 10 : 16, compact ? 18 : 27);
        const kind =
          f.kind === "medical_support"
            ? "medical"
            : f.kind === "meal_distribution" || f.kind === "food_distribution"
              ? "box"
              : f.kind === "water_station" || f.kind === "water_distribution"
                ? "water"
                : f.kind === "toilet"
                  ? "wc"
                  : "desk";
        const limited =
          typeof f.working_units === "number" && f.working_units < f.capacity;
        ctx.fillStyle = "#25425625";
        round(ctx, p.x - s * 0.6, p.y - s * 0.5 + 3, s * 1.2, s * 1.15, 3);
        ctx.fill();
        ctx.fillStyle = limited ? "#fff0ef" : "#f9fcf7";
        ctx.strokeStyle = limited ? "#f35c68" : "#8da5ad";
        ctx.lineWidth = 1.5;
        round(ctx, p.x - s * 0.6, p.y - s * 0.6, s * 1.2, s * 1.15, 3);
        ctx.fill();
        ctx.stroke();
        symbol(
          ctx,
          kind,
          p.x,
          p.y - s * 0.08,
          s * 0.66,
          limited ? "#ed425c" : kind === "medical" ? "#ec5172" : "#387ca1",
        );
        if (layer === "operations" && s > 16) {
          ctx.font = "bold 10px sans-serif";
          ctx.textAlign = "center";
          ctx.fillStyle = limited ? "#dc3358" : "#264d6c";
          ctx.fillText(
            `${f.working_units ?? f.capacity}/${f.capacity}`,
            p.x,
            p.y + s + 6,
          );
        }
        hits.push({
          type: "facility",
          id: f.id,
          x: p.x,
          y: p.y,
          r: s * 0.8,
          label: `${f.id} · 稼働 ${f.working_units ?? f.capacity}/${f.capacity}`,
        });
      }
    }
    drawActors(ctx, frame, point, scale, w, h, options, hits);
    // Room captions remain in each recorded polygon; narrow rooms get short multiline names.
    for (const b of bounds) {
      if (b.w < 15 || b.h < 15) continue;
      const { spec, key } = b;
      const main = key === "sleeping",
        corridor = key === "corridor";
      if (corridor) {
        ctx.fillStyle = "#ffffffbd";
        round(ctx, b.x + 4, b.y + 4, b.w - 8, Math.min(24, b.h - 8), 3);
        ctx.fill();
        ctx.font = `bold ${compact ? 9 : 12}px sans-serif`;
        ctx.fillStyle = "#34536c";
        ctx.textAlign = "center";
        ctx.fillText(spec.name, b.x + b.w / 2, b.y + Math.min(20, b.h * 0.72));
        continue;
      }
      const font = main
          ? compact
            ? 11
            : 15
          : clamp(b.w / 3.4, compact ? 7.5 : 9, 12),
        lines = main
          ? [
              (frame.zones || []).find((z) => z.id === b.z.zone_id)?.name ||
                spec.name,
            ]
          : options.diagram
            ? [spec.short.replace("\n", "・")]
            : spec.short.split("\n");
      const cardW = main ? Math.min(b.w - 16, 230) : Math.max(12, b.w - 7),
        cardH = main
          ? compact
            ? 20
            : 34
          : options.diagram
            ? 24
            : lines.length * (font + 2) + (b.w > 33 ? 24 : 11),
        cx = b.x + b.w / 2,
        top = b.y + 8;
      ctx.fillStyle = "#fffffff0";
      ctx.strokeStyle = "#d1dfdb";
      ctx.lineWidth = 0.8;
      round(ctx, cx - cardW / 2, top, cardW, Math.min(cardH, b.h - 12), 4);
      ctx.fill();
      ctx.stroke();
      let yy = top + font + 7;
      if (!main && b.w > 33 && !options.diagram) {
        symbol(
          ctx,
          spec.icon,
          cx,
          top + 12,
          13,
          key === "medical" ? "#ed5170" : "#2a6993",
        );
        yy = top + font + 24;
      }
      ctx.fillStyle = "#173c69";
      ctx.font = `700 ${font}px "Hiragino Sans",sans-serif`;
      ctx.textAlign = "center";
      lines.forEach((line, i) =>
        ctx.fillText(line, cx, yy + i * (font + 2), cardW - 5),
      );
    }
    // Scale is derived from the physical coordinate transform, never from the backdrop.
    if (w > 450 && !options.diagram) {
      const meters = scale * 5 < w * 0.2 ? 5 : 2,
        sw = meters * scale,
        x = w - sw - 18,
        y = h - 18;
      ctx.fillStyle = "#ffffffdf";
      round(ctx, x - 8, y - 17, sw + 16, 28, 4);
      ctx.fill();
      ctx.strokeStyle = "#3d6178";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + sw, y);
      ctx.moveTo(x, y - 4);
      ctx.lineTo(x, y + 2);
      ctx.moveTo(x + sw, y - 4);
      ctx.lineTo(x + sw, y + 2);
      ctx.stroke();
      ctx.font = "9px sans-serif";
      ctx.fillStyle = "#3d6178";
      ctx.textAlign = "center";
      ctx.fillText(`${meters} m`, x + sw / 2, y - 5);
    }
    return hits;
  }
  const staticCache = new WeakMap();
  function drawCached(sf, source, options = {}) {
    const frame =
      options.mode === "schematic"
        ? window.HinanjoScene.project(source, options.mode)
        : source;
    if (!frame?.geometry) return draw(sf, frame, options);
    const { ctx, w, h } = sf,
      g = frame.geometry;
    const counts = {};
    for (const a of frame.agents || [])
      counts[a.zone_id] = (counts[a.zone_id] || 0) + 1;
    const key = JSON.stringify([
      w,
      h,
      devicePixelRatio,
      options.layer,
      options.zoom,
      options.pan,
      options.viewportBounds,
      g,
      frame.geometry_overlay,
      frame.facilities,
      options.layer === "crowd" ? counts : null,
    ]);
    let cache = staticCache.get(ctx.canvas);
    if (!cache || cache.key !== key) {
      const canvas = document.createElement("canvas"),
        dpr = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.ceil(w * dpr);
      canvas.height = Math.ceil(h * dpr);
      const context = canvas.getContext("2d");
      context.scale(dpr, dpr);
      const hits = draw(
        { ctx: context, w, h },
        { ...frame, agents: [], people: [], staff: [] },
        {
          ...options,
          crowdCounts: counts,
          diagram: frame.presentation_mode === "schematic",
        },
      );
      cache = { key, canvas, hits };
      staticCache.set(ctx.canvas, cache);
    }
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(cache.canvas, 0, 0, w, h);
    const compact = w < 460,
      pad = compact ? 12 : 22;
    const viewW = options.viewportBounds?.width || g.width_m,
      viewH = options.viewportBounds?.height || g.height_m;
    const scale =
      Math.min((w - pad * 2) / viewW, (h - pad * 2) / viewH) *
      (options.zoom || 1);
    const ox = (w - viewW * scale) / 2 + (options.pan?.x || 0),
      oy = (h - viewH * scale) / 2 + (options.pan?.y || 0);
    const point = (p) => ({ x: ox + p.x * scale, y: oy + p.y * scale });
    const hits = cache.hits.map((hit) => ({ ...hit }));
    drawActors(
      ctx,
      frame,
      point,
      scale,
      w,
      h,
      { ...options, diagram: frame.presentation_mode === "schematic" },
      hits,
    );
    return hits;
  }
  window.HinanjoMap = { draw: drawCached };
})();
