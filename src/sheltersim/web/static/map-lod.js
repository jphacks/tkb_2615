/* Display-only level of detail for recorded shelter maps. Never changes frame data. */
(() => {
  "use strict";
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  function pointGroups(items, getPoint, projectPoint, options = {}) {
    const width = Math.max(1, Number(options.width) || 1);
    const height = Math.max(1, Number(options.height) || 1);
    const zoom = Math.max(0.1, Number(options.zoom) || 1);
    const maxDirect = Math.max(1, Number(options.maxDirect) || 180);
    const projected = [];
    for (const item of items || []) {
      const point = getPoint(item);
      if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
      const [x, y] = projectPoint(point);
      if (Number.isFinite(x) && Number.isFinite(y)) projected.push({ item, x, y });
    }
    const density = projected.length / (width * height);
    const screenDensity = density / (zoom * zoom);
    const compact = projected.length > maxDirect && (screenDensity > 0.00022 || (projected.length > 500 && zoom < 1.4));
    if (!compact) {
      return { total: projected.length, clustered: 0, groups: projected.map(row => ({ kind: "item", ...row })) };
    }

    const cellSize = clamp((zoom < 0.75 ? 24 : zoom < 1.5 ? 18 : 12) + Math.min(8, density * 1600), 9, 30);
    const cells = new Map(), groups = [];
    for (const row of projected) {
      if (options.forceIndividual?.(row.item)) {
        groups.push({ kind: "item", ...row });
        continue;
      }
      const zone = options.groupKey?.(row.item) || "";
      const key = `${zone}:${Math.floor(row.x / cellSize)}:${Math.floor(row.y / cellSize)}`;
      let cell = cells.get(key);
      if (!cell) {
        cell = { kind: "cluster", xTotal: 0, yTotal: 0, count: 0, items: [] };
        cells.set(key, cell);
      }
      cell.xTotal += row.x;
      cell.yTotal += row.y;
      cell.count += 1;
      cell.items.push(row.item);
    }
    for (const cell of cells.values()) {
      if (cell.count === 1) {
        groups.push({ kind: "item", item: cell.items[0], x: cell.xTotal, y: cell.yTotal });
      } else {
        groups.push({ kind: "cluster", x: cell.xTotal / cell.count, y: cell.yTotal / cell.count, count: cell.count, items: cell.items });
      }
    }
    groups.sort((a, b) => a.x - b.x || a.y - b.y || (a.kind === "item" ? -1 : 1));
    return { total: projected.length, clustered: projected.length - groups.filter(row => row.kind === "item").length, groups };
  }

  function limitRelated(items, zoom, priority = () => false, low = 120, medium = 360, high = 900) {
    const rows = Array.isArray(items) ? items : [];
    const limit = zoom < 1 ? low : zoom < 2 ? medium : high;
    if (rows.length <= limit) return { total: rows.length, items: rows };
    const selected = rows.filter(priority), selectedSet = new Set(selected);
    const rest = rows.filter(row => !selectedSet.has(row));
    return { total: rows.length, items: [...selected.slice(0, limit), ...rest.slice(0, Math.max(0, limit - selected.length))] };
  }

  window.ShelterSimMapLOD = Object.freeze({ pointGroups, limitRelated });
})();
