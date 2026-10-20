import type {LayoutResult, Point} from './graphLayout';

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
function seed(value: string) {
  let hash = 2166136261;
  for (const character of value) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  return (hash >>> 0) / 4294967295;
}

/** A static overview constellation. Camera interpolation belongs to the view;
 * card geometry, saved sizes, and source connection anchors stay authoritative. */
export function organicOverviewPositions(layout: LayoutResult): Map<string, Point> {
  const result = new Map(layout.positions);
  for (const group of layout.groups) {
    const ids = [...group.nodeIds].sort();
    if (!ids.length) continue;
    const scale = group.recordScale || 1;
    // The widest file silhouette is 1.2 × glyph size. Reserve a
    // 136-unit glyph plus 12 units of motion on each side.
    const margin = 96 * scale;
    const left = group.x + group.contentPadding + margin;
    const right = Math.max(left, group.x + group.width - group.contentPadding - margin);
    const top = group.y + group.headerHeight + margin;
    const bottom = Math.max(top, group.y + group.height - group.contentPadding - margin);
    const center = {x: (left + right) / 2, y: (top + bottom) / 2};
    const rx = (right - left) / 2, ry = (bottom - top) / 2;
    const angle = seed(group.id) * Math.PI * 2;
    const points = ids.map((id, index): Point => {
      if (ids.length === 1) return {...center};
      if (ids.length === 2) {
        const direction = index === 0 ? -1 : 1;
        return {x: center.x + direction * rx, y: center.y + direction * ry};
      }
      const theta = ids.length === 3 ? -Math.PI / 2 + index * Math.PI * 2 / 3 : angle + index * GOLDEN_ANGLE;
      const radius = ids.length === 3 ? .72 : Math.sqrt((index + .5) / ids.length) * .95;
      // Small seeded variation softens the spiral without introducing a frame
      // simulation or depending on API record order.
      const variation = ids.length <= 3 ? 1 : .94 + seed(id) * .12;
      // Map the circular cloud into the rectangular body without imposing rows.
      const rectangular = Math.max(Math.abs(Math.cos(theta)), Math.abs(Math.sin(theta)));
      return {x: center.x + Math.cos(theta) / rectangular * rx * radius * variation, y: center.y + Math.sin(theta) / rectangular * ry * radius * variation};
    });
    // Stretch only outward, preserving separation and the seeded cloud shape.
    // Sparse groups deserve the same usable body coverage as dense groups.
    const fillBody = () => {
      if (points.length < 2) return;
      const minX = Math.min(...points.map(point => point.x)), maxX = Math.max(...points.map(point => point.x));
      const minY = Math.min(...points.map(point => point.y)), maxY = Math.max(...points.map(point => point.y));
      for (const point of points) {
        if (maxX > minX) point.x = left + (point.x - minX) / (maxX - minX) * (right - left);
        if (maxY > minY) point.y = top + (point.y - minY) / (maxY - minY) * (bottom - top);
      }
    };
    fillBody();
    // Include the expanded 216 × 204 record tile and its small hover/drift allowance.
    const clearanceX = 220 * scale, clearanceY = 210 * scale;
    // A bounded deterministic relaxation handles elongated and resized bodies.
    // The ordinary layout allocates substantially more area than these glyphs.
    for (let pass = 0; pass < 320; pass++) {
      let moved = false;
      for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
        const a = points[i], b = points[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        if (Math.abs(dx) >= clearanceX - .001 || Math.abs(dy) >= clearanceY - .001) continue;
        const distance = Math.hypot(dx, dy);
        if (distance < .001) {dx = Math.cos(i * GOLDEN_ANGLE); dy = Math.sin(i * GOLDEN_ANGLE);}
        const length = Math.hypot(dx, dy);
        // Follow the existing pair direction until one rectangular axis clears;
        // diagonal distance alone permits tile corners to intersect.
        const required = Math.min(clearanceX / Math.abs(dx / length), clearanceY / Math.abs(dy / length));
        const push = (required - distance) * .52;
        a.x -= dx / length * push; a.y -= dy / length * push;
        b.x += dx / length * push; b.y += dy / length * push;
        moved = true;
      }
      for (const point of points) {
        point.x = Math.min(right, Math.max(left, point.x));
        point.y = Math.min(bottom, Math.max(top, point.y));
      }
      if (!moved) break;
    }
    fillBody();
    ids.forEach((id, index) => result.set(id, points[index]));
  }
  return new Map([...result].sort(([a], [b]) => a.localeCompare(b)));
}
